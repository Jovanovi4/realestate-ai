from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View

from .agency_access import workspace_queryset
from .models import Property, PropertyOwnerLink
from .services.owner_report_service import owner_report_data, owner_report_pdf


def visible_owner_link(user, link_pk):
    property_ids = workspace_queryset(Property.objects, user).values("pk")
    return get_object_or_404(
        PropertyOwnerLink.objects.select_related("property", "client"), pk=link_pk, property_id__in=property_ids,
    )


def report_days(request):
    return 90 if request.GET.get("days") == "90" else 30


class OwnerAgreementListView(LoginRequiredMixin, View):
    def get(self, request):
        today = timezone.localdate()
        scope = "all" if request.GET.get("scope") == "all" else "soon"
        links = workspace_queryset(PropertyOwnerLink.objects, request.user, prefix="property__").filter(
            agreement_expires_on__isnull=False,
        ).exclude(property__status="archived").select_related("property", "client")
        if scope == "soon":
            links = links.filter(agreement_expires_on__lte=today + timedelta(days=30))
        page = Paginator(links.order_by("agreement_expires_on", "pk"), 25).get_page(request.GET.get("page"))
        return render(request, "properties/owner_agreements.html", {
            "links": page.object_list, "page_obj": page, "scope": scope, "today": today,
            "soon_until": today + timedelta(days=30),
        })


class OwnerReportView(LoginRequiredMixin, View):
    def get(self, request, link_pk):
        link = visible_owner_link(request.user, link_pk)
        response = render(request, "properties/owner_report.html", owner_report_data(link, days=report_days(request)))
        response["Cache-Control"] = "private, no-store"
        return response


class OwnerReportPDFView(LoginRequiredMixin, View):
    def get(self, request, link_pk):
        link = visible_owner_link(request.user, link_pk)
        report = owner_report_data(link, days=report_days(request))
        response = HttpResponse(owner_report_pdf(report), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="owner-report-{link.property_id}-{timezone.localdate():%Y%m%d}.pdf"'
        response["Cache-Control"] = "private, no-store"
        return response
