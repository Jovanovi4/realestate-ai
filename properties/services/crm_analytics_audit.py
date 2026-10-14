"""Read-only reconciliation of analytics with the CRM list querysets."""

from datetime import timedelta
from time import perf_counter

from django.db import connection, transaction
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ..agency_access import workspace_queryset
from ..analytics_views import AnalyticsView
from ..deal_views import DealBoardView
from ..models import OwnerInquiry
from ..views import LeadListView
from .crm_analytics_service import AnalyticsPeriod, _real_leads, build_report
from .crm_extended_analytics_service import build_extended_report


def audit_report(user, scope, *, days=30):
    """Use the actual lead/deal list filters as an independent count source."""
    period = AnalyticsPeriod.ending_now(now=timezone.now())
    if days != 30:
        period = AnalyticsPeriod.ending_now(
            now=period.now, first_day=period.last_day - timedelta(days=days - 1),
        )
    filters = {"scope": scope.kind}
    if scope.agency_id and scope.member_id:
        if scope.member_id == user.pk:
            filters["assigned"] = "mine"
        else:
            filters["responsible"] = str(scope.member_id)
    dates = {"date_from": period.first_day.isoformat(), "date_to": period.last_day.isoformat()}
    factory = RequestFactory()

    def lead_list_count(metric, *, source=None):
        params = {**filters, **dates, "metric": metric}
        if source is not None:
            params["source"] = source
        request = factory.get("/leads/", params)
        request.user = user
        view = LeadListView()
        view.request = request
        return view.get_queryset().count()

    def board_count(*, stage=None, created=False):
        params = dict(filters)
        if stage:
            params["stage"] = stage
        request = factory.get("/deals/", params)
        request.user = user
        view = DealBoardView()
        view.request = request
        records = view.filtered_deals()
        if created:
            records = records.filter(created_at__gte=period.start, created_at__lt=period.end)
        return records.count()

    already_in_transaction = connection.in_atomic_block
    with transaction.atomic():
        if connection.vendor == "postgresql" and not already_in_transaction:
            # A stable snapshot prevents a new lead arriving between report and list counts.
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("SET TRANSACTION READ ONLY")
        with CaptureQueriesContext(connection) as captured:
            started = perf_counter()
            report = build_report(scope, period)
            extended = build_extended_report(scope, period, base_report=report)
            report_ms = round((perf_counter() - started) * 1000, 1)
            report_queries = len(captured)

            comparisons = {}

            def compare(key, reported, listed):
                comparisons[key] = {"analytics": reported, "crm_list": listed, "match": reported == listed}

            compare("leads_received", report["during"]["received"], lead_list_count("received"))
            compare("leads_contacted", report["during"]["contacted"], lead_list_count("contacted"))
            compare("leads_unanswered", report["now"]["unanswered"], lead_list_count("unanswered"))
            compare("deals_created", report["during"]["created_deals"], board_count(created=True))
            for stage in report["stages"]:
                compare(f'deal_stage_{stage["key"]}', stage["count"], board_count(stage=stage["key"]))
            for source in report["sources"]:
                compare(f'lead_source_{source["key"]}', source["count"], lead_list_count("received", source=source["key"]))

            # The owners list has no period filter yet; apply its base workspace
            # queryset, then the documented analytics cohort and demo rules.
            inquiries = workspace_queryset(OwnerInquiry.objects, user).filter(client__is_demo=False)
            inquiries = inquiries.filter(property__isnull=True) | inquiries.filter(property__is_demo=False)
            if scope.kind == "personal":
                inquiries = inquiries.filter(agency__isnull=True, owner_id=user.pk)
            else:
                inquiries = inquiries.filter(agency_id=scope.agency_id)
                if scope.member_id:
                    inquiries = inquiries.filter(assigned_to_id=scope.member_id)
            compare("owner_inquiries_received", extended["owners"]["received"], inquiries.filter(
                created_at__gte=period.start, created_at__lt=period.end,
            ).count())
            compare("property_lead_total", _real_leads(scope).filter(
                property__isnull=False, created_at__gte=period.start, created_at__lt=period.end,
            ).count(), sum(row["leads"] for row in extended["properties"]))

            all_ms = round((perf_counter() - started) * 1000, 1)
            total_queries = len(captured)

        page_params = {"scope": scope.kind, **dates}
        if scope.agency_id and scope.member_id:
            page_params["member"] = str(scope.member_id)
        page_request = factory.get("/analytics/", page_params)
        page_request.user = user
        with CaptureQueriesContext(connection) as page_queries:
            page_started = perf_counter()
            page_response = AnalyticsView.as_view()(page_request)
            page_response.render()
            page_ms = round((perf_counter() - page_started) * 1000, 1)
        if page_response.status_code != 200:
            raise RuntimeError(f"Страница аналитики вернула HTTP {page_response.status_code}.")

    return {
        "database": connection.vendor,
        "scope": {"kind": scope.kind, "agency_id": scope.agency_id, "member_id": scope.member_id},
        "period": {"first_day": period.first_day.isoformat(), "last_day": period.last_day.isoformat()},
        "performance": {
            "analytics_ms": report_ms, "analytics_queries": report_queries,
            "full_audit_ms": all_ms, "full_audit_queries": total_queries,
            "page_ms": page_ms, "page_queries": len(page_queries),
        },
        "comparisons": comparisons,
        "mismatches": [key for key, value in comparisons.items() if not value["match"]],
    }
