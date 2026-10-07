"""Transactional outbox for buyer bot replies and new-lead notifications.

The provider may accept a request just before our process dies. Such an outcome
cannot be distinguished from a failed request, so retries provide at-least-once
delivery. Incoming update and lead deduplication remain separate safeguards.
"""

import logging
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from properties.models import OutboundMessage, Property, TelegramFeed, TelegramSubscriber


logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
LEASE_SECONDS = 120
RETRY_SECONDS = (10, 30, 120, 600)


class DeliveryCancelled(Exception):
    """A queued message is no longer valid and must not be retried."""


def enqueue_public_actions(update_id, actions):
    """Call inside the same transaction as the incoming Telegram update."""
    ids = []
    for index, (method, payload) in enumerate(actions):
        expiry = timezone.now() + timedelta(seconds=30) if method == "answerCallbackQuery" else None
        message, _ = OutboundMessage.objects.get_or_create(
            dedupe_key=f"public-update:{update_id}:{index}",
            defaults={
                "channel": OutboundMessage.Channel.PUBLIC_TELEGRAM,
                "payload": {"method": method, "data": payload},
                "expires_at": expiry,
            },
        )
        ids.append(message.pk)
    return ids


def enqueue_lead_notifications(lead, profile, text):
    """Queue channel-specific snapshots before the lead transaction commits."""
    if not profile:
        return []
    ids = []
    channels = []
    if settings.EMAIL_NOTIFICATIONS_ENABLED and settings.DEFAULT_FROM_EMAIL and profile.email:
        channels.append((OutboundMessage.Channel.EMAIL, {
            "recipient": profile.email,
            "subject": f"Новая заявка: {lead.property.title}",
            "text": text,
        }))
    if settings.TELEGRAM_NOTIFICATIONS_ENABLED and settings.TELEGRAM_BOT_TOKEN and profile.telegram_chat_id:
        channels.append((OutboundMessage.Channel.STAFF_TELEGRAM, {
            "chat_id": profile.telegram_chat_id,
            "text": text,
        }))
    for channel, payload in channels:
        message, _ = OutboundMessage.objects.get_or_create(
            dedupe_key=f"lead:{lead.pk}:{channel}",
            defaults={"channel": channel, "payload": payload},
        )
        ids.append(message.pk)
    return ids


def _claim(message_id):
    now = timezone.now()
    with transaction.atomic():
        message = OutboundMessage.objects.select_for_update().filter(pk=message_id).first()
        if not message:
            return None
        eligible = (
            (message.status == OutboundMessage.Status.PENDING and message.next_attempt_at <= now)
            or (message.status == OutboundMessage.Status.SENDING and message.lease_until and message.lease_until <= now)
        )
        if not eligible:
            return None
        if message.expires_at and message.expires_at <= now:
            message.status = OutboundMessage.Status.CANCELLED
            message.last_error = "Время действия ответа истекло"
            message.lease_until = None
            message.claim_token = None
            message.payload = {}
            message.save(update_fields=["status", "last_error", "lease_until", "claim_token", "payload"])
            return None
        if message.attempts >= MAX_ATTEMPTS:
            message.status = OutboundMessage.Status.FAILED
            message.last_error = "Нет подтверждения предыдущей отправки"
            message.lease_until = None
            message.claim_token = None
            message.save(update_fields=["status", "last_error", "lease_until", "claim_token"])
            return None
        old_status, old_attempts, old_claim = message.status, message.attempts, message.claim_token
        claim = uuid.uuid4()
        claimed = OutboundMessage.objects.filter(
            pk=message.pk, status=old_status, attempts=old_attempts, claim_token=old_claim,
        ).update(status=OutboundMessage.Status.SENDING, attempts=old_attempts + 1,
                 lease_until=now + timedelta(seconds=LEASE_SECONDS), claim_token=claim)
        if not claimed:
            return None
        message.status = OutboundMessage.Status.SENDING
        message.attempts += 1
        message.claim_token = claim
        return message


def _send_public(message):
    from .telegram_public_service import _api, feed_matches_property, is_visible, send_listing

    if not settings.TELEGRAM_PUBLIC_ENABLED or not settings.TELEGRAM_PUBLIC_BOT_TOKEN:
        raise RuntimeError("public_bot_disabled")
    method = message.payload["method"]
    data = message.payload["data"]
    if method == "listing":
        property = Property.objects.filter(pk=data["property_id"]).first()
        feed = TelegramFeed.objects.filter(pk=data["feed_id"]).first()
        if not (property and feed and is_visible(property) and feed_matches_property(feed, property)
                and TelegramSubscriber.objects.filter(feed=feed, chat_id=data["chat_id"], active=True).exists()):
            raise DeliveryCancelled("Объявление или подписка уже неактуальны")
        if not send_listing(data["chat_id"], property):
            raise DeliveryCancelled("Объявление больше недоступно")
    elif method in {"sendMessage", "answerCallbackQuery"}:
        _api(method, data)
    else:
        raise DeliveryCancelled("Неизвестное действие бота")


def _send_staff_telegram(message):
    if not settings.TELEGRAM_NOTIFICATIONS_ENABLED or not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("staff_bot_disabled")
    response = requests.post(
        f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
        data=message.payload, timeout=8,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("telegram_rejected")


def _send_email(message):
    if not settings.EMAIL_NOTIFICATIONS_ENABLED or not settings.DEFAULT_FROM_EMAIL:
        raise RuntimeError("email_disabled")
    delivered = send_mail(
        subject=message.payload["subject"], message=message.payload["text"],
        from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[message.payload["recipient"]],
        fail_silently=False,
    )
    if delivered != 1:
        raise RuntimeError("email_not_accepted")


def _safe_error(error):
    # requests exceptions can contain /bot<TOKEN>/ in their URL. Never persist
    # or log their string representation, response body, recipient or payload.
    response = getattr(error, "response", None)
    code = f" HTTP {response.status_code}" if response is not None and response.status_code else ""
    if isinstance(error, RuntimeError) and str(error) in {
        "public_bot_disabled", "staff_bot_disabled", "email_disabled",
        "telegram_rejected", "email_not_accepted",
    }:
        return str(error)
    return f"{type(error).__name__}{code}"[:160]


def deliver_message(message_id):
    """Try once; a worker can retry pending or expired leased messages later."""
    message = _claim(message_id)
    if message is None:
        return False
    try:
        if message.channel == OutboundMessage.Channel.PUBLIC_TELEGRAM:
            _send_public(message)
        elif message.channel == OutboundMessage.Channel.STAFF_TELEGRAM:
            _send_staff_telegram(message)
        elif message.channel == OutboundMessage.Channel.EMAIL:
            _send_email(message)
        else:
            raise DeliveryCancelled("Неизвестный канал доставки")
    except DeliveryCancelled as error:
        OutboundMessage.objects.filter(pk=message.pk, claim_token=message.claim_token,
            status=OutboundMessage.Status.SENDING).update(
                status=OutboundMessage.Status.CANCELLED, last_error=str(error)[:160],
                lease_until=None, claim_token=None, payload={},
            )
        return False
    except Exception as error:
        now = timezone.now()
        failed = message.attempts >= MAX_ATTEMPTS
        retry_after = RETRY_SECONDS[min(message.attempts - 1, len(RETRY_SECONDS) - 1)]
        OutboundMessage.objects.filter(pk=message.pk, claim_token=message.claim_token,
            status=OutboundMessage.Status.SENDING).update(
                status=OutboundMessage.Status.FAILED if failed else OutboundMessage.Status.PENDING,
                next_attempt_at=now + timedelta(seconds=retry_after), last_error=_safe_error(error),
                lease_until=None, claim_token=None,
            )
        logger.warning("Outbound delivery %s failed (%s)", message.pk, _safe_error(error))
        return False
    OutboundMessage.objects.filter(pk=message.pk, claim_token=message.claim_token,
        status=OutboundMessage.Status.SENDING).update(
            status=OutboundMessage.Status.SENT, sent_at=timezone.now(), last_error="",
            lease_until=None, claim_token=None, payload={},
        )
    return True


def deliver_messages(message_ids):
    return sum(deliver_message(message_id) for message_id in message_ids)


def deliver_messages_safely(message_ids):
    """Never turn an already-committed lead or update into a client HTTP error."""
    try:
        return deliver_messages(message_ids)
    except Exception as error:
        logger.error("Immediate outbox delivery deferred (%s)", type(error).__name__)
        return 0


def due_message_ids(limit=50):
    now = timezone.now()
    return list(OutboundMessage.objects.filter(
        Q(status=OutboundMessage.Status.PENDING, next_attempt_at__lte=now)
        | Q(status=OutboundMessage.Status.SENDING, lease_until__lte=now),
    ).order_by("next_attempt_at", "pk").values_list("pk", flat=True)[:limit])
