import csv
from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from .agency_access import membership_for
from .services.csv_security import spreadsheet_safe_cell
from .services.crm_extended_analytics_service import build_extended_report
from .services.crm_analytics_service import (
    ANALYTICS_TZ, AnalyticsPeriod, build_report, resolve_scope, team_member_rows,
)


class AnalyticsView(LoginRequiredMixin, TemplateView):
    template_name = "properties/analytics.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        membership = membership_for(self.request.user)
        requested_scope = self.request.GET.get("scope", "team" if membership else "personal")
        scope = resolve_scope(
            self.request.user,
            requested_scope=requested_scope,
            requested_member=self.request.GET.get("member"),
            membership=membership,
        )
        now = timezone.now()
        today = now.astimezone(ANALYTICS_TZ).date()
        period_error = ""
        try:
            raw_first = self.request.GET.get("date_from")
            raw_last = self.request.GET.get("date_to")
            if raw_first or raw_last:
                last_day = date.fromisoformat(raw_last) if raw_last else today
                first_day = date.fromisoformat(raw_first) if raw_first else last_day - timedelta(days=29)
            else:
                days = self.request.GET.get("days", "30")
                if days not in {"7", "30", "90"}:
                    raise ValueError
                last_day = today
                first_day = today - timedelta(days=int(days) - 1)
            period = AnalyticsPeriod.ending_now(now=now, first_day=first_day, last_day=last_day)
        except ValueError:
            period_error = "Проверьте даты: период не может превышать 366 дней или заканчиваться раньше начала."
            period = AnalyticsPeriod.ending_now(now=now)

        report = build_report(scope, period)
        extended = build_extended_report(scope, period, base_report=report)
        date_params = {"date_from": period.first_day.isoformat(), "date_to": period.last_day.isoformat()}
        scope_params = {"scope": scope.kind}
        if scope.member_id and membership and membership.role != "agent":
            scope_params["member"] = scope.member_id

        def report_url(**params):
            return f"{reverse('analytics')}?{urlencode({**scope_params, **params})}"

        drilldown_scope = {"scope": scope.kind}
        if scope.agency_id and scope.member_id:
            if membership.role == "agent":
                drilldown_scope["assigned"] = "mine"
            else:
                drilldown_scope["responsible"] = str(scope.member_id)

        def lead_url(*, metric, source=None, first_day=None, last_day=None):
            params = {**drilldown_scope, "metric": metric}
            if metric in {"received", "contacted"}:
                params.update({
                    "date_from": (first_day or period.first_day).isoformat(),
                    "date_to": (last_day or period.last_day).isoformat(),
                })
            if source is not None:
                params["source"] = source
            return f"{reverse('lead_list')}?{urlencode(params)}"

        def deal_url(stage):
            return f"{reverse('deal_board')}?{urlencode({**drilldown_scope, 'stage': stage})}"

        for source in report["sources"]:
            source["url"] = lead_url(metric="received", source=source["key"])
            source["width"] = round(100 * source["count"] / report["during"]["received"]) if report["during"]["received"] else 0
        for bucket in report["chart"]:
            bucket["url"] = lead_url(metric="received", first_day=bucket["start"], last_day=bucket["end"])
        for stage in report["stages"]:
            stage["url"] = deal_url(stage["key"])
        member_rows = team_member_rows(scope, period) if membership and membership.role in {"owner", "manager"} else []
        for row in member_rows:
            if row["user_id"]:
                row["url"] = report_url(**date_params, member=row["user_id"])
        member_choices = (
            membership.agency.memberships.select_related("user", "user__realtor_profile").order_by("user_id")
            if membership and membership.role in {"owner", "manager"} else []
        )
        context.update({
            "report": report,
            "extended": extended,
            "export_urls": {
                kind: f"{reverse('analytics_export')}?{urlencode({**scope_params, **date_params, 'report': kind})}"
                for kind in ("properties", "channels", "owners", "comparison")
            },
            "period_error": period_error,
            "membership": membership,
            "member_choices": member_choices,
            "member_rows": member_rows,
            "can_view_team": bool(membership),
            "can_choose_member": bool(membership and membership.role in {"owner", "manager"}),
            "preset_links": [
                {"days": days, "url": report_url(days=days), "active": (period.last_day - period.first_day).days + 1 == days and period.last_day == today}
                for days in (7, 30, 90)
            ],
            "personal_url": report_url(scope="personal", **date_params),
            "team_url": f"{reverse('analytics')}?{urlencode({'scope': 'team', **date_params})}",
            "lead_links": {
                "received": lead_url(metric="received"),
                "contacted": lead_url(metric="contacted"),
                "unanswered": lead_url(metric="unanswered"),
                "overdue": lead_url(metric="overdue"),
            },
            "deal_url": reverse("deal_board") + ("?" + urlencode(drilldown_scope) if drilldown_scope else ""),
        })
        return context


class AnalyticsExportView(AnalyticsView):
    """Export the same scoped figures shown in the analytics screen."""

    def get(self, request, *args, **kwargs):
        kind = request.GET.get("report", "")
        if kind not in {"properties", "channels", "owners", "comparison"}:
            return HttpResponse("Неизвестный отчёт", status=400)
        context = self.get_context_data()
        if context["period_error"]:
            return HttpResponse(context["period_error"], status=400)
        extended = context["extended"]
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="analytics-{kind}.csv"'
        response.write("\ufeff")
        writer = csv.writer(response, quoting=csv.QUOTE_ALL)
        writer.writerow(["Период", f'{context["report"]["period"].first_day} — {context["report"]["period"].last_day}', "Москва"])
        if kind == "properties":
            writer.writerow(["Объект", "Первая публикация", "Просмотры лендинга", "Заявки", "Целевые", "Переведены в сделку", "Показы проведены", "Telegram доставлено"])
            for row in extended["properties"]:
                writer.writerow([
                    spreadsheet_safe_cell(row["property"].title),
                    row["property"].first_published_at.astimezone(ANALYTICS_TZ).date() if row["property"].first_published_at else "",
                    row["views"], row["leads"], row["qualified"], row["converted"], row["showings"], row["telegram_delivered"],
                ])
        elif kind == "channels":
            writer.writerow(["Источник", "Заявки", "Целевые", "Переведены в сделку"])
            for row in extended["channels"]:
                writer.writerow([spreadsheet_safe_cell(row["label"]), row["received"], row["qualified"], row["converted"]])
            writer.writerow(["Telegram: доставлено объявлений", extended["telegram_delivered"]])
            writer.writerow(["Telegram: откликов через бота", extended["telegram_responses"]])
        elif kind == "owners":
            writer.writerow(["Показатель", "Значение"])
            for label, key in (
                ("Поступило обращений", "received"), ("Первые разговоры", "contacted"),
                ("Согласованы из поступивших", "agreed_cohort"), ("Объект создан из поступивших", "linked_cohort"),
                ("Просрочены сейчас", "overdue_now"),
            ):
                writer.writerow([label, extended["owners"][key]])
        else:
            writer.writerow(["Показатель", "Текущий период", "Предыдущий период", "Разница"])
            for row in extended["comparison"]:
                writer.writerow([row["label"], row["current"], row["previous"], row["delta"]])
        return response
