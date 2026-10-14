"""Suggest and safely merge duplicate contacts inside one CRM workspace."""

import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import (
    Client, ClientInteraction, ClientMergeRecord, ClientPropertySuggestion,
    ClientReminder, Deal, Lead, OwnerInquiry, PropertyOwnerLink,
)


class ClientMergeConflict(ValueError):
    pass


def _phone_key(phone):
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits[0] in "78":
        return digits[1:]
    return digits


def possible_matches(client, queryset):
    """Only surface plausible matches; a person must still approve the merge."""
    key = _phone_key(client.phone)
    candidate_q = Q(name__iexact=client.name.strip())
    if len(key) >= 7:
        candidate_q |= Q(phone__endswith=key[-4:])
    candidates = queryset.exclude(pk=client.pk).filter(
        agency_id=client.agency_id,
    ).filter(candidate_q).order_by("name", "pk")[:80]
    result = []
    for other in candidates:
        other_key = _phone_key(other.phone)
        same_phone = bool(key and key == other_key)
        same_name = bool(client.name.strip() and client.name.strip().casefold() == other.name.strip().casefold())
        if same_phone or same_name:
            result.append({"client": other, "reason": "Тот же телефон" if same_phone else "Совпадает имя"})
    return result


def merge_conflicts(target, source):
    if target.pk == source.pk or target.agency_id != source.agency_id:
        return ["Контакты должны быть разными и находиться в одном пространстве."]
    if not target.agency_id and target.owner_id != source.owner_id:
        return ["Личные контакты должны принадлежать одному пользователю."]
    if target.is_demo != source.is_demo:
        return ["Демонстрационный и рабочий контакты нельзя объединять."]
    conflicts = []
    owner_properties = set(PropertyOwnerLink.objects.filter(client=target).values_list("property_id", flat=True))
    if PropertyOwnerLink.objects.filter(client=source, property_id__in=owner_properties).exists():
        conflicts.append("Оба контакта указаны собственниками одного объекта.")
    suggestions = set(ClientPropertySuggestion.objects.filter(client=target).values_list("property_id", flat=True))
    if ClientPropertySuggestion.objects.filter(client=source, property_id__in=suggestions).exists():
        conflicts.append("Один объект уже предложен обоим контактам.")
    active_properties = set(Deal.objects.filter(client=target, stage__in=Deal.ACTIVE_STAGES).values_list("property_id", flat=True))
    if Deal.objects.filter(client=source, stage__in=Deal.ACTIVE_STAGES, property_id__in=active_properties).exists():
        conflicts.append("У обоих контактов есть активная сделка по одному объекту.")
    feedback_showings = set(ClientReminder.objects.filter(
        client=target, owner_showing__isnull=False,
    ).values_list("owner_showing_id", flat=True))
    if ClientReminder.objects.filter(client=source, owner_showing_id__in=feedback_showings).exists():
        conflicts.append("Есть повторная задача обратной связи по одному показу.")
    return conflicts


@transaction.atomic
def merge_clients(*, target_id, source_id, actor):
    """Move every client relation, or leave both records unchanged on conflict."""
    locked = list(Client.objects.select_for_update().filter(pk__in=[target_id, source_id]).order_by("pk"))
    if len(locked) != 2 or target_id == source_id:
        raise ClientMergeConflict("Выберите два разных существующих контакта.")
    by_id = {item.pk: item for item in locked}
    target, source = by_id[target_id], by_id[source_id]
    conflicts = merge_conflicts(target, source)
    if conflicts:
        raise ClientMergeConflict(" ".join(conflicts))

    relations = {
        "заявки": Lead,
        "обращения собственников": OwnerInquiry,
        "задачи": ClientReminder,
        "сделки": Deal,
        "история": ClientInteraction,
        "собственность": PropertyOwnerLink,
        "предложения": ClientPropertySuggestion,
    }
    counts = {label: model.objects.filter(client=source).count() for label, model in relations.items()}
    snapshot = {
        "source": source.source, "role": source.contact_role, "status": source.status,
        "notes": source.notes, "search_notes": source.search_notes,
        "created_at": source.created_at.isoformat(),
    }
    # Bulk updates deliberately avoid per-record side effects: the workspace and
    # linked deal/lead stay identical, while every relationship changes together.
    for model in relations.values():
        model.objects.filter(client=source).update(client=target)
    ClientMergeRecord.objects.filter(target=source).update(target=target)

    for field in (
        "preferred_contact_time", "preferred_area", "budget", "search_deal_type", "search_segment",
        "search_property_type", "search_rooms_min", "search_area_min", "search_area_max",
        "search_commercial_purpose", "search_currency", "search_notes", "outcome_reason",
    ):
        if not getattr(target, field) and getattr(source, field):
            setattr(target, field, getattr(source, field))
    if source.notes.strip():
        suffix = f"Из контакта «{source.name}» ({source.phone}): {source.notes.strip()}"
        target.notes = f"{target.notes.strip()}\n\n{suffix}".strip()
    if target.contact_role != source.contact_role:
        target.contact_role = "both"
    if source.first_contacted_at and (not target.first_contacted_at or source.first_contacted_at < target.first_contacted_at):
        target.first_contacted_at = source.first_contacted_at
    target.updated_at = timezone.now()
    target.save()

    ClientMergeRecord.objects.create(
        target=target, source_id=source.pk, source_name=source.name, source_phone=source.phone,
        source_snapshot=snapshot, moved_counts=counts, merged_by=actor,
    )
    source.delete()
    return target, counts
