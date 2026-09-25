from django.conf import settings
from django.utils import timezone

from .models import ClientReminder, Lead, RealtorProfile, Showing
from .agency_access import membership_for, workspace_queryset


def realtor_profile(request):
    """Expose the current user's profile to the global navigation safely."""
    if not request.user.is_authenticated:
        return {"ai_enabled": settings.AI_ENABLED}
    membership = membership_for(request.user)
    new_leads = workspace_queryset(Lead.objects, request.user, prefix="property__").filter(status="new")
    if membership and membership.role == "agent":
        new_leads = new_leads.filter(assigned_to=request.user)
    new_leads_count = new_leads.count()
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    overdue_count = ClientReminder.objects.filter(owner=request.user, is_done=False, due_at__lt=timezone.now()).count()
    overdue_count += Showing.objects.filter(owner=request.user, status="planned", starts_at__lt=today_start).count()
    return {
        "ai_enabled": settings.AI_ENABLED,
        "nav_profile": RealtorProfile.objects.filter(user=request.user).first(),
        "nav_agency_membership": membership,
        "nav_new_leads_count": new_leads_count,
        "nav_new_leads_label": "99+" if new_leads_count > 99 else str(new_leads_count),
        "nav_overdue_count": overdue_count,
        "nav_overdue_label": "99+" if overdue_count > 99 else str(overdue_count),
    }
