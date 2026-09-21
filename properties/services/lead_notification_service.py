import logging

import requests
from django.conf import settings
from django.core.mail import send_mail


logger = logging.getLogger(__name__)


class LeadNotificationService:
    """Deliver new public landing leads without affecting lead creation."""

    @classmethod
    def notify_new_lead(cls, lead, profile, lead_url):
        if not profile:
            return
        message = cls._message(lead, lead_url)
        cls._send_email(profile.email, lead, message)
        cls._send_telegram(profile.telegram_chat_id, lead, message)

    @staticmethod
    def _message(lead, lead_url):
        lines = [
            "Новая заявка с лендинга",
            f"Объект: {lead.property.title}",
            f"Клиент: {lead.name}",
            f"Телефон: {lead.phone}",
            f"Повод: {lead.get_contact_purpose_display()}",
        ]
        if lead.message:
            lines.append(f"Сообщение: {lead.message[:800]}")
        lines.extend(("", f"Открыть заявку: {lead_url}"))
        return "\n".join(lines)

    @classmethod
    def _send_email(cls, recipient, lead, message):
        if not (settings.EMAIL_NOTIFICATIONS_ENABLED and settings.DEFAULT_FROM_EMAIL and recipient):
            return
        try:
            send_mail(
                subject=f"Новая заявка: {lead.property.title}",
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Could not send email notification for lead %s", lead.pk)

    @classmethod
    def _send_telegram(cls, chat_id, lead, message):
        if not (settings.TELEGRAM_NOTIFICATIONS_ENABLED and settings.TELEGRAM_BOT_TOKEN and chat_id):
            return
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": message},
                timeout=8,
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                logger.warning("Telegram rejected notification for lead %s", lead.pk)
        except (requests.RequestException, ValueError):
            logger.exception("Could not send Telegram notification for lead %s", lead.pk)
