from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Agency, AgencyMembership, Client, ClientReminder, Deal, Lead, Property
from .services.crm_analytics_service import ANALYTICS_TZ, AnalyticsPeriod, AnalyticsScope, build_report, resolve_scope, team_member_rows


class AnalyticsServiceTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 11, 12, tzinfo=dt_timezone.utc)
        self.period = AnalyticsPeriod.ending_now(now=self.now, first_day=date(2026, 10, 1))
        self.user = User.objects.create_user("analytics-owner", password="password")
        self.property = Property.objects.create(owner=self.user, title="Квартира")
        self.client_record = Client.objects.create(owner=self.user, name="Анна", phone="+79990000111")
        self.scope = AnalyticsScope(user_id=self.user.pk)

    def lead(self, *, minutes_ago, response_after=None, property=None, **fields):
        property = property or self.property
        lead = Lead.objects.create(
            property=property, client=fields.pop("client", self.client_record),
            name="Анна", phone="+79990000111", **fields,
        )
        created = self.now - timedelta(minutes=minutes_ago)
        values = {"created_at": created}
        if response_after is not None:
            values["first_contacted_at"] = created + timedelta(minutes=response_after)
        Lead.objects.filter(pk=lead.pk).update(**values)
        return lead

    def test_first_response_archive_demo_and_time_zone(self):
        answered = self.lead(minutes_ago=120, response_after=20, source="landing")
        unanswered = self.lead(minutes_ago=70, source="telegram_bot")
        archived = self.lead(minutes_ago=180, response_after=40)
        archived.status = "archived"
        archived.save(update_fields=["status"])
        Lead.objects.filter(pk=archived.pk).update(created_at=self.now - timedelta(minutes=180), first_contacted_at=self.now - timedelta(minutes=140))
        demo_property = Property.objects.create(owner=self.user, title="Демо", is_demo=True)
        self.lead(minutes_ago=90, property=demo_property, client=None, is_demo=True)

        report = build_report(self.scope, self.period)

        self.assertEqual(report["during"]["received"], 3)
        self.assertEqual(report["during"]["sla_eligible"], 3)
        self.assertEqual(report["during"]["sla_on_time"], 1)
        self.assertEqual(report["during"]["sla_percent"], 33.3)
        self.assertEqual(report["during"]["median_response_minutes"], 30)
        self.assertEqual(report["now"]["unanswered"], 1)
        self.assertEqual(report["now"]["first_response_overdue"], 1)
        self.assertEqual(report["now"]["overdue_leads"], 1)
        self.assertEqual(report["now"]["open_tasks"], 2)
        self.assertEqual({item["key"]: item["count"] for item in report["sources"]}, {"landing": 2, "telegram_bot": 1})
        self.assertEqual(sum(bucket["count"] for bucket in report["chart"]), 3)
        self.assertEqual(report["chart"][-1]["start"], date(2026, 10, 11))
        self.assertTrue(Lead.objects.filter(pk=answered.pk).exists())
        self.assertTrue(Lead.objects.filter(pk=unanswered.pk).exists())

    def test_two_leads_can_convert_to_one_deal_without_double_counting_deals(self):
        first = self.lead(minutes_ago=240, response_after=10)
        second = self.lead(minutes_ago=220, response_after=12)
        deal = Deal.objects.create(owner=self.user, client=self.client_record, property=self.property)
        Deal.objects.filter(pk=deal.pk).update(created_at=self.now - timedelta(hours=2))
        Lead.objects.filter(pk__in=[first.pk, second.pk]).update(
            outcome="converted", outcome_deal=deal, outcome_at=self.now - timedelta(hours=1),
        )

        report = build_report(self.scope, self.period)

        self.assertEqual(report["during"]["received"], 2)
        self.assertEqual(report["during"]["converted_leads"], 2)
        self.assertEqual(report["during"]["converted_cohort"], 2)
        self.assertEqual(report["during"]["created_deals"], 1)
        self.assertEqual(report["now"]["active_deals"], 1)
        self.assertEqual(next(stage["count"] for stage in report["stages"] if stage["key"] == "new"), 1)

    def test_completed_tasks_exclude_cancelled_and_demo(self):
        completed = ClientReminder.objects.create(owner=self.user, client=self.client_record, text="Позвонить")
        ClientReminder.objects.filter(pk=completed.pk).update(is_done=True, completed_at=self.now - timedelta(hours=1))
        cancelled = ClientReminder.objects.create(owner=self.user, client=self.client_record, text="Отмена")
        ClientReminder.objects.filter(pk=cancelled.pk).update(
            is_done=True, completed_at=self.now - timedelta(hours=1), cancelled_at=self.now,
        )
        demo_client = Client.objects.create(owner=self.user, name="Демо", phone="+79990000999", is_demo=True)
        ClientReminder.objects.create(owner=self.user, client=demo_client, text="Демо")

        report = build_report(self.scope, self.period)

        self.assertEqual(report["during"]["completed_tasks"], 1)
        self.assertEqual(report["now"]["open_tasks"], 0)

    def test_empty_denominator_is_not_zero_percent(self):
        report = build_report(self.scope, self.period)
        self.assertIsNone(report["during"]["sla_percent"])
        self.assertIsNone(report["during"]["median_response_minutes"])

    def test_moscow_midnight_sets_period_boundary(self):
        self.lead(minutes_ago=14 * 60 + 30)  # 10 October 21:30 UTC = 11 October 00:30 Moscow
        one_day = AnalyticsPeriod.ending_now(now=self.now, first_day=date(2026, 10, 11), last_day=date(2026, 10, 11))
        report = build_report(self.scope, one_day)
        self.assertEqual(report["during"]["received"], 1)
        self.assertEqual(len(report["chart"]), 1)


class AnalyticsAccessTests(TestCase):
    def setUp(self):
        self.founder = User.objects.create_user("analytics-founder", password="password")
        self.agent = User.objects.create_user("analytics-agent", password="password")
        self.other = User.objects.create_user("analytics-other", password="password")
        self.agency = Agency.objects.create(name="Команда", owner=self.founder)
        self.owner_membership = AgencyMembership.objects.create(agency=self.agency, user=self.founder, role="owner")
        self.agent_membership = AgencyMembership.objects.create(agency=self.agency, user=self.agent, role="agent")
        self.team_property = Property.objects.create(owner=self.founder, agency=self.agency, title="Общий объект")
        self.private_property = Property.objects.create(owner=self.founder, title="Личный объект")
        self.team_client = Client.objects.create(owner=self.founder, agency=self.agency, name="А", phone="+79990000001")
        self.agent_lead = Lead.objects.create(property=self.team_property, client=self.team_client, assigned_to=self.agent, name="А", phone="+79990000001")
        self.founder_lead = Lead.objects.create(property=self.team_property, client=self.team_client, assigned_to=self.founder, name="Б", phone="+79990000002")
        self.personal_lead = Lead.objects.create(property=self.private_property, name="Личный", phone="+79990000003")
        self.now = timezone.now()
        first_day = self.now.astimezone(ANALYTICS_TZ).date() - timedelta(days=10)
        self.period = AnalyticsPeriod.ending_now(now=self.now, first_day=first_day)
        Lead.objects.filter(pk__in=[self.agent_lead.pk, self.founder_lead.pk, self.personal_lead.pk]).update(created_at=self.now - timedelta(hours=1))

    def test_team_and_personal_scopes_never_mix(self):
        team = build_report(resolve_scope(self.founder, requested_scope="team", membership=self.owner_membership), self.period)
        personal = build_report(resolve_scope(self.founder, requested_scope="personal", membership=self.owner_membership), self.period)
        self.assertEqual(team["during"]["received"], 2)
        self.assertEqual(personal["during"]["received"], 1)
        self.assertEqual(team["now"]["unanswered"], 2)
        self.assertEqual(personal["now"]["unanswered"], 1)

    def test_agent_cannot_request_other_members_metrics(self):
        scope = resolve_scope(self.agent, requested_scope="team", requested_member=self.founder.pk, membership=self.agent_membership)
        self.assertEqual(scope.member_id, self.agent.pk)
        self.assertEqual(build_report(scope, self.period)["during"]["received"], 1)
        self.client.force_login(self.agent)
        response = self.client.get(reverse("analytics"), {"scope": "team", "member": self.founder.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["report"]["scope"].member_id, self.agent.pk)
        self.assertNotContains(response, "По ответственным")

    def test_manager_breakdown_and_filtered_links(self):
        scope = resolve_scope(self.founder, requested_scope="team", membership=self.owner_membership)
        rows = team_member_rows(scope, self.period)
        self.assertEqual({row["user_id"]: row["received"] for row in rows}, {self.founder.pk: 1, self.agent.pk: 1})
        self.client.force_login(self.founder)
        response = self.client.get(reverse("analytics"), {"scope": "team"})
        self.assertContains(response, "По ответственным")
        self.assertContains(response, f"{reverse('lead_list')}?scope=team&amp;metric=received")
        self.assertContains(response, f"{reverse('deal_board')}?scope=team&amp;stage=new")
        self.assertEqual(response.context["report"]["during"]["received"], 2)
        chart_bucket = next(bucket for bucket in response.context["report"]["chart"] if bucket["count"] == 2)
        chart_list = self.client.get(chart_bucket["url"])
        self.assertEqual({lead.pk for lead in chart_list.context["leads"]}, {self.agent_lead.pk, self.founder_lead.pk})

    def test_member_overdue_includes_missed_next_action(self):
        Lead.objects.filter(pk=self.agent_lead.pk).update(
            first_contacted_at=self.now - timedelta(minutes=40),
            next_action_due_at=self.now - timedelta(minutes=1),
        )
        scope = resolve_scope(self.founder, requested_scope="team", membership=self.owner_membership)
        report = build_report(scope, self.period)
        rows = team_member_rows(scope, self.period)
        agent_row = next(row for row in rows if row["user_id"] == self.agent.pk)
        self.assertEqual(agent_row["overdue"], 1)
        self.assertEqual(sum(row["overdue"] for row in rows), report["now"]["overdue_leads"])

    def test_manager_can_see_team_breakdown_but_outsider_cannot(self):
        manager = User.objects.create_user("analytics-manager", password="password")
        AgencyMembership.objects.create(agency=self.agency, user=manager, role="manager")
        self.client.force_login(manager)
        page = self.client.get(reverse("analytics"), {"scope": "team"})
        self.assertContains(page, "По ответственным")
        self.assertContains(page, 'name="member"')
        self.client.force_login(self.other)
        outsider = self.client.get(reverse("analytics"), {"scope": "team", "member": self.founder.pk})
        self.assertEqual(outsider.context["report"]["scope"].kind, "personal")
        self.assertEqual(outsider.context["report"]["during"]["received"], 0)
        self.assertNotContains(outsider, "По ответственным")

    def test_period_filter_validates_dates_and_source_drilldown(self):
        self.client.force_login(self.founder)
        today = self.now.astimezone(ANALYTICS_TZ).date()
        seven_days = self.client.get(reverse("analytics"), {"scope": "team", "days": "7"})
        self.assertEqual(seven_days.context["report"]["period"].first_day, today - timedelta(days=6))
        self.assertContains(seven_days, '<details class="analytics-filters__advanced">')
        self.assertContains(seven_days, 'id="analytics-from-mobile"')
        invalid = self.client.get(reverse("analytics"), {
            "scope": "team", "date_from": (today + timedelta(days=1)).isoformat(), "date_to": today.isoformat(),
        })
        self.assertContains(invalid, "Проверьте даты")
        self.assertContains(invalid, '<details class="analytics-filters__advanced" open>')
        self.assertEqual((invalid.context["report"]["period"].last_day - invalid.context["report"]["period"].first_day).days, 29)
        filtered = self.client.get(reverse("lead_list"), {
            "scope": "team", "metric": "received", "responsible": self.agent.pk,
            "source": "landing", "date_from": self.period.first_day.isoformat(), "date_to": today.isoformat(),
        })
        self.assertEqual([lead.pk for lead in filtered.context["leads"]], [self.agent_lead.pk])
        self.assertContains(filtered, "Аналитика · Заявки за период · Лендинг")

    def test_current_overdue_is_grouped_with_the_work_queue(self):
        self.client.force_login(self.founder)
        response = self.client.get(reverse("analytics"), {"scope": "team"})
        html = response.content.decode()
        period_section = html.split('id="analytics-period-title"', 1)[1].split('id="analytics-now-title"', 1)[0]
        now_section = html.split('id="analytics-now-title"', 1)[1].split('class="analytics-queue"', 1)[0]

        self.assertNotIn("Просроченных заявок сейчас", period_section)
        self.assertIn("Просроченных заявок сейчас", now_section)
        self.assertIn(response.context["lead_links"]["overdue"].replace("&", "&amp;"), now_section)

    def test_drilldown_leads_respects_scope_and_metric(self):
        self.client.force_login(self.founder)
        response = self.client.get(reverse("lead_list"), {
            "scope": "team", "metric": "unanswered", "responsible": self.agent.pk,
        })
        self.assertEqual([lead.pk for lead in response.context["leads"]], [self.agent_lead.pk])
        self.assertContains(response, "Аналитика · Без первого ответа")
        self.assertContains(response, 'name="metric" value="unanswered"')

    def test_filtered_deal_board_shows_selected_stage_only(self):
        deal = Deal.objects.create(owner=self.founder, agency=self.agency, responsible=self.agent,
                                   client=self.team_client, property=self.team_property, stage="viewing")
        self.client.force_login(self.founder)
        response = self.client.get(reverse("deal_board"), {"scope": "team", "stage": "viewing", "responsible": self.agent.pk})
        self.assertEqual(response.context["analytics_stage_filter"], "viewing")
        self.assertContains(response, "Аналитика · Команда · этап «Показ»")
        self.assertContains(response, f'data-deal-id="{deal.pk}"')
        self.assertContains(response, 'data-deal-stage="new"', count=1)
        self.assertContains(response, 'data-deal-stage="new" data-active-stage="true" hidden')
