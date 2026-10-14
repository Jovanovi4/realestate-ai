"""Read-only closure preview and the one-way transition to a retained archive."""

from django.db.models import Sum
from django.utils import timezone

from billing.models import BillingAccount, StoredFile
from billing.presentation import subscription_summary

from ..models import (
    AgencyInvitation, AgencyMembership, AuditEvent, Client, ClientReminder, Deal,
    Lead, OutboundMessage, OwnerInquiry, Property, PropertyImage, Showing,
    TelegramConversation, TelegramDelivery, TelegramFeed, TelegramSubscriber,
)
from .staff_display import staff_display_name


def closure_snapshot(agency):
    """Values shown to the owner and signed into the confirmation token."""
    members = list(AgencyMembership.objects.filter(agency=agency).select_related("user", "user__realtor_profile")
                   .order_by("joined_at", "pk"))
    pending = AgencyInvitation.objects.filter(
        agency=agency, accepted_by__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now(),
    )
    account = BillingAccount.objects.filter(agency=agency).first()
    files = StoredFile.objects.filter(account=account) if account else StoredFile.objects.none()
    file_total = files.aggregate(bytes=Sum("size_bytes"))["bytes"] or 0
    plan = subscription_summary(account) if account else None
    feed = TelegramFeed.objects.filter(agency=agency).first()
    return {
        "agency_name": agency.name,
        "member_list": [{"id": item.user_id, "name": staff_display_name(item.user), "role": item.get_role_display()} for item in members],
        "members": len(members),
        "pending_invitation_ids": list(pending.order_by("pk").values_list("pk", flat=True)),
        "invitations": pending.count(),
        "properties": Property.objects.filter(agency=agency).count(),
        "clients": Client.objects.filter(agency=agency).count(),
        "leads": Lead.objects.filter(agency=agency).count(),
        "deals": Deal.objects.filter(agency=agency).count(),
        "owner_inquiries": OwnerInquiry.objects.filter(agency=agency).count(),
        "tasks": ClientReminder.objects.filter(agency=agency).count(),
        "showings": Showing.objects.filter(agency=agency).count(),
        "images": PropertyImage.objects.filter(property__agency=agency).count(),
        "files": files.count(),
        "file_bytes": file_total,
        "unknown_file_sizes": files.filter(size_bytes__isnull=True).count(),
        "landings": Property.objects.filter(agency=agency, landing_published=True).count(),
        "telegram_listings": Property.objects.filter(agency=agency, telegram_published=True).count(),
        "telegram_subscribers": TelegramSubscriber.objects.filter(feed=feed, active=True).count() if feed else 0,
        "billing_account_id": account.pk if account else None,
        "plan": plan["plan_name"] if plan else "Счёт не создан",
        "plan_state": plan["state_label"] if plan else "—",
    }


def close_agency(agency, actor, snapshot):
    """Call inside a transaction while holding a row lock on the agency."""
    now = timezone.now()
    for invite in AgencyInvitation.objects.filter(pk__in=snapshot["pending_invitation_ids"]).iterator():
        invite.revoked_at = now
        invite.save(update_fields=["revoked_at"])
    agency.closed_at = now
    agency.closed_by = actor
    agency.closure_snapshot = snapshot
    agency.save(update_fields=["closed_at", "closed_by", "closure_snapshot"])

    # Keep subscribers and publications for historical reporting. Both the
    # worker and the webhook check the closed flag before exposing a listing.
    TelegramDelivery.objects.filter(
        publication__feed__agency=agency, sent_at__isnull=True, attempts__lt=5,
    ).update(attempts=5, last_error="Команда закрыта")
    TelegramConversation.objects.filter(pending_property__agency=agency).update(
        pending_property=None, pending_phone="", pending_at=None,
    )
    feed = TelegramFeed.objects.filter(agency=agency).first()
    if feed:
        OutboundMessage.objects.filter(
            channel=OutboundMessage.Channel.PUBLIC_TELEGRAM,
            status__in=[OutboundMessage.Status.PENDING, OutboundMessage.Status.SENDING],
            payload__method="listing", payload__data__feed_id=feed.pk,
        ).update(status=OutboundMessage.Status.CANCELLED, payload={}, lease_until=None, claim_token=None,
                 last_error="Команда закрыта")

    # Release the one active membership slot per user. The signed snapshot and
    # AuditEvent rows retain the former roster without exposing shared CRM.
    for membership in AgencyMembership.objects.filter(agency=agency).iterator():
        membership.delete()
    AuditEvent.objects.create(
        agency=agency, actor=actor, model_name="Команда", object_pk=str(agency.pk),
        action="updated", changed_fields=["Закрыта"],
    )
