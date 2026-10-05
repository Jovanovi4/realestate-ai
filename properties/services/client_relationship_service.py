from django.db import transaction

from ..models import Client, ClientInteraction


def activate_client_relationship(client_id, *, reason, lead=None):
    """A real contact or new opportunity starts active work with the client."""
    with transaction.atomic():
        client = Client.objects.select_for_update().get(pk=client_id)
        if client.status == "in_progress":
            return False
        previous = client.get_status_display()
        client.status = "in_progress"
        client.save(update_fields=["status", "updated_at"])
        ClientInteraction.objects.create(
            client=client,
            lead=lead,
            interaction_type="status",
            text=f"Отношения с клиентом: {previous} → В работе. {reason}",
        )
        return True
