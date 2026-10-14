from django.db import transaction

from .outbound_message_service import deliver_messages_safely, enqueue_lead_notifications


class LeadNotificationService:
    """Persist notifications with their lead before attempting delivery."""

    @classmethod
    def notify_new_lead(cls, lead, profile, lead_url):
        if not profile:
            return
        message = cls._message(lead, lead_url)
        ids = enqueue_lead_notifications(lead, profile, message)
        if ids:
            transaction.on_commit(lambda ids=tuple(ids): deliver_messages_safely(ids))

    @staticmethod
    def _message(lead, lead_url):
        lines = [
            "Новая заявка с лендинга" if lead.source == "landing" else f"Новая заявка: {lead.get_source_display()}",
            f"Запрос: {lead.property.title if lead.property_id else 'Подбор недвижимости'}",
            f"Клиент: {lead.name}",
            f"Телефон: {lead.phone}",
            f"Повод: {lead.get_contact_purpose_display() if lead.property_id else 'Подбор недвижимости'}",
        ]
        if lead.message:
            lines.append(f"Сообщение: {lead.message[:800]}")
        lines.extend(("", f"Открыть заявку: {lead_url}"))
        return "\n".join(lines)
