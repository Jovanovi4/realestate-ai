"""Workspace integration pages."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.views import View

from .agency_access import membership_for
from .services.telegram_feed_overview import feed_overview


class TelegramIntegrationView(LoginRequiredMixin, View):
    def get(self, request):
        membership = membership_for(request.user)
        workspace = "team" if membership and request.GET.get("workspace") == "team" else "personal"
        overview = feed_overview(
            agency=membership.agency if workspace == "team" else None,
            owner=request.user if workspace == "personal" else None,
            page_number=request.GET.get("telegram_page"),
        )
        return render(request, "properties/telegram_integration.html", {
            "telegram_overview": overview,
            "telegram_workspace": workspace,
            "telegram_has_team": bool(membership),
        })
