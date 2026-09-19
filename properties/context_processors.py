from django.conf import settings

from .models import Lead, RealtorProfile


def realtor_profile(request):
    """Expose the current user's profile to the global navigation safely."""
    if not request.user.is_authenticated:
        return {"ai_enabled": settings.AI_ENABLED}
    new_leads_count = Lead.objects.filter(property__owner=request.user, status="new").count()
    return {
        "ai_enabled": settings.AI_ENABLED,
        "nav_profile": RealtorProfile.objects.filter(user=request.user).first(),
        "nav_new_leads_count": new_leads_count,
        "nav_new_leads_label": "99+" if new_leads_count > 99 else str(new_leads_count),
    }
