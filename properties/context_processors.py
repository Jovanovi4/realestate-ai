from django.conf import settings
from django.utils import timezone

from .models import ClientReminder, Lead, OwnerInquiry, RealtorProfile, Showing
from .agency_access import membership_for, workspace_queryset


def active_navigation_item(request):
    name = (request.resolver_match.url_name or "") if request.resolver_match else ""
    if name in {"workday", "work_items", "task_create", "task_detail", "showing_create", "showing_detail"}:
        return "today"
    if name in {"landing_list", "landing_unpublish"}:
        return "landings"
    if name == "integration_telegram":
        return "telegram"
    if name == "analytics":
        return "analytics"
    if "property" in name or name in {
        "upload_images", "reorder_images", "set_primary_image", "avito_export", "cian_export", "ai_assistant", "telegram_publish",
    }:
        return "properties"
    if "lead" in name:
        return "leads"
    if name.startswith("owner_inquiry") or name.startswith("owner_agreement") or name.startswith("owner_report"):
        return "clients"
    if "client" in name:
        return "clients"
    if "deal" in name:
        return "deals"
    return ""


def realtor_profile(request):
    """Expose the current user's profile to the global navigation safely."""
    if not request.user.is_authenticated:
        return {"ai_enabled": settings.AI_ENABLED}
    membership = membership_for(request.user)
    new_leads = workspace_queryset(Lead.objects, request.user, prefix="property__").exclude(status="archived").filter(outcome="open", first_contacted_at__isnull=True)
    if membership and membership.role == "agent":
        new_leads = new_leads.filter(assigned_to=request.user)
    new_leads_count = new_leads.count()
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    overdue_count = ClientReminder.objects.filter(owner=request.user, is_done=False, cancelled_at__isnull=True, lead__isnull=True, due_at__lt=timezone.now()).count()
    overdue_count += Showing.objects.filter(owner=request.user, status="planned", starts_at__lt=today_start).count()
    overdue_count += workspace_queryset(Lead.objects, request.user, prefix="property__").filter(
        Lead.overdue_filter(timezone.now()), assigned_to=request.user,
    ).exclude(status="archived").count()
    owner_inquiries = workspace_queryset(OwnerInquiry.objects, request.user).filter(
        assigned_to=request.user, property__isnull=True,
    ).exclude(result="declined")
    overdue_count += owner_inquiries.filter(first_contacted_at__isnull=True, first_response_due_at__lt=timezone.now()).count()
    overdue_count += owner_inquiries.filter(first_contacted_at__isnull=False, next_action_due_at__lt=timezone.now()).count()
    nav_item = active_navigation_item(request)
    nav_section = {
        "properties": "realty", "landings": "realty",
        "leads": "crm", "clients": "crm", "deals": "crm", "analytics": "crm",
        "telegram": "integrations",
    }.get(nav_item, "")
    return {
        "ai_enabled": settings.AI_ENABLED,
        "nav_profile": RealtorProfile.objects.filter(user=request.user).first(),
        "nav_agency_membership": membership,
        "nav_new_leads_count": new_leads_count,
        "nav_new_leads_label": "99+" if new_leads_count > 99 else str(new_leads_count),
        "nav_overdue_count": overdue_count,
        "nav_overdue_label": "99+" if overdue_count > 99 else str(overdue_count),
        "nav_active_item": nav_item,
        "nav_active_section": nav_section,
    }
