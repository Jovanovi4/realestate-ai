"""Manager-facing team queue and quick reassignment of open work."""

from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, DateTimeField, ExpressionWrapper, F, When
from django.db.models.functions import Least
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from billing.enforcement import TariffDenied, require_allowed
from billing.models import BillingAccount
from billing.rules import Operation, PolicyService
from billing.services import AccountService

from .agency_access import membership_for
from .models import AgencyMembership, Lead
from .services.crm_analytics_service import AnalyticsPeriod, resolve_scope, team_open_work


def managing_membership(user):
    membership = membership_for(user)
    if not membership or membership.role not in {"owner", "manager"}:
        raise Http404
    return membership


class AgencyWorkloadView(LoginRequiredMixin, View):
    template_name = "properties/agency_workload.html"

    def get(self, request):
        membership = managing_membership(request.user)
        members = list(AgencyMembership.objects.filter(agency=membership.agency)
                       .select_related("user", "user__realtor_profile").order_by("user_id"))
        member_id = request.GET.get("member", "")
        if member_id and (not member_id.isdecimal() or int(member_id) not in {item.user_id for item in members}):
            raise Http404
        state = request.GET.get("state", "all")
        kind = request.GET.get("kind", "all")
        if state not in {"all", "unanswered", "overdue", "unassigned"} or kind not in {"all", "lead", "task"}:
            raise Http404
        if kind == "task" and state in {"unanswered", "unassigned"}:
            raise Http404
        if member_id and state == "unassigned":
            raise Http404

        period = AnalyticsPeriod.ending_now()
        scope = resolve_scope(request.user, requested_scope="team", membership=membership)
        leads, tasks = team_open_work(scope)
        if member_id:
            leads = leads.filter(assigned_to_id=int(member_id))
            tasks = tasks.filter(owner_id=int(member_id))
        if state == "unanswered":
            leads = leads.filter(first_contacted_at__isnull=True)
            tasks = tasks.none()
        elif state == "overdue":
            leads = leads.filter(Lead.overdue_filter(period.now))
            tasks = tasks.filter(due_at__lt=period.now)
        elif state == "unassigned":
            leads = leads.filter(assigned_to__isnull=True)
            tasks = tasks.none()
        if kind == "lead":
            tasks = tasks.none()
        elif kind == "task":
            leads = leads.none()

        first_response_due = ExpressionWrapper(F("created_at") + timedelta(minutes=30), output_field=DateTimeField())
        attention_due = Case(
            When(first_contacted_at__isnull=True, then=Least(first_response_due, F("next_action_due_at"))),
            default=F("next_action_due_at"), output_field=DateTimeField(),
        )
        leads = leads.select_related("property", "assigned_to__realtor_profile").annotate(
            queue_due_at=attention_due,
        ).order_by("queue_due_at", "pk")
        tasks = tasks.select_related("client", "deal__property", "property", "owner__realtor_profile").order_by(
            F("due_at").asc(nulls_last=True), "pk",
        )
        lead_page = Paginator(leads, 20).get_page(request.GET.get("lead_page"))
        task_page = Paginator(tasks, 20).get_page(request.GET.get("task_page"))

        def link(*, next_state=state, next_kind=kind, next_member=member_id, lead_page_number=None, task_page_number=None):
            params = {"state": next_state, "kind": next_kind}
            if next_member:
                params["member"] = next_member
            if lead_page_number:
                params["lead_page"] = lead_page_number
            if task_page_number:
                params["task_page"] = task_page_number
            return f"{reverse('agency_workload')}?{urlencode(params)}"

        return render(request, self.template_name, {
            "members": members,
            "member_id": member_id,
            "state": state,
            "kind": kind,
            "lead_page": lead_page,
            "task_page": task_page,
            "now": period.now,
            "state_links": [(value, label, link(next_state=value,
                next_kind="lead" if value in {"unanswered", "unassigned"} else kind,
                next_member="" if value == "unassigned" else member_id))
                            for value, label in (("all", "Все"), ("unanswered", "Без ответа"),
                                                  ("overdue", "Просрочено"), ("unassigned", "Без ответственного"))],
            "kind_links": [(value, label, link(next_kind=value,
                next_state="all" if value == "task" and state in {"unanswered", "unassigned"} else state)) for value, label in
                           (("all", "Все дела"), ("lead", "Заявки"), ("task", "Задачи"))],
            "member_links": [(item, link(next_member=str(item.user_id), next_state="all")) for item in members],
            "all_members_url": link(next_member="", next_state="all"),
            "current_url": link(lead_page_number=lead_page.number, task_page_number=task_page.number),
            "lead_previous_url": link(lead_page_number=lead_page.previous_page_number()) if lead_page.has_previous() else None,
            "lead_next_url": link(lead_page_number=lead_page.next_page_number()) if lead_page.has_next() else None,
            "task_previous_url": link(task_page_number=task_page.previous_page_number()) if task_page.has_previous() else None,
            "task_next_url": link(task_page_number=task_page.next_page_number()) if task_page.has_next() else None,
        })


class AgencyWorkAssignView(LoginRequiredMixin, View):
    def post(self, request):
        membership = managing_membership(request.user)
        kind = request.POST.get("kind")
        record_id = request.POST.get("record_id", "")
        assignee_id = request.POST.get("assigned_to", "")
        if kind not in {"lead", "task"} or not record_id.isdecimal() or not assignee_id.isdecimal():
            raise Http404
        return_url = request.POST.get("next", "")
        if not url_has_allowed_host_and_scheme(return_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            return_url = reverse("agency_workload")
        account = AccountService.ensure_agency(membership.agency)
        with transaction.atomic():
            BillingAccount.objects.select_for_update().get(pk=account.pk)
            assignee = get_object_or_404(AgencyMembership.objects.select_for_update(),
                                        agency=membership.agency, user_id=int(assignee_id))
            scope = resolve_scope(request.user, requested_scope="team", membership=membership)
            leads, tasks = team_open_work(scope)
            if kind == "lead":
                record = get_object_or_404(leads.select_for_update(of=("self",)).select_related("property"), pk=int(record_id))
                resource = record
            else:
                record = get_object_or_404(tasks.select_for_update(of=("self",)), pk=int(record_id))
                resource = record
            try:
                require_allowed(PolicyService.evaluate(account, request.user, Operation.EDIT_CRM, resource=resource))
            except TariffDenied as error:
                messages.error(request, error.message)
                return redirect(return_url)
            if kind == "lead":
                if record.assigned_to_id != assignee.user_id:
                    record.assigned_to = assignee.user
                    record.save(update_fields=["assigned_to", "updated_at"])
            elif record.owner_id != assignee.user_id:
                record.owner = assignee.user
                record.save(update_fields=["owner"])
        messages.success(request, "Ответственный назначен.")
        return redirect(return_url)
