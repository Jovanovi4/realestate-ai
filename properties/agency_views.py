from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from .agency_access import membership_for, may_manage_agency, workspace_queryset
from .models import (
    Agency, AgencyInvitation, AgencyMembership, Client, ClientReminder, Deal,
    Lead, Property, Showing,
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
        })


class AgencyCreateView(LoginRequiredMixin, View):
    def post(self, request):
        if membership_for(request.user):
            return redirect("agency_dashboard")
        form = AgencyCreateForm(request.POST)
        if not form.is_valid():
            return render(request, "properties/agency_dashboard.html", {"create_form": form}, status=400)
        with transaction.atomic():
            agency, _ = Agency.objects.get_or_create(owner=request.user, defaults={"name": form.cleaned_data["name"]})
            AgencyMembership.objects.get_or_create(agency=agency, user=request.user, defaults={"role": "owner"})
            # The founder explicitly converts their existing personal database into shared agency data.
            for model in (Property, Client, Deal, ClientReminder, Showing):
                for record in model.objects.filter(owner=request.user, agency__isnull=True).iterator():
                    record.agency = agency
                    record.save(update_fields=["agency"])
        messages.success(request, "Агентство создано. Ваши объекты, клиенты и сделки стали общей базой команды.")
        return redirect("agency_dashboard")


class AgencyInviteView(LoginRequiredMixin, View):
    def post(self, request):
        membership = membership_for(request.user)
        if not membership or membership.role not in {"owner", "manager"}:
            raise Http404
        form = InvitationForm(request.POST)
        if form.is_valid() and (membership.role == "owner" or form.cleaned_data["role"] == "agent"):
            invite = AgencyInvitation.objects.create(
                agency=membership.agency, role=form.cleaned_data["role"], created_by=request.user,
                expires_at=timezone.now() + timedelta(days=7),
            )
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
        with transaction.atomic():
            invite = get_object_or_404(AgencyInvitation.objects.select_for_update().select_related("agency"), code=code, accepted_by__isnull=True, expires_at__gt=timezone.now())
            if membership_for(request.user):
                messages.error(request, "Вы уже состоите в агентстве.")
                return redirect("agency_dashboard")
            AgencyMembership.objects.create(agency=invite.agency, user=request.user, role=invite.role)
            invite.accepted_by = request.user
            invite.save(update_fields=["accepted_by"])
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
        replacement = membership.agency.owner
        with transaction.atomic():
            for model in (Property, Client, Deal, ClientReminder, Showing):
                for record in model.objects.filter(agency=membership.agency, owner=target.user).iterator():
                    record.owner = replacement
                    record.save(update_fields=["owner"])
            for lead in Lead.objects.filter(property__agency=membership.agency, assigned_to=target.user).iterator():
                lead.assigned_to = replacement
                lead.save(update_fields=["assigned_to"])
            for deal in Deal.objects.filter(agency=membership.agency, responsible=target.user).iterator():
                deal.responsible = replacement
                deal.save(update_fields=["responsible"])
            target.delete()
        messages.success(request, "Сотрудник исключён; его активные назначения переданы владельцу агентства.")
        return redirect("agency_dashboard")


class LeadAssignView(LoginRequiredMixin, View):
    def post(self, request, pk):
        membership = membership_for(request.user)
        if not membership or membership.role not in {"owner", "manager"}:
            raise Http404
        lead = get_object_or_404(workspace_queryset(Lead.objects, request.user, prefix="property__"), pk=pk, property__agency=membership.agency)
        assignee_id = request.POST.get("assigned_to", "")
        if not assignee_id.isdigit():
            raise Http404
        assignee = get_object_or_404(AgencyMembership, agency=membership.agency, user_id=assignee_id)
        lead.assigned_to = assignee.user
        lead.save(update_fields=["assigned_to", "updated_at"])
        messages.success(request, "Ответственный за заявку назначен.")
        return redirect("lead_detail", pk=lead.pk)
