from django.core.exceptions import ValidationError

from ..models import ClientInteraction, ClientReminder, Deal


def convert_lead_to_deal(lead, deal):
    """Finish an open lead and carry its pending action into the deal.

    The caller locks the lead and wraps this operation in a transaction.
    """
    if lead.status == "archived" or lead.outcome != "open":
        raise ValidationError("Заявка уже завершена или находится в архиве.")
    if lead.qualification != "qualified" or not lead.first_contacted_at:
        raise ValidationError("Сначала зафиксируйте первый контакт и квалифицируйте заявку.")
    if lead.client_id != deal.client_id or lead.property_id != deal.property_id:
        raise ValidationError("Сделка должна относиться к клиенту и объекту заявки.")

    source_task = lead.action_tasks.select_for_update().filter(is_done=False).first()
    lead.outcome = "converted"
    lead.outcome_deal = deal
    lead.save(update_fields=["outcome", "outcome_deal", "updated_at"])
    if source_task and deal.stage in Deal.ACTIVE_STAGES:
        ClientReminder.objects.create(
            owner=deal.responsible or deal.owner,
            agency=deal.agency,
            client=deal.client,
            deal=deal,
            text=source_task.text,
            due_at=source_task.due_at,
        )
    ClientInteraction.objects.create(
        client=deal.client,
        lead=lead,
        interaction_type="status",
        text=f"Заявка переведена в сделку по объекту «{deal.property.title}».",
    )
