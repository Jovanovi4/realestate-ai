from datetime import timedelta
import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from billing.models import BillingAccount
from billing.enforcement import TariffDenied, enabled as billing_enforcement_enabled, require_allowed
from billing.rules import PolicyService
from billing.reservations import DuplicateResourceRequest, request_identity, reserved_resources
from billing.rules import Operation
from billing.services import AccountService
from billing.usage import attempt

from .agency_access import membership_for, may_manage_agency, workspace_queryset
from .models import (
    Agency, AgencyInvitation, AgencyMembership, Client, ClientReminder, Deal,
    Lead, OwnerInquiry, Property, Showing,
)


class AgencyCreateForm(forms.Form):
    name = forms.CharField(label="Название агентства", max_length=150)


class InvitationForm(forms.Form):
    role = forms.ChoiceField(label="Роль сотрудника", choices=AgencyInvitation._meta.get_field("role").choices)


class AgencyDashboardView(LoginRequiredMixin, View):
    def get(self, request):
        membership = membership_for(request.user)
        if not membership:
            return render(request, "properties/agency_dashboard.html", {"create_form": AgencyCreateForm()})
        agency = membership.agency
        audit_paginator = Paginator(agency.audit_events.select_related("actor").order_by("-created_at", "-pk"), 10)
        audit_events = audit_paginator.get_page(request.GET.get("history_page"))
        return render(request, "properties/agency_dashboard.html", {
            "membership": membership,
            "agency": agency,
            "members": agency.memberships.select_related("user", "user__realtor_profile").order_by("joined_at"),
            "invitations": agency.invitations.filter(accepted_by__isnull=True, expires_at__gt=timezone.now()).order_by("-created_at") if may_manage_agency(request.user) else [],
            "invitation_form": InvitationForm(),
            "audit_events": audit_events,
            "can_manage": may_manage_agency(request.user),
            "is_owner": membership.role == "owner",
            "resource_request_key": str(uuid.uuid4()),
        })


class AgencyCreateView(LoginRequiredMixin, View):
    def post(self, request):
        if membership_for(request.user):
            return redirect("agency_dashboard")
        form = AgencyCreateForm(request.POST)
        if not form.is_valid():
            return render(request, "properties/agency_dashboard.html", {"create_form": form}, status=400)
        try:
            with transaction.atomic():
                personal = AccountService.ensure_personal(request.user)
                BillingAccount.objects.select_for_update().get(pk=personal.pk)
                require_allowed(PolicyService.evaluate(personal, request.user, Operation.CREATE_AGENCY))
                agency, _ = Agency.objects.get_or_create(owner=request.user, defaults={"name": form.cleaned_data["name"]})
                if billing_enforcement_enabled():
                    AccountService.convert_founder_account(agency, request.user)
                else:
                    attempt(lambda: AccountService.convert_founder_account(agency, request.user))
                AgencyMembership.objects.get_or_create(agency=agency, user=request.user, defaults={"role": "owner"})
                # The founder explicitly converts their existing personal database into shared agency data.
                for model in (Property, Client, Deal, ClientReminder, Showing):
                    for record in model.objects.filter(owner=request.user, agency__isnull=True).iterator():
                        record.agency = agency
                        record.save(update_fields=["agency"])
        except TariffDenied as error:
            form.add_error(None, error.message)
            return render(request, "properties/agency_dashboard.html", {"create_form": form}, status=403)
        messages.success(request, "Агентство создано. Ваши объекты, клиенты и сделки стали общей базой команды.")
        return redirect("agency_dashboard")


class AgencyInviteView(LoginRequiredMixin, View):
    def post(self, request):
        membership = membership_for(request.user)
        if not membership or membership.role not in {"owner", "manager"}:
            raise Http404
        form = InvitationForm(request.POST)
        if form.is_valid() and (membership.role == "owner" or form.cleaned_data["role"] == "agent"):
            account = AccountService.ensure_agency(membership.agency)
            try:
                with reserved_resources(account=account, actor=request.user, action=Operation.INVITE_MEMBER,
                        amounts={"invited_seats": 1}, invited_role=form.cleaned_data["role"],
                        **request_identity(request)) as reservation:
                    invite = AgencyInvitation.objects.create(
                        agency=membership.agency, role=form.cleaned_data["role"], created_by=request.user,
                        expires_at=timezone.now() + timedelta(days=7),
                    )
                    reservation.result_id = str(invite.pk)
            except DuplicateResourceRequest as duplicate:
                invite = AgencyInvitation.objects.filter(pk=duplicate.reservation.result_id, agency=membership.agency).first()
                if invite is None:
                    messages.info(request, "Этот запрос уже обрабатывается или приглашение отозвано.")
                    return redirect("agency_dashboard")
            except ValidationError:
                messages.error(request, "Ключ запроса уже использован для другого приглашения. Обновите страницу и повторите действие.")
                return redirect("agency_dashboard")
            except TariffDenied as error:
                messages.error(request, error.message)
                return redirect("agency_dashboard")
            messages.success(request, f"Приглашение создано. Передайте сотруднику ссылку: {request.build_absolute_uri(reverse('agency_join', args=[invite.code]))}")
        else:
            messages.error(request, "Выберите роль сотрудника.")
        return redirect("agency_dashboard")


class AgencyJoinView(LoginRequiredMixin, View):
    def get(self, request, code):
        invite = get_object_or_404(AgencyInvitation.objects.select_related("agency"), code=code, accepted_by__isnull=True, expires_at__gt=timezone.now())
        if membership_for(request.user):
            messages.error(request, "Вы уже состоите в агентстве.")
            return redirect("agency_dashboard")
        return render(request, "properties/agency_join.html", {"invite": invite})

    def post(self, request, code):
        visible = get_object_or_404(AgencyInvitation.objects.select_related("agency"), code=code,
            accepted_by__isnull=True, expires_at__gt=timezone.now())
        account = AccountService.ensure_agency(visible.agency)
        try:
            with transaction.atomic():
                BillingAccount.objects.select_for_update().get(pk=account.pk)
                invite = get_object_or_404(AgencyInvitation.objects.select_for_update().select_related("agency"), code=code, accepted_by__isnull=True, expires_at__gt=timezone.now())
                if membership_for(request.user):
                    messages.error(request, "Вы уже состоите в агентстве.")
                    return redirect("agency_dashboard")
                require_allowed(PolicyService.evaluate(account, request.user, Operation.ACCEPT_INVITE, resource=invite))
                AgencyMembership.objects.create(agency=invite.agency, user=request.user, role=invite.role)
                invite.accepted_by = request.user
                invite.save(update_fields=["accepted_by"])
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("agency_join", code=code)
        messages.success(request, f"Вы присоединились к агентству «{invite.agency.name}».")
        return redirect("agency_dashboard")


class AgencyInviteRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        membership = membership_for(request.user)
        if not membership or membership.role not in {"owner", "manager"}:
            raise Http404
        invite = get_object_or_404(AgencyInvitation, pk=pk, agency=membership.agency, accepted_by__isnull=True)
        if membership.role == "manager" and invite.created_by_id != request.user.pk:
            raise Http404
        try:
            account = AccountService.ensure_agency(membership.agency)
            require_allowed(PolicyService.evaluate(account, request.user, Operation.EDIT_CRM))
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("agency_dashboard")
        invite.delete()
        messages.success(request, "Приглашение отозвано.")
        return redirect("agency_dashboard")


class AgencyMemberRoleView(LoginRequiredMixin, View):
    def post(self, request, pk):
        membership = membership_for(request.user)
        if not membership or membership.role != "owner":
            raise Http404
        target = get_object_or_404(AgencyMembership, pk=pk, agency=membership.agency)
        role = request.POST.get("role")
        if target.role == "owner" or role not in {"manager", "agent"}:
            raise Http404
        try:
            account = AccountService.ensure_agency(membership.agency)
            require_allowed(PolicyService.evaluate(account, request.user, Operation.CHANGE_MEMBER_ROLE,
                resource=target, invited_role=role))
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("agency_dashboard")
        target.role = role
        target.save(update_fields=["role"])
        messages.success(request, "Роль сотрудника обновлена.")
        return redirect("agency_dashboard")


class AgencyMemberRemoveView(LoginRequiredMixin, View):
    def post(self, request, pk):
        membership = membership_for(request.user)
        if not membership or membership.role != "owner":
            raise Http404
        target = get_object_or_404(AgencyMembership, pk=pk, agency=membership.agency)
        if target.role == "owner":
            raise Http404
        try:
            account = AccountService.ensure_agency(membership.agency)
            require_allowed(PolicyService.evaluate(account, request.user, Operation.REMOVE_MEMBER, resource=target))
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("agency_dashboard")
        replacement = membership.agency.owner
        with transaction.atomic():
            for model in (Property, Client, Deal, ClientReminder, Showing, OwnerInquiry):
                for record in model.objects.filter(agency=membership.agency, owner=target.user).iterator():
                    record.owner = replacement
                    record.save(update_fields=["owner"])
            for lead in Lead.objects.filter(property__agency=membership.agency, assigned_to=target.user).iterator():
                lead.assigned_to = replacement
                lead.save(update_fields=["assigned_to"])
            for inquiry in OwnerInquiry.objects.filter(agency=membership.agency, assigned_to=target.user).iterator():
                inquiry.assigned_to = replacement
                inquiry.save(update_fields=["assigned_to", "updated_at"])
            for deal in Deal.objects.filter(agency=membership.agency, responsible=target.user).iterator():
                deal.responsible = replacement
                deal.save(update_fields=["responsible"])
            target.delete()
        messages.success(request, "Сотрудник исключён; его активные назначения переданы владельцу агентства.")
        return redirect("agency_dashboard")


class LeadAssignView(LoginRequiredMixin, View):
    def post(self, request, pk):
        from .views import is_lead_panel_request, lead_panel_feedback
        membership = membership_for(request.user)
        if not membership or membership.role not in {"owner", "manager"}:
            raise Http404
        lead = get_object_or_404(
            workspace_queryset(Lead.objects, request.user, prefix="property__")
            .select_related("property", "client", "assigned_to__realtor_profile"),
            pk=pk, property__agency=membership.agency,
        )
        assignee_id = request.POST.get("assigned_to", "")
        if not assignee_id.isdigit():
            if is_lead_panel_request(request):
                return lead_panel_feedback(request, lead, "Выберите сотрудника команды.", level="error")
            raise Http404
        assignee = AgencyMembership.objects.filter(agency=membership.agency, user_id=assignee_id).first()
        if not assignee:
            if is_lead_panel_request(request):
                return lead_panel_feedback(request, lead, "Выберите сотрудника команды.", level="error")
            raise Http404
        try:
            account = AccountService.ensure_agency(membership.agency)
            require_allowed(PolicyService.evaluate(account, request.user, Operation.EDIT_CRM, resource=lead.property))
        except TariffDenied as error:
            if is_lead_panel_request(request):
                return lead_panel_feedback(request, lead, error.message, level="error")
            messages.error(request, error.message)
            return redirect(f"{reverse('lead_detail', args=[lead.pk])}#lead-about")
        lead.assigned_to = assignee.user
        lead.save(update_fields=["assigned_to", "updated_at"])
        if is_lead_panel_request(request):
            return lead_panel_feedback(request, lead, "Ответственный за заявку назначен.")
        messages.success(request, "Ответственный за заявку назначен.")
        return redirect(f"{reverse('lead_detail', args=[lead.pk])}#lead-about")
