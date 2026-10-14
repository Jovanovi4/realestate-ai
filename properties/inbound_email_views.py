"""Review mail deliveries and convert them without retyping into CRM records."""

import re
from email.utils import parseaddr

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from billing.enforcement import TariffDenied

from .agency_access import agency_for
from .forms import InboundEmailConvertForm, normalize_phone
from .models import InboundEvent, RealtorProfile
from .services.inbound_intake_service import ingest_owner, ingest_search
from .services.lead_notification_service import LeadNotificationService


def visible_mail(user):
    agency = agency_for(user)
    scope = Q(link__owner=user)
    if agency:
        scope |= Q(link__agency=agency)
    return InboundEvent.objects.filter(channel="email").filter(scope)


def suggested_phone(body):
    for match in re.finditer(r"(?<!\d)(?:\+7|7|8)[\d\s()\-]{9,22}\d", body):
        try:
            return normalize_phone(match.group().strip())
        except ValidationError:
            continue
    return ""


class InboundEmailListView(LoginRequiredMixin, View):
    def get(self, request):
        pending = visible_mail(request.user).filter(
            lead__isnull=True, owner_inquiry__isnull=True, ignored_at__isnull=True,
        ).select_related("link__agency").order_by("-received_at", "-pk")[:100]
        return render(request, "properties/inbound_email_list.html", {"pending": pending})


class InboundEmailDetailView(LoginRequiredMixin, View):
    template_name = "properties/inbound_email_detail.html"

    def event(self, request, pk):
        return get_object_or_404(visible_mail(request.user).select_related("link", "link__agency"), pk=pk)

    def render_form(self, request, event, form, status=200):
        return render(request, self.template_name, {"event": event, "form": form}, status=status)

    def get(self, request, pk):
        event = self.event(request, pk)
        if event.lead_id or event.owner_inquiry_id or event.ignored_at:
            raise Http404
        sender_name, _ = parseaddr(event.sender)
        form = InboundEmailConvertForm(initial={
            "kind": "search", "name": sender_name, "phone": suggested_phone(event.body),
            "interest_type": "buy", "deal_type": "sale", "message": event.body,
        })
        return self.render_form(request, event, form)

    def post(self, request, pk):
        event = self.event(request, pk)
        if event.lead_id or event.owner_inquiry_id or event.ignored_at:
            raise Http404
        if request.POST.get("action") == "ignore":
            event.ignored_at = timezone.now()
            event.save(update_fields=["ignored_at"])
            return redirect("inbound_email_list")
        form = InboundEmailConvertForm(request.POST)
        if not form.is_valid():
            return self.render_form(request, event, form, status=400)
        data = form.cleaned_data
        detail = f"{event.sender} · {event.subject}"[:255]
        try:
            with transaction.atomic():
                if data["kind"] == "search":
                    lead, created = ingest_search(
                        event.link, channel="email", external_id=event.external_id,
                        name=data["name"], phone=data["phone"], message=data["message"],
                        interest_type=data["interest_type"], source_detail=detail,
                        received_at=event.received_at, assigned_to=request.user,
                    )
                    if created:
                        profile = RealtorProfile.objects.filter(user=request.user).first()
                        record_url = request.build_absolute_uri(reverse("lead_detail", args=[lead.pk]))
                        LeadNotificationService.notify_new_lead(lead, profile, record_url)
                    return redirect("lead_detail", pk=lead.pk)
                inquiry, _ = ingest_owner(
                    event.link, channel="email", external_id=event.external_id,
                    name=data["name"], phone=data["phone"], deal_type=data["deal_type"],
                    address=data["address"].strip(), description=data["message"],
                    expected_price=data["expected_price"], source_detail=detail,
                    received_at=event.received_at, assigned_to=request.user,
                )
                return redirect("owner_inquiry_detail", pk=inquiry.pk)
        except TariffDenied as error:
            form.add_error(None, error.message)
        except ValueError as error:
            form.add_error(None, str(error))
        return self.render_form(request, event, form, status=400)
