import logging
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import send_mail
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from ..models import AccountEmailRequest, RealtorProfile


logger = logging.getLogger(__name__)


class AccountTokenGenerator(PasswordResetTokenGenerator):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.key_salt = f"realtor-ai.account.{kind}"

    def _make_hash_value(self, user, timestamp):
        profile = RealtorProfile.objects.filter(user=user).first()
        state = f"{profile.email}:{profile.email_verification_nonce}:{profile.email_verified_at}" if profile else ""
        return super()._make_hash_value(user, timestamp) + state

    def check_token(self, user, token):
        if not user or not user.is_active or not user.has_usable_password():
            return False
        profile = RealtorProfile.objects.filter(user=user).first()
        if not profile or not profile.email or bool(profile.email_verified_at) != (self.kind == "reset"):
            return False
        return super().check_token(user, token)


email_verification_token = AccountTokenGenerator("verify")
password_recovery_token = AccountTokenGenerator("reset")


def account_base_url():
    # Never derive security links from an incoming Host / forwarded header.
    value = settings.ACCOUNT_PUBLIC_BASE_URL
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ImproperlyConfigured("Set ACCOUNT_PUBLIC_BASE_URL to the canonical site origin.")
    if parsed.scheme != "https" and not settings.DEBUG:
        raise ImproperlyConfigured("Account emails require HTTPS in production.")
    return value.rstrip("/")


def enqueue_account_email(user, kind):
    profile = RealtorProfile.objects.filter(user=user).first() if user else None
    return AccountEmailRequest.objects.create(
        user=user, kind=kind, nonce=profile.email_verification_nonce if profile else None,
        auth_hash=user.get_session_auth_hash() if user else "",
    )


def send_pending_account_emails(*, limit=100):
    base_url = account_base_url()
    if not settings.DEFAULT_FROM_EMAIL:
        raise ImproperlyConfigured("Set DEFAULT_FROM_EMAIL for account emails.")
    counts = {"sent": 0, "discarded": 0, "failed": 0}
    ids = AccountEmailRequest.objects.filter(status="pending").order_by("created_at").values_list("pk", flat=True)[:limit]
    for pk in list(ids):
        with transaction.atomic():
            lock_options = {"skip_locked": True} if connection.features.has_select_for_update_skip_locked else {}
            item = AccountEmailRequest.objects.select_for_update(**lock_options).filter(pk=pk, status="pending").first()
            if not item:
                continue
            now = timezone.now()
            if item.last_attempt_at and now - item.last_attempt_at < timedelta(minutes=2):
                continue
            user = item.user
            # Keep the recipient and token state consistent if the email changes
            # concurrently. Otherwise a token for the new email could be mailed
            # to the previous address between validation and token generation.
            profile = RealtorProfile.objects.select_for_update().filter(user=user).first() if user else None
            valid_state = (
                user and user.is_active and user.has_usable_password() and profile and profile.email
                and profile.email_verification_nonce == item.nonce
                and constant_time_compare(item.auth_hash, user.get_session_auth_hash())
                and bool(profile.email_verified_at) == (item.kind == "reset")
            )
            if not valid_state or now - item.created_at > timedelta(seconds=settings.ACCOUNT_EMAIL_QUEUE_TTL):
                item.status = "discarded"
                item.completed_at = now
                counts["discarded"] += 1
            else:
                uid = urlsafe_base64_encode(force_bytes(user.pk))
                generator = password_recovery_token if item.kind == "reset" else email_verification_token
                route = "password_reset_confirm" if item.kind == "reset" else "account_email_confirm"
                link = base_url + reverse(route, kwargs={"uidb64": uid, "token": generator.make_token(user)})
                subject = "Восстановление доступа — RealtorAI" if item.kind == "reset" else "Подтвердите email — RealtorAI"
                message = f"{subject}\n\nОткройте ссылку:\n{link}\n\nСсылка действует 1 час. Если вы не запрашивали это письмо, просто проигнорируйте его."
                item.attempts += 1
                item.last_attempt_at = now
                try:
                    delivered = send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [profile.email], fail_silently=False)
                    if not delivered:
                        raise RuntimeError("Mail backend did not accept the message")
                except Exception as error:
                    logger.error("Account email delivery failed for queue item %s (%s)", item.pk, type(error).__name__)
                    if item.attempts >= 3:
                        item.status = "failed"
                        item.completed_at = now
                    counts["failed"] += 1
                else:
                    item.status = "sent"
                    item.completed_at = now
                    counts["sent"] += 1
            item.save(update_fields=["status", "attempts", "last_attempt_at", "completed_at"])
    return counts
