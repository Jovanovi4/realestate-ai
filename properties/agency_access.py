from django.db.models import Q

from .models import AgencyMembership


def membership_for(user):
    if not user.is_authenticated:
        return None
    return AgencyMembership.objects.select_related("agency").filter(user=user).first()


def agency_for(user):
    membership = membership_for(user)
    return membership.agency if membership else None


def workspace_filter(user, *, prefix=""):
    """Include the team's shared data and the user's unshared legacy/private data."""
    agency = agency_for(user)
    owner_key = f"{prefix}owner"
    agency_key = f"{prefix}agency"
    private = Q(**{owner_key: user, f"{agency_key}__isnull": True})
    return private | Q(**{agency_key: agency}) if agency else private


def workspace_queryset(queryset, user, *, prefix=""):
    return queryset.filter(workspace_filter(user, prefix=prefix))


def may_manage_agency(user):
    membership = membership_for(user)
    return bool(membership and membership.role in {"owner", "manager"})


def deletable_queryset(queryset, user, *, prefix=""):
    visible = workspace_queryset(queryset, user, prefix=prefix)
    if may_manage_agency(user):
        return visible
    return visible.filter(**{f"{prefix}agency__isnull": True})
