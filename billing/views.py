from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from properties.agency_access import membership_for

from .models import BillingAccount
from .presentation import workspace_summary
from .usage import enabled


@method_decorator(never_cache, name="dispatch")
class BillingOverviewView(LoginRequiredMixin, View):
    def get(self, request):
        now = timezone.now()
        personal = BillingAccount.objects.filter(personal_user=request.user).first()
        workspaces = [{
            "kind": "personal",
            "title": "Личное пространство",
            "description": "Тариф для ваших личных объектов и CRM.",
            **workspace_summary(personal or BillingAccount(personal_user=request.user), now=now),
        }]
        membership = membership_for(request.user)
        if membership:
            team = BillingAccount.objects.filter(agency=membership.agency).first()
            workspaces.append({
                "kind": "team",
                "title": membership.agency.name,
                "description": "Общий тариф команды. Личный тариф не увеличивает лимиты команды.",
                **workspace_summary(team or BillingAccount(agency=membership.agency), now=now),
            })
        return render(request, "billing/overview.html", {
            "billing_workspaces": workspaces,
            "billing_has_team": len(workspaces) > 1,
            "billing_has_dates": any(workspace["period"] or workspace["upcoming_period"] for workspace in workspaces),
            "billing_timezone": timezone.get_current_timezone_name(),
            "billing_recording_enabled": enabled(),
        })
