"""Idempotent creation of CRM requests from an identified workspace channel."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from billing.enforcement import require_new_action
from billing.rules import Operation

from ..models import Client, InboundEvent, Lead, OwnerInquiry, PublicIntakeLink


def _contact_for(link, *, name, phone, source, role):
    responsible = link.responsible
    lookup = {"agency": link.agency, "phone": phone}
    if not link.agency_id:
        lookup["owner"] = responsible
    client, created = Client.objects.get_or_create(
        **lookup,
        defaults={"owner": responsible, "name": name, "source": source, "contact_role": role},
    )
    if not created and client.contact_role != role and client.contact_role != "both":
        client.contact_role = "both"
        client.save(update_fields=["contact_role", "updated_at"])
    return client


def ingest_search(link, *, channel, external_id, name, phone, message, interest_type, source_detail="", consent=False,
                  received_at=None, assigned_to=None):
    """Return (lead, created); a repeated external ID returns the saved lead."""
    with transaction.atomic():
        link = PublicIntakeLink.objects.select_for_update().get(pk=link.pk)
        if not link.enabled or (link.agency_id and link.agency.closed_at):
            raise ValueError("Публичный приём обращений отключён.")
        responsible = link.responsible
        require_new_action(responsible, Operation.EDIT_CRM, agency=link.agency)
        event, _ = InboundEvent.objects.select_for_update().get_or_create(
            link=link, channel=channel, external_id=external_id,
        )
        if event.ignored_at:
            raise ValueError("Входящее сообщение отклонено.")
        if event.owner_inquiry_id:
            raise ValueError("Сообщение уже связано с обращением собственника.")
        if event.lead_id:
            return event.lead, False
        client = _contact_for(link, name=name, phone=phone, source=channel, role="seeker")
        if not client.search_deal_type:
            client.search_deal_type = "rent" if interest_type == "long_rent" else "sale"
            client.save(update_fields=["search_deal_type", "updated_at"])
        due_at = (received_at or timezone.now()) + timedelta(minutes=30)
        lead = Lead.objects.create(
            owner=responsible, agency=link.agency, assigned_to=assigned_to or responsible, client=client,
            name=name, phone=phone, message=message, interest_type=interest_type,
            source=channel, source_detail=source_detail[:255],
            next_action="Связаться с клиентом", next_action_due_at=due_at,
            personal_data_consent_at=timezone.now() if consent else None,
            personal_data_consent_version=settings.PERSONAL_DATA_CONSENT_VERSION if consent else "",
        )
        if received_at:
            Lead.objects.filter(pk=lead.pk).update(created_at=received_at)
            lead.created_at = received_at
        event.lead = lead
        event.save(update_fields=["lead"])
        return lead, True


def ingest_owner(link, *, channel, external_id, name, phone, deal_type, address, description,
                 expected_price=None, source_detail="", consent=False, received_at=None, assigned_to=None):
    """Return (inquiry, created); one delivery cannot create two inquiries."""
    with transaction.atomic():
        link = PublicIntakeLink.objects.select_for_update().get(pk=link.pk)
        if not link.enabled or (link.agency_id and link.agency.closed_at):
            raise ValueError("Публичный приём обращений отключён.")
        responsible = link.responsible
        require_new_action(responsible, Operation.EDIT_CRM, agency=link.agency)
        event, _ = InboundEvent.objects.select_for_update().get_or_create(
            link=link, channel=channel, external_id=external_id,
        )
        if event.ignored_at:
            raise ValueError("Входящее сообщение отклонено.")
        if event.lead_id:
            raise ValueError("Сообщение уже связано с заявкой.")
        if event.owner_inquiry_id:
            return event.owner_inquiry, False
        client = _contact_for(link, name=name, phone=phone, source=channel, role="owner")
        due_at = (received_at or timezone.now()) + timedelta(minutes=30)
        inquiry = OwnerInquiry.objects.create(
            owner=responsible, agency=link.agency, client=client, assigned_to=assigned_to or responsible,
            deal_type=deal_type, address=address, description=description,
            expected_price=expected_price, currency="RUB", source=channel,
            source_detail=source_detail[:255], first_response_due_at=due_at,
            next_action="Связаться с собственником", next_action_due_at=due_at,
            personal_data_consent_at=timezone.now() if consent else None,
            personal_data_consent_version=settings.PERSONAL_DATA_CONSENT_VERSION if consent else "",
        )
        if received_at:
            OwnerInquiry.objects.filter(pk=inquiry.pk).update(created_at=received_at)
            inquiry.created_at = received_at
        event.owner_inquiry = inquiry
        event.save(update_fields=["owner_inquiry"])
        return inquiry, True
