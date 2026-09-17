from .models import RealtorProfile


def realtor_profile(request):
    """Expose the current user's profile to the global navigation safely."""
    if not request.user.is_authenticated:
        return {}
    return {"nav_profile": RealtorProfile.objects.filter(user=request.user).first()}
