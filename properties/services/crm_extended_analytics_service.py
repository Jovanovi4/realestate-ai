"""Period reports with explicit cohorts and forward-only event boundaries."""

from collections import Counter
from datetime import timedelta

from django.db.models import Count, Q, Sum

from ..models import LandingDailyView, Lead, OwnerInquiry, Property, TelegramDelivery
from .crm_analytics_service import (
    AnalyticsPeriod, _real_deals, _real_leads, _real_showings, _real_tasks, build_report,
)


def _real_properties(scope):
    records = Property.objects.filter(is_demo=False)
    if scope.agency_id:
        records = records.filter(agency_id=scope.agency_id)
        if scope.member_id:
            records = records.filter(
                Q(owner_id=scope.member_id)
                | Q(pk__in=_real_leads(scope).values("property_id"))
                | Q(pk__in=_real_showings(scope).values("deal__property_id"))
            )
    else:
        records = records.filter(agency__isnull=True, owner_id=scope.user_id)
    return records


def _real_owner_inquiries(scope):
    records = OwnerInquiry.objects.filter(client__is_demo=False).filter(
        Q(property__isnull=True) | Q(property__is_demo=False),
    )
    if scope.agency_id:
        records = records.filter(agency_id=scope.agency_id)
        if scope.member_id:
            records = records.filter(assigned_to_id=scope.member_id)
    else:
        records = records.filter(agency__isnull=True, owner_id=scope.user_id)
    return records


def _counts(queryset, field):
    return dict(queryset.values(field).annotate(total=Count("pk")).values_list(field, "total"))


def build_extended_report(scope, period, *, base_report=None):
    """Objects, sources, owners, and equal-length preceding period."""
    properties = _real_properties(scope)
    leads = _real_leads(scope)
    received = leads.filter(created_at__gte=period.start, created_at__lt=period.end)
    showings = _real_showings(scope)
    owner_inquiries = _real_owner_inquiries(scope)
    inquiries_received = owner_inquiries.filter(created_at__gte=period.start, created_at__lt=period.end)

    view_counts = dict(LandingDailyView.objects.filter(
        property__in=properties, day__gte=period.first_day, day__lte=period.last_day,
    ).values("property_id").annotate(total=Sum("views")).values_list("property_id", "total"))
    lead_counts = _counts(received, "property_id")
    qualified_counts = _counts(received.filter(qualification="qualified"), "property_id")
    converted_counts = _counts(received.filter(outcome="converted", outcome_deal__isnull=False), "property_id")
    showing_counts = _counts(showings.filter(
        status="completed", completed_at__gte=period.start, completed_at__lt=period.end,
    ), "deal__property_id")
    delivery_counts = dict(TelegramDelivery.objects.filter(
        publication__property__in=properties, sent_at__gte=period.start, sent_at__lt=period.end,
    ).values("publication__property_id").annotate(total=Count("pk")).values_list("publication__property_id", "total"))

    property_rows = [{
        "property": item,
        "views": view_counts.get(item.pk, 0),
        "leads": lead_counts.get(item.pk, 0),
        "qualified": qualified_counts.get(item.pk, 0),
        "converted": converted_counts.get(item.pk, 0),
        "showings": showing_counts.get(item.pk, 0),
        "telegram_delivered": delivery_counts.get(item.pk, 0),
    } for item in properties.order_by("title", "pk")]
    property_rows.sort(key=lambda row: (-row["leads"], -row["views"], row["property"].pk))

    source_labels = dict(Lead.SOURCE_CHOICES)
    def normalized_counts(queryset):
        counts = Counter()
        for key, count in _counts(queryset, "source").items():
            counts[key if key in source_labels else "unknown"] += count
        return counts

    source_counts = normalized_counts(received)
    source_qualified = normalized_counts(received.filter(qualification="qualified"))
    source_converted = normalized_counts(received.filter(outcome="converted", outcome_deal__isnull=False))
    channel_rows = [{
        "key": key, "label": source_labels.get(key, "Не указан"), "received": count,
        "qualified": source_qualified.get(key, 0), "converted": source_converted.get(key, 0),
    } for key, count in source_counts.items()]
    channel_rows.sort(key=lambda row: (-row["received"], row["label"]))

    open_inquiries = owner_inquiries.exclude(result="declined").exclude(status="archived").filter(property__isnull=True)
    overdue = sum(1 for item in open_inquiries.iterator() if item.attention_due_at < period.now)
    owners = {
        "received": inquiries_received.count(),
        "contacted": owner_inquiries.filter(first_contacted_at__gte=period.start, first_contacted_at__lt=period.end).count(),
        "agreed_cohort": inquiries_received.filter(result="agreed").count(),
        "linked_cohort": inquiries_received.filter(property__isnull=False).count(),
        "overdue_now": overdue,
    }

    days = (period.last_day - period.first_day).days + 1
    previous = AnalyticsPeriod.ending_now(
        now=period.now, first_day=period.first_day - timedelta(days=days),
        last_day=period.first_day - timedelta(days=1),
    )
    current_base = base_report or build_report(scope, period)
    previous_received = leads.filter(created_at__gte=previous.start, created_at__lt=previous.end).count()
    previous_deals = _real_deals(scope).filter(created_at__gte=previous.start, created_at__lt=previous.end).count()
    previous_tasks = _real_tasks(scope).filter(
        is_done=True, cancelled_at__isnull=True,
        completed_at__gte=previous.start, completed_at__lt=previous.end,
    ).count()
    comparison = [{
        "label": label, "current": current, "previous": prior, "delta": current - prior,
    } for label, current, prior in (
        ("Поступило заявок", current_base["during"]["received"], previous_received),
        ("Создано сделок", current_base["during"]["created_deals"], previous_deals),
        ("Выполнено задач", current_base["during"]["completed_tasks"], previous_tasks),
    )]

    return {
        "properties": property_rows, "channels": channel_rows,
        "owners": owners, "comparison": comparison, "previous_period": previous,
        "telegram_delivered": sum(delivery_counts.values()),
        "telegram_responses": received.filter(source="telegram_bot").count(),
        "landing_views": sum(view_counts.values()),
        "completed_showings": sum(showing_counts.values()),
    }
