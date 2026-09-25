import logging

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from ..models import ClientReminder, RealtorProfile


logger = logging.getLogger(__name__)


class OverdueTaskNotificationService:
    """Send one digest for overdue tasks and mark it only after successful delivery."""

    @classmethod
    def send_for_user(cls, user, tasks):
        tasks = list(tasks)
        if not tasks:
            return False
        profile = RealtorProfile.objects.filter(user=user).first()
        if not profile:
            return False

        lines = ["Просроченные задачи RealtorAI:"]
        for task in tasks:
            client_name = f" · {task.client.name[:40]}" if task.client else ""
            lines.append(f"• {timezone.localtime(task.due_at):%d.%m %H:%M} — {task.text[:100]}{client_name}")
        message = "\n".join(lines)
        email_delivered = cls._send_email(profile.email, message)
        telegram_delivered = cls._send_telegram(profile.telegram_chat_id, message)
        delivered = email_delivered or telegram_delivered
        if delivered:
            ClientReminder.objects.filter(
                pk__in=[task.pk for task in tasks],
                owner=user,
                is_done=False,
                due_at__lt=timezone.now(),
                overdue_notified_at__isnull=True,
            ).update(overdue_notified_at=timezone.now())
        return delivered

    @staticmethod
    def _send_email(recipient, message):
        if not (settings.EMAIL_NOTIFICATIONS_ENABLED and settings.DEFAULT_FROM_EMAIL and recipient):
            return False
        try:
            return send_mail(
                subject="Просроченные задачи RealtorAI",
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                fail_silently=False,
            ) == 1
        except Exception:
            logger.exception("Could not send overdue task email")
            return False

    @staticmethod
    def _send_telegram(chat_id, message):
        if not (settings.TELEGRAM_NOTIFICATIONS_ENABLED and settings.TELEGRAM_BOT_TOKEN and chat_id):
            return False
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": message},
                timeout=8,
            )
            response.raise_for_status()
            return bool(response.json().get("ok"))
        except (requests.RequestException, ValueError):
            logger.exception("Could not send overdue task Telegram message")
            return False
