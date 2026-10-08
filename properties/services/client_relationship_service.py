from django.db import transaction

from ..models import Client, ClientInteraction, Deal


def relationship_change_error(client, status):
    if status == "new" and client.first_contacted_at:
        return "Первый контакт уже записан. Используйте состояние «В работе»."
    if status in {"new", "nurture", "inactive"} and client.deals.filter(stage__in=Deal.ACTIVE_STAGES).exists():
        return "У клиента есть активная сделка. Завершите её или оставьте отношения «В работе»."
    if status in {"nurture", "inactive"} and client.leads.filter(outcome="open").exclude(status="archived").exists():
        return "У клиента есть открытая заявка. Завершите её или оставьте отношения «В работе»."
    return None


def change_client_relationship(client, status, *, reason="", lead=None):
    if client.status == status:
        return False
    previous = client.get_status_display()
    client.status = status
    client.save(update_fields=["status", "updated_at"])
    ClientInteraction.objects.create(
        client=client,
        lead=lead,
        interaction_type="status",
        text=f"Отношения с клиентом: {previous} → {client.get_status_display()}." + (f" {reason}" if reason else ""),
    )
    return True


def activate_client_relationship(client_id, *, reason, lead=None):
    """A real contact or new opportunity starts active work with the client."""
    with transaction.atomic():
        client = Client.objects.select_for_update().get(pk=client_id)
        if client.status == "in_progress":
            return False
        return change_client_relationship(client, "in_progress", reason=reason, lead=lead)
