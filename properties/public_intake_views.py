"""Public forms for requests that do not yet belong to a property."""

import uuid

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from billing.enforcement import TariffDenied

from .agency_access import agency_for, may_manage_agency
from .forms import PublicOwnerRequestForm, PublicSearchRequestForm
from .models import PublicIntakeLink, RealtorProfile
from .services.inbound_intake_service import ingest_owner, ingest_search
from .services.lead_notification_service import LeadNotificationService
from .services.public_form_security import PublicLeadFormSecurity


class PublicIntakeLinksView(LoginRequiredMixin, View):
    def get(self, request):
        return redirect("lead_list")

    def post(self, request):
        scope = request.POST.get("scope")
        if scope == "personal":
            link = get_object_or_404(PublicIntakeLink, owner=request.user)
        elif scope == "team":
            agency = agency_for(request.user)
            if not agency or not may_manage_agency(request.user):
                raise Http404
            link = get_object_or_404(PublicIntakeLink, agency=agency)
        else:
            raise Http404
        action = request.POST.get("action")
        if action not in {"pause", "resume"}:
            raise Http404
        link.enabled = action == "resume"
        link.save(update_fields=["enabled"])
        return redirect("lead_list")


def submit_public_request(request, link, kind, form, *, channel="public_form", event_prefix="", assigned_to=None):
    """Validate and ingest either public form; return whether it was accepted."""
    if request.POST.get(form.add_prefix("website"), "").strip():
        return True
    if not PublicLeadFormSecurity.allow_submission(request, f"intake:{link.pk}"):
        form.add_error(None, "Слишком много попыток. Подождите несколько минут и попробуйте снова.")
    if not form.is_valid() or form.non_field_errors():
        return False
    if not PublicLeadFormSecurity.verify_turnstile(request):
        form.add_error(None, "Не удалось подтвердить отправку. Попробуйте ещё раз.")
        return False
    data = form.cleaned_data
    external_id = f"{event_prefix}{kind}:{data['request_id']}"
    try:
        with transaction.atomic():
            if kind == "search":
                record, created = ingest_search(
                    link, channel=channel, external_id=external_id,
                    name=data["name"], phone=data["phone"], message=data["message"].strip(),
                    interest_type=data["interest_type"], source_detail=data["source_detail"], consent=True,
                    assigned_to=assigned_to,
                )
                if created:
                    profile = RealtorProfile.objects.filter(user=assigned_to or link.responsible).first()
                    record_url = request.build_absolute_uri(reverse("lead_detail", args=[record.pk]))
                    LeadNotificationService.notify_new_lead(record, profile, record_url)
            else:
                ingest_owner(
                    link, channel=channel, external_id=external_id,
                    name=data["name"], phone=data["phone"], deal_type=data["deal_type"],
                    address=data["address"].strip(), description=data["description"].strip(),
                    expected_price=data["expected_price"], source_detail=data["source_detail"], consent=True,
                    assigned_to=assigned_to,
                )
    except TariffDenied:
        form.add_error(None, "Сейчас отправить обращение не удалось. Свяжитесь с риелтором напрямую.")
        return False
    except ValueError:
        raise Http404
    return True


class PublicRequestView(View):
    template_name = "properties/public_intake_form.html"

    def link_for(self, code):
        link = get_object_or_404(
            PublicIntakeLink.objects.select_related("owner", "agency__owner"), code=code, enabled=True,
        )
        if link.agency_id and link.agency.closed_at:
            raise Http404
        if not link.responsible.is_active:
            raise Http404
        return link

    def context(self, link, form, kind, **extra):
        profile = RealtorProfile.objects.filter(user=link.responsible).first()
        return {
            "form": form,
            "kind": kind,
            "workspace_name": link.agency.name if link.agency_id else (
                profile.display_name if profile and profile.display_name else link.responsible.get_full_name() or "Риелтор"
            ),
            "turnstile_enabled": settings.TURNSTILE_ENABLED,
            "turnstile_site_key": settings.TURNSTILE_SITE_KEY,
            **extra,
        }

    def get(self, request, code, kind):
        link = self.link_for(code)
        form_class = PublicSearchRequestForm if kind == "search" else PublicOwnerRequestForm
        source_detail = "Форма подбора" if kind == "search" else "Форма собственника"
        utm = request.GET.get("utm_source", "").strip()[:100]
        if utm:
            source_detail = f"{source_detail} · {utm}"
        form = form_class(initial={"request_id": uuid.uuid4(), "source_detail": source_detail})
        return render(request, self.template_name, self.context(link, form, kind))

    def post(self, request, code, kind):
        link = self.link_for(code)
        form_class = PublicSearchRequestForm if kind == "search" else PublicOwnerRequestForm
        form = form_class(request.POST)
        if submit_public_request(request, link, kind, form):
            return render(request, self.template_name, self.context(link, form_class(), kind, sent=True))
        return render(request, self.template_name, self.context(link, form, kind))
