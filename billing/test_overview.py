from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch
import uuid

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from properties.models import Agency, AgencyMembership, RealtorProfile

from .models import BillingAccount, BillingPeriod, PlanVersion, Subscription
from .rules import AccessState
from .services import AccountService, SubscriptionService, current_plan_version
from .tests import seed_test_catalog


START = datetime(2026, 1, 31, 12, 15, tzinfo=datetime_timezone.utc)
NOW = START + timedelta(days=1)


@override_settings(BILLING_SHADOW_ENABLED=False, TIME_ZONE="Europe/Lisbon")
class BillingOverviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_test_catalog()
        User = get_user_model()
        cls.user = User.objects.create_user("overview-owner")
        cls.other = User.objects.create_user("overview-other")
        cls.operator = User.objects.create_user("overview-operator", is_staff=True, is_superuser=True)

    def setUp(self):
        self.client.force_login(self.user)

    def overview(self, *, now=NOW, params=None):
        with patch("billing.views.timezone.now", return_value=now):
            return self.client.get(reverse("billing:overview"), params)

    def period(self, *, account=None, plan="standard", starts_at=START):
        account = account or AccountService.ensure_personal(self.user)
        return SubscriptionService.grant_period(
            account=account, plan_version=current_plan_version(plan), actor=self.operator,
            reason="PRIVATE OPERATOR REASON", request_key=uuid.uuid4(), starts_at=starts_at,
        )

    def team(self, *, role="agent"):
        agency = Agency.objects.create(owner=self.other, name="Команда для проверки")
        AgencyMembership.objects.create(agency=agency, user=self.other, role="owner")
        member = AgencyMembership.objects.create(agency=agency, user=self.user, role=role)
        return agency, member, AccountService.ensure_agency(agency)

    def test_anonymous_user_must_log_in(self):
        self.client.logout()
        response = self.overview()
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('billing:overview')}")

    def test_demo_without_account_is_read_only_even_with_verified_email(self):
        RealtorProfile.objects.create(user=self.user, email="owner@example.test", email_verified_at=NOW)
        before = {model: model.objects.count() for model in apps.get_app_config("billing").get_models()}

        def reads_only(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reads_only):
            response = self.overview()
        self.assertContains(response, "Демо")
        self.assertContains(response, "Демо действует без срока окончания")
        self.assertNotContains(response, "Часовой пояс дат:")
        self.assertNotContains(response, "<dt>Ожидается</dt>")
        self.assertEqual(response.context["billing_workspaces"][0]["state"], AccessState.DEMO)
        self.assertEqual(before, {model: model.objects.count() for model in before})
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_current_period_dates_and_pilot_source_are_displayed(self):
        period = self.period()
        response = self.overview()
        workspace = response.context["billing_workspaces"][0]
        self.assertEqual(workspace["plan_name"], "Стандарт")
        self.assertEqual(workspace["state"], AccessState.ACTIVE)
        self.assertEqual(workspace["period"]["starts_at"], period.starts_at)
        self.assertEqual(workspace["period"]["ends_at"], period.ends_at)
        self.assertContains(response, "31.01.2026, 12:15")
        self.assertContains(response, "28.02.2026, 12:15")
        self.assertContains(response, "Пилотный доступ")
        self.assertContains(response, "Тарифные ограничения пока не применяются")
        self.assertNotContains(response, "PRIVATE OPERATOR REASON")
        self.assertNotContains(response, "Оплачено")

    def test_period_boundaries_and_grace_follow_the_policy_clock(self):
        period = self.period()
        cases = (
            (period.starts_at - timedelta(microseconds=1), AccessState.DEMO),
            (period.starts_at, AccessState.ACTIVE),
            (period.ends_at - timedelta(microseconds=1), AccessState.ACTIVE),
            (period.ends_at, AccessState.GRACE),
            (period.ends_at + timedelta(days=7), AccessState.RESTRICTED),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                workspace = self.overview(now=now).context["billing_workspaces"][0]
                self.assertEqual(workspace["state"], expected)
                if expected == AccessState.GRACE:
                    self.assertEqual(workspace["grace_ends_at"], period.ends_at + timedelta(days=7))
                    self.assertEqual(workspace["period_label"], "Завершённый период")
                if expected == AccessState.RESTRICTED:
                    self.assertEqual(workspace["plan_name"], "Демо")
                    self.assertEqual(workspace["period_label"], "Последний период")
                    self.assertEqual(workspace["period"]["plan_name"], "Стандарт")

    def test_grace_duration_uses_period_snapshot(self):
        version = current_plan_version("standard")
        terms = {name: getattr(version, name) for name in PlanVersion.TERMS}
        custom = PlanVersion.objects.create(**{**terms, "version": 2, "grace_days": 3})
        account = AccountService.ensure_personal(self.user)
        period = SubscriptionService.grant_period(
            account=account, plan_version=custom, actor=self.operator, reason="Проверка льготного срока",
            request_key=uuid.uuid4(), starts_at=START,
        )
        workspace = self.overview(now=period.ends_at).context["billing_workspaces"][0]
        self.assertEqual(workspace["grace_ends_at"], period.ends_at + timedelta(days=3))

    def test_future_period_does_not_replace_current_tariff(self):
        period = self.period(starts_at=START + timedelta(days=10))
        workspace = self.overview().context["billing_workspaces"][0]
        self.assertEqual(workspace["state"], AccessState.DEMO)
        self.assertEqual(workspace["plan_name"], "Демо")
        self.assertIsNone(workspace["period"])
        self.assertEqual(workspace["upcoming_period"]["starts_at"], period.starts_at)

    def test_current_and_next_assigned_periods_are_separate(self):
        current = self.period()
        upcoming = self.period(plan="business", starts_at=current.ends_at)
        workspace = self.overview().context["billing_workspaces"][0]
        self.assertEqual(workspace["plan_name"], "Стандарт")
        self.assertEqual(workspace["period"]["ends_at"], upcoming.starts_at)
        self.assertEqual(workspace["upcoming_period"]["plan_name"], "Бизнес")

    def test_suspension_keeps_assigned_plan_and_hides_internal_reason(self):
        period = self.period()
        subscription = period.subscription
        subscription.is_suspended = True
        subscription.suspension_reason = "PRIVATE SUSPENSION REASON"
        subscription.save()
        response = self.overview()
        workspace = response.context["billing_workspaces"][0]
        self.assertEqual(workspace["state"], AccessState.SUSPENDED)
        self.assertEqual(workspace["plan_name"], "Стандарт")
        self.assertEqual(workspace["period_label"], "Назначенный период")
        self.assertContains(response, "Приостановлен")
        self.assertNotContains(response, "PRIVATE SUSPENSION REASON")

    def test_voided_period_is_not_presented_as_current_or_future_access(self):
        period = self.period()
        period.voided_at, period.void_reason = NOW, "PRIVATE VOID REASON"
        period.save()
        response = self.overview()
        workspace = response.context["billing_workspaces"][0]
        self.assertIsNone(workspace["period"])
        self.assertIsNone(workspace["upcoming_period"])
        self.assertNotContains(response, "PRIVATE VOID REASON")

    def test_dates_are_rendered_in_the_current_timezone_including_dst(self):
        period = self.period(starts_at=START.replace(month=7))
        response = self.overview(now=period.starts_at)
        self.assertContains(response, "31.07.2026, 13:15")
        self.assertContains(response, "31.08.2026, 13:15")
        self.assertContains(response, "Часовой пояс дат: Europe/Lisbon")

    def test_personal_and_team_tariffs_are_separate_for_every_member_role(self):
        _, member, account = self.team()
        self.period(account=account, plan="business")
        self.period()
        for role in ("agent", "manager"):
            member.role = role
            member.save()
            with self.subTest(role=role):
                workspaces = self.overview().context["billing_workspaces"]
                self.assertEqual([(w["kind"], w["plan_name"]) for w in workspaces], [("personal", "Стандарт"), ("team", "Бизнес")])
        self.client.force_login(self.other)
        workspaces = self.overview().context["billing_workspaces"]
        self.assertEqual([(w["kind"], w["plan_name"]) for w in workspaces], [("personal", "Демо"), ("team", "Бизнес")])

    def test_removed_member_and_payer_cannot_view_the_team(self):
        agency, member, account = self.team()
        self.period(account=account, plan="business")
        account.payer = self.user
        account.save()
        member.delete()
        response = self.overview(params={"account_id": account.pk, "agency_id": agency.pk})
        self.assertEqual(len(response.context["billing_workspaces"]), 1)
        self.assertFalse(response.context["billing_has_team"])
        self.assertNotContains(response, 'id="billing-team-tab"')
        self.assertNotContains(response, agency.name)
        self.assertNotContains(response, "Бизнес")

    def test_team_without_billing_account_shows_demo_without_writes(self):
        agency = Agency.objects.create(owner=self.user, name="Новая команда")
        AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
        response = self.overview()
        workspaces = response.context["billing_workspaces"]
        self.assertEqual([w["plan_name"] for w in workspaces], ["Демо", "Демо"])
        self.assertFalse(BillingAccount.objects.exists())
        self.assertFalse(Subscription.objects.exists())

    def test_missing_catalog_or_corrupt_snapshot_show_a_repair_state(self):
        period = self.period()
        BillingPeriod.objects.filter(pk=period.pk).update(terms_snapshot={"schema": 999})
        response = self.overview()
        self.assertContains(response, "Требует проверки")
        self.assertEqual(response.context["billing_workspaces"][0]["state"], AccessState.UNKNOWN)
        self.client.force_login(self.other)
        PlanVersion.objects.filter(plan__code="demo").update(available=False)
        self.assertContains(self.overview(), "Не определён")

    def test_profile_is_the_only_navigation_entry_and_post_is_not_supported(self):
        profile = self.client.get(reverse("edit_profile"))
        self.assertContains(profile, f'href="{reverse("billing:overview")}"', count=1)
        self.assertNotContains(self.overview(), f'href="{reverse("billing:overview")}"')
        self.assertEqual(self.client.post(reverse("billing:overview")).status_code, 405)

    def test_resource_descriptions_are_visible_and_help_is_limited_to_headings(self):
        response = self.overview()
        self.assertContains(response, '<p class="billing-resource__hint">', count=4)
        self.assertContains(response, "Вне архива, включая черновики")
        self.assertContains(response, "Объекты считаются вне архива, включая черновики, завершённые и демо")
        self.assertNotContains(response, 'aria-label="Пояснение: Объекты"')
        self.assertContains(response, 'aria-label="Пояснение: Ресурсы"')
        self.assertNotContains(response, 'aria-label="Пояснение: Ожидаемый расход ИИ"')
        self.assertNotContains(response, "billing-help--end")
        self.assertContains(response, 'class="billing-help" type="button" data-ui-help')
        self.assertContains(response, 'class="billing-help__content" hidden')
        self.assertContains(response, 'href="#ui-icon-info"')
        self.assertContains(response, "Тарифные ограничения пока не применяются")

    def test_team_workspaces_use_the_existing_tabs_and_personal_only_does_not(self):
        personal_response = self.overview()
        self.assertFalse(personal_response.context["billing_has_team"])
        self.assertNotContains(personal_response, 'id="billing-workspace-tabs"')
        self.assertContains(personal_response, 'id="billing-personal-pane"')

        self.team()
        team_response = self.overview()
        self.assertTrue(team_response.context["billing_has_team"])
        self.assertContains(team_response, 'id="billing-workspace-tabs"')
        self.assertContains(team_response, 'id="billing-personal-tab" data-bs-toggle="tab" data-bs-target="#billing-personal-pane"')
        self.assertContains(team_response, 'id="billing-team-tab" data-bs-toggle="tab" data-bs-target="#billing-team-pane"')
        self.assertContains(team_response, 'class="detail-section billing-workspace tab-pane fade show active" id="billing-personal-pane"')
        self.assertContains(team_response, 'class="detail-section billing-workspace tab-pane fade" id="billing-team-pane"')
