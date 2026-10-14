"""Read-only CRM analytics built from the dates already stored in the database."""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Case, Count, DateTimeField, ExpressionWrapper, F, Min, Q, When
from django.db.models.functions import Least, TruncDate
from django.utils import timezone

from ..models import AgencyMembership, ClientReminder, Deal, Lead, Showing


ANALYTICS_TZ = ZoneInfo(settings.TIME_ZONE)
MAX_PERIOD_DAYS = 366


@dataclass(frozen=True)
class AnalyticsPeriod:
    first_day: date
    last_day: date
    start: datetime
    end: datetime
    now: datetime

    @classmethod
    def ending_now(cls, *, now=None, first_day=None, last_day=None):
        now = now or timezone.now()
        today = now.astimezone(ANALYTICS_TZ).date()
        last_day = min(last_day or today, today)
        first_day = first_day or (last_day - timedelta(days=29))
        if first_day > last_day or (last_day - first_day).days >= MAX_PERIOD_DAYS:
            raise ValueError("Период должен содержать от 1 до 366 дней.")
        start = datetime.combine(first_day, time.min, ANALYTICS_TZ)
        tomorrow = datetime.combine(last_day + timedelta(days=1), time.min, ANALYTICS_TZ)
        return cls(first_day, last_day, start, min(tomorrow, now), now)


@dataclass(frozen=True)
class AnalyticsScope:
    user_id: int
    agency_id: int | None = None
    member_id: int | None = None

    @property
    def kind(self):
        return "team" if self.agency_id else "personal"


def resolve_scope(user, *, requested_scope="personal", requested_member=None, membership=None):
    """Agents see only their own team metrics, even with a forged member id."""
    if requested_scope != "team" or membership is None or membership.user_id != user.pk:
        return AnalyticsScope(user_id=user.pk)
    if membership.role == "agent":
        return AnalyticsScope(user_id=user.pk, agency_id=membership.agency_id, member_id=user.pk)
    member_id = None
    if requested_member and str(requested_member).isdigit():
        candidate = int(requested_member)
        if AgencyMembership.objects.filter(agency_id=membership.agency_id, user_id=candidate).exists():
            member_id = candidate
    return AnalyticsScope(user_id=user.pk, agency_id=membership.agency_id, member_id=member_id)


def _real_leads(scope):
    qs = Lead.objects.filter(is_demo=False).filter(Q(property__isnull=True) | Q(property__is_demo=False)).filter(
        Q(client__isnull=True) | Q(client__is_demo=False),
    )
    if scope.agency_id:
        qs = qs.filter(agency_id=scope.agency_id)
        if scope.member_id:
            qs = qs.filter(assigned_to_id=scope.member_id)
    else:
        qs = qs.filter(agency__isnull=True, owner_id=scope.user_id)
    return qs


def _real_deals(scope):
    qs = Deal.objects.filter(is_demo=False, property__is_demo=False, client__is_demo=False).filter(
        Q(lead__isnull=True) | Q(lead__is_demo=False),
    )
    if scope.agency_id:
        qs = qs.filter(agency_id=scope.agency_id)
        if scope.member_id:
            qs = qs.filter(responsible_id=scope.member_id)
    else:
        qs = qs.filter(agency__isnull=True, owner_id=scope.user_id)
    return qs


def _real_tasks(scope):
    qs = ClientReminder.objects.filter(
        Q(client__isnull=True) | Q(client__is_demo=False),
        Q(deal__isnull=True) | Q(deal__is_demo=False, deal__property__is_demo=False),
        Q(lead__isnull=True) | (Q(lead__is_demo=False) & (Q(lead__property__isnull=True) | Q(lead__property__is_demo=False))),
        Q(property__isnull=True) | Q(property__is_demo=False),
        Q(owner_showing__isnull=True) | Q(owner_showing__deal__is_demo=False),
    )
    if scope.agency_id:
        qs = qs.filter(agency_id=scope.agency_id)
        if scope.member_id:
            qs = qs.filter(owner_id=scope.member_id)
    else:
        qs = qs.filter(agency__isnull=True, owner_id=scope.user_id)
    return qs


def _real_showings(scope):
    qs = Showing.objects.filter(deal__is_demo=False, deal__client__is_demo=False, deal__property__is_demo=False)
    if scope.agency_id:
        qs = qs.filter(agency_id=scope.agency_id)
        if scope.member_id:
            qs = qs.filter(owner_id=scope.member_id)
    else:
        qs = qs.filter(agency__isnull=True, owner_id=scope.user_id)
    return qs


def team_open_work(scope):
    """The manager queue: one row per open lead or independent task."""
    if not scope.agency_id or scope.member_id:
        raise ValueError("Для общей очереди нужна область команды.")
    leads = _real_leads(scope).filter(outcome="open").exclude(status="archived")
    tasks = _real_tasks(scope).filter(is_done=False, cancelled_at__isnull=True, lead__isnull=True)
    return leads, tasks


def _percentage(part, whole):
    return round(part * 100 / whole, 1) if whole else None


def build_report(scope, period):
    """Return distinct current-state and period metrics without reading PII."""
    leads = _real_leads(scope)
    deals = _real_deals(scope)
    tasks = _real_tasks(scope)
    showings = _real_showings(scope)
    open_leads = leads.filter(outcome="open").exclude(status="archived")
    unanswered = open_leads.filter(first_contacted_at__isnull=True)
    open_tasks = tasks.filter(is_done=False, cancelled_at__isnull=True)

    received = leads.filter(created_at__gte=period.start, created_at__lt=period.end)
    contacted = leads.filter(first_contacted_at__gte=period.start, first_contacted_at__lt=period.end)
    mature = received.filter(created_at__lte=period.now - timedelta(minutes=30))
    eligible_count = mature.count()
    on_time = mature.filter(
        first_contacted_at__gte=F("created_at"),
        first_contacted_at__lte=F("created_at") + timedelta(minutes=30),
    ).count()
    response_minutes = [
        (first_contact - created).total_seconds() / 60
        for created, first_contact in contacted.values_list("created_at", "first_contacted_at").iterator()
        if first_contact >= created
    ]
    source_rows = received.values("source").annotate(total=Count("pk")).order_by()
    known_sources = dict(Lead.SOURCE_CHOICES)
    source_counts = Counter()
    for row in source_rows:
        code = row["source"]
        source_counts[code if code in known_sources else "unknown"] += row["total"]
    sources = [
        {"key": code, "label": known_sources.get(code, "Не указан"), "count": total}
        for code, total in source_counts.items()
    ]
    sources.sort(key=lambda item: (-item["count"], item["label"]))

    daily_counts = {
        row["day"]: row["total"]
        for row in received.annotate(day=TruncDate("created_at", tzinfo=ANALYTICS_TZ))
        .values("day").annotate(total=Count("pk")).order_by()
    }
    days = (period.last_day - period.first_day).days + 1
    bucket_days = 1 if days <= 45 else 7
    chart = []
    for offset in range(0, days, bucket_days):
        bucket_start = period.first_day + timedelta(days=offset)
        bucket_end = min(period.last_day, bucket_start + timedelta(days=bucket_days - 1))
        total = sum(daily_counts.get(bucket_start + timedelta(days=day), 0) for day in range((bucket_end - bucket_start).days + 1))
        chart.append({"start": bucket_start, "end": bucket_end, "count": total})
    max_chart = max((bucket["count"] for bucket in chart), default=0)
    for bucket in chart:
        bucket["height"] = round(100 * bucket["count"] / max_chart) if max_chart else 0

    stage_counts = Counter(dict(deals.values("stage").annotate(total=Count("pk")).values_list("stage", "total")))
    stages = [{"key": key, "label": label, "count": stage_counts[key]} for key, label in Deal.STAGE_CHOICES]
    max_stage = max(stage_counts.values(), default=0)
    for stage in stages:
        stage["width"] = round(100 * stage["count"] / max_stage) if max_stage else 0

    return {
        "scope": scope,
        "period": period,
        "now": {
            "open_leads": open_leads.count(),
            "unanswered": unanswered.count(),
            "first_response_overdue": unanswered.filter(created_at__lt=period.now - timedelta(minutes=30)).count(),
            "overdue_leads": open_leads.filter(Lead.overdue_filter(period.now)).count(),
            "open_tasks": open_tasks.count(),
            "overdue_tasks": open_tasks.filter(due_at__lt=period.now).count(),
            "planned_showings": showings.filter(status="planned", starts_at__gte=period.now).count(),
            "active_deals": sum(stage_counts[key] for key in Deal.ACTIVE_STAGES),
        },
        "during": {
            "received": received.count(),
            "contacted": contacted.count(),
            "converted_leads": leads.filter(
                outcome="converted", outcome_deal__isnull=False,
                outcome_at__gte=period.start, outcome_at__lt=period.end,
            ).count(),
            "converted_cohort": received.filter(outcome="converted", outcome_deal__isnull=False).count(),
            "created_deals": deals.filter(created_at__gte=period.start, created_at__lt=period.end).count(),
            "sla_eligible": eligible_count,
            "sla_on_time": on_time,
            "sla_percent": _percentage(on_time, eligible_count),
            "median_response_minutes": round(median(response_minutes), 1) if response_minutes else None,
            "median_sample": len(response_minutes),
            "completed_tasks": tasks.filter(
                is_done=True, cancelled_at__isnull=True,
                completed_at__gte=period.start, completed_at__lt=period.end,
            ).count(),
        },
        "chart": chart,
        "sources": sources,
        "stages": stages,
    }


def team_member_rows(scope, period):
    """Compact manager-only breakdown; records without an assignee stay visible."""
    if not scope.agency_id or scope.member_id:
        return []
    members = AgencyMembership.objects.filter(agency_id=scope.agency_id).select_related("user", "user__realtor_profile").order_by("user_id")
    active_lead = Q(outcome="open") & ~Q(status="archived")
    first_response_due = ExpressionWrapper(F("created_at") + timedelta(minutes=30), output_field=DateTimeField())
    lead_due = Case(
        When(first_contacted_at__isnull=True, then=Least(first_response_due, F("next_action_due_at"))),
        default=F("next_action_due_at"), output_field=DateTimeField(),
    )
    lead_rows = {
        row["assigned_to_id"]: row for row in _real_leads(scope).values("assigned_to_id").annotate(
            received=Count("pk", filter=Q(created_at__gte=period.start, created_at__lt=period.end)),
            open_leads=Count("pk", filter=active_lead),
            unanswered=Count("pk", filter=active_lead & Q(first_contacted_at__isnull=True)),
            overdue=Count("pk", filter=Lead.overdue_filter(period.now) & ~Q(status="archived")),
            next_due_at=Min(lead_due, filter=active_lead),
        )
    }
    task_rows = {
        row["owner_id"]: row for row in _real_tasks(scope).values("owner_id").annotate(
            open_tasks=Count("pk", filter=Q(is_done=False, cancelled_at__isnull=True)),
            overdue_independent_tasks=Count("pk", filter=Q(
                is_done=False, cancelled_at__isnull=True, lead__isnull=True, due_at__lt=period.now,
            )),
            next_due_at=Min("due_at", filter=Q(is_done=False, cancelled_at__isnull=True)),
        )
    }
    deal_rows = {
        row["responsible_id"]: row for row in _real_deals(scope).values("responsible_id").annotate(
            active_deals=Count("pk", filter=Q(stage__in=Deal.ACTIVE_STAGES)),
        )
    }
    rows = []

    def nearest_due(user_id):
        dates = (lead_rows.get(user_id, {}).get("next_due_at"), task_rows.get(user_id, {}).get("next_due_at"))
        return min((value for value in dates if value is not None), default=None)

    for membership in members:
        user_id = membership.user_id
        rows.append({
            "user": membership.user,
            "user_id": user_id,
            "received": lead_rows.get(user_id, {}).get("received", 0),
            "open_leads": lead_rows.get(user_id, {}).get("open_leads", 0),
            "unanswered": lead_rows.get(user_id, {}).get("unanswered", 0),
            "overdue": lead_rows.get(user_id, {}).get("overdue", 0),
            "urgent_work": lead_rows.get(user_id, {}).get("overdue", 0) + task_rows.get(user_id, {}).get("overdue_independent_tasks", 0),
            "open_tasks": task_rows.get(user_id, {}).get("open_tasks", 0),
            "active_deals": deal_rows.get(user_id, {}).get("active_deals", 0),
            "next_due_at": nearest_due(user_id),
        })
    if None in lead_rows or None in deal_rows:
        rows.append({
            "user": None,
            "user_id": None,
            "received": lead_rows.get(None, {}).get("received", 0),
            "open_leads": lead_rows.get(None, {}).get("open_leads", 0),
            "unanswered": lead_rows.get(None, {}).get("unanswered", 0),
            "overdue": lead_rows.get(None, {}).get("overdue", 0),
            "urgent_work": lead_rows.get(None, {}).get("overdue", 0),
            "open_tasks": 0,
            "active_deals": deal_rows.get(None, {}).get("active_deals", 0),
            "next_due_at": nearest_due(None),
        })
    return rows
