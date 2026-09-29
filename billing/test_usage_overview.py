from datetime import timedelta
from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from properties.models import AIContent, Agency, AgencyInvitation, AgencyMembership, Property, RealtorProfile

from .models import AccountInventory, BillingAccount, DemoCreditGrant, PlanVersion, StoredFile, UsageOperation
from .overview_usage import format_storage_bytes
from .rules import Operation, collect_usage
from .services import AccountService, SubscriptionService, current_plan_version
from .test_overview import NOW, START
from .tests import seed_test_catalog
from .usage import begin_operation, finish_operation


class StorageDisplayTests(SimpleTestCase):
    def test_binary_units_preserve_known_zero_and_small_files(self):
        cases = ((None, "Объём уточняется"), (0, "0 МБ"), (1, "1 Б"), (1024, "1 КБ"),
                 (1024**2 // 2, "512 КБ"), (200 * 1024**2, "200 МБ"),
                 (2 * 1024**3, "2 ГБ"), (3 * 1024**2 // 2, "1,5 МБ"))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(format_storage_bytes(value), expected)

    def test_invalid_scope_cannot_count_unowned_or_mixed_records(self):
        for account in (BillingAccount(), BillingAccount(personal_user_id=1, agency_id=1)):
            with self.subTest(account=account), self.assertRaises(ValueError):
                collect_usage(account)


@override_settings(BILLING_SHADOW_ENABLED=False)
class BillingUsageOverviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_test_catalog()
        User = get_user_model()
        cls.user = User.objects.create_user("usage-overview-owner")
        cls.other = User.objects.create_user("usage-overview-other")
        cls.operator = User.objects.create_user("usage-overview-operator", is_superuser=True, is_staff=True)

    def setUp(self):
        self.client.force_login(self.user)
        self.account = AccountService.ensure_personal(self.user)

    def overview(self, *, now=NOW):
        with patch("billing.views.timezone.now", return_value=now):
            return self.client.get(reverse("billing:overview"))

    def workspace(self, *, now=NOW, team=False):
        return self.overview(now=now).context["billing_workspaces"][int(team)]

    def rows(self, workspace):
        return {row["key"]: row for row in workspace["resources"]}

    def property(self, *, account=None, owner=None, **values):
        return Property.objects.create(owner=owner or self.user, agency_id=(account or self.account).agency_id,
                                       title="Объект для учёта", **values)

    def grant(self):
        RealtorProfile.objects.create(user=self.user, email="owner@example.test", email_verified_at=NOW)
        return SubscriptionService.ensure_demo_grant(self.user)

    def period(self, *, account=None, plan="standard", starts_at=START):
        return SubscriptionService.grant_period(account=account or self.account, actor=self.operator,
            plan_version=current_plan_version(plan), starts_at=starts_at, request_key=uuid.uuid4(), reason="Тест интерфейса расхода")

    def team(self):
        agency = Agency.objects.create(owner=self.other, name="Команда расхода")
        AgencyMembership.objects.create(agency=agency, user=self.other, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.user, role="agent")
        return agency, AccountService.ensure_agency(agency)

    def operation(self, *, account=None, action="short_text", status="succeeded", now=NOW):
        account = account or self.account
        prop = self.property(account=account)
        operation = begin_operation(account=account, actor=self.user, action=Operation.AI_GENERATE,
            resource=prop, ai_action=action, now=now)
        if status == "succeeded":
            contents = [AIContent.objects.create(property=prop, content_type="headline", content="Тестовый результат")
                        for _ in range(3 if action == "package" else 7 if action == "landing" else 1)]
            finish_operation(operation, result_ids=[content.pk for content in contents],
                deltas={"ai_credits": operation.planned_credits}, now=now)
        elif status != "running":
            finish_operation(operation, status=status, now=now)
        return operation

    def test_resource_counts_use_scope_and_archive_rules_without_an_account(self):
        self.account.delete()
        self.property(status="draft", is_demo=True)
        self.property(status="sold", landing_published=True)
        self.property(status="archived", landing_published=True)
        self.property(owner=self.other, status="published", landing_published=True)
        before = BillingAccount.objects.count()
        rows = self.rows(self.workspace())
        self.assertEqual((rows["active_properties"]["used"], rows["landings"]["used"], rows["seats"]["used"]), (2, 1, 1))
        self.assertIn("В архиве: 1", rows["active_properties"]["hint"])
        self.assertIsNone(rows["storage_bytes"]["used"])
        self.assertEqual(BillingAccount.objects.count(), before)

    def test_counts_are_live_even_when_ledger_has_no_entries(self):
        prop = self.property()
        self.assertEqual(self.rows(self.workspace())["active_properties"]["used"], 1)
        Property.objects.filter(pk=prop.pk).update(status="archived")
        self.assertEqual(self.rows(self.workspace())["active_properties"]["used"], 0)
        self.assertFalse(self.account.usage_entries.exists())

    def test_team_counts_include_invitation_reservations_and_expiry(self):
        agency, account = self.team()
        self.property(account=account, owner=self.other)
        self.property()
        AgencyInvitation.objects.create(agency=agency, created_by=self.other, expires_at=NOW + timedelta(days=1))
        AgencyInvitation.objects.create(agency=agency, created_by=self.other, expires_at=NOW)
        AgencyInvitation.objects.create(agency=agency, created_by=self.other, expires_at=NOW + timedelta(days=1), accepted_by=self.user)
        rows = self.rows(self.workspace(team=True))
        self.assertEqual(rows["active_properties"]["used"], 1)
        self.assertEqual(rows["seats"]["used"], 3)
        self.assertIn("Действующие приглашения: 1", rows["seats"]["hint"])
        rows = self.rows(self.workspace(team=True, now=NOW + timedelta(days=1)))
        self.assertEqual(rows["seats"]["used"], 2)

    def test_storage_distinguishes_unknown_from_known_zero_and_keeps_orphans(self):
        self.assertIsNone(self.rows(self.workspace())["storage_bytes"]["used"])
        inventory = AccountInventory.objects.create(account=self.account, references_complete=True, files_complete=True, checked_at=NOW)
        self.assertEqual(self.rows(self.workspace())["storage_bytes"]["used_label"], "0 МБ")
        asset = StoredFile.objects.create(account=self.account, name="properties/private-orphan.jpg", size_bytes=3 * 1024**2,
                                         state=StoredFile.State.PRESENT)
        response = self.overview()
        row = self.rows(response.context["billing_workspaces"][0])["storage_bytes"]
        self.assertEqual((row["used"], row["used_label"]), (3 * 1024**2, "3 МБ"))
        self.assertNotContains(response, asset.name)
        inventory.references_complete = False
        inventory.save()
        self.assertIsNone(self.rows(self.workspace())["storage_bytes"]["used"])

    def test_unknown_file_size_never_becomes_zero_even_with_stale_inventory_flags(self):
        AccountInventory.objects.create(account=self.account, references_complete=True, files_complete=True)
        StoredFile.objects.create(account=self.account, name="properties/unknown.jpg")
        row = self.rows(self.workspace())["storage_bytes"]
        self.assertFalse(row["known"])
        self.assertEqual(row["used_label"], "Объём уточняется")

    def test_snapshot_limits_are_not_replaced_by_new_catalog_versions(self):
        self.period()
        version = current_plan_version("standard")
        terms = {name: getattr(version, name) for name in PlanVersion.TERMS}
        PlanVersion.objects.create(**{**terms, "version": 2, "active_properties_limit": 25})
        self.assertEqual(self.rows(self.workspace())["active_properties"]["limit"], 20)

    def test_over_limit_and_at_limit_are_informational_and_progress_is_clamped(self):
        for _ in range(3):
            self.property(landing_published=True)
        response = self.overview()
        rows = self.rows(response.context["billing_workspaces"][0])
        self.assertEqual((rows["active_properties"]["excess"], rows["active_properties"]["percent"]), (1, 100))
        self.assertContains(response, "Превышение: 1")
        self.assertNotContains(response, 'aria-valuenow="150"')
        prop = Property.objects.filter(owner=self.user).first()
        prop.status = "archived"
        prop.save()
        self.assertTrue(self.rows(self.workspace())["active_properties"]["at_limit"])

    def test_missing_demo_grant_does_not_show_unissued_credits_or_create_them(self):
        response = self.overview()
        self.assertEqual(response.context["billing_workspaces"][0]["ai"]["allocated"], 0)
        self.assertContains(response, "Подтвердите email")
        RealtorProfile.objects.create(user=self.user, email="owner@example.test", email_verified_at=NOW)
        response = self.overview()
        self.assertContains(response, "Разовый бонус Демо ещё не начислен")
        self.assertEqual(response.context["billing_workspaces"][0]["ai"]["remaining"], 0)
        self.assertFalse(DemoCreditGrant.objects.exists())

    def test_package_is_one_spend_and_pending_and_failed_calls_are_distinct(self):
        grant = self.grant()
        self.operation(action="package")
        self.operation(status="running")
        self.operation(status="unknown")
        self.operation(action="landing", status="failed")
        ai = self.workspace()["ai"]
        self.assertEqual((ai["allocated"], ai["spent"], ai["reserved"], ai["pending"], ai["remaining"]), (5, 3, 2, 0, 0))
        self.assertTrue(ai["unknown_operations"])
        self.assertFalse(ai["over_limit"])
        self.assertContains(self.overview(), "неизвестным исходом")
        grant.refresh_from_db()
        self.assertEqual((grant.credits_used, grant.credits_reserved), (3, 2))

    def test_demo_overspend_shows_zero_remaining_and_explicit_excess(self):
        self.grant()
        self.operation(action="landing")
        self.operation(action="package")
        ai = self.workspace()["ai"]
        self.assertEqual((ai["spent"], ai["remaining"], ai["excess"], ai["percent"]), (8, 0, 3, 100))
        self.assertTrue(ai["over_limit"])

    def test_paid_budget_does_not_mix_demo_and_counts_financial_fields_once(self):
        self.grant()
        self.operation(action="package", now=START - timedelta(days=1))
        period = self.period()
        period.ai_credits_used, period.ai_credits_reserved = 10, 2
        period.save()
        self.operation(action="package")
        self.operation(status="running")
        ai = self.workspace()["ai"]
        self.assertEqual((ai["allocated"], ai["spent"], ai["reserved"], ai["pending"], ai["remaining"]), (100, 13, 3, 0, 84))

    def test_grace_preserves_expired_period_spend_and_next_period_starts_fresh(self):
        period = self.period()
        self.operation(action="package")
        self.operation(status="running")
        ai = self.workspace(now=period.ends_at)["ai"]
        self.assertEqual((ai["spent"], ai["reserved"], ai["pending"], ai["remaining"]), (3, 1, 0, 96))
        self.assertEqual(ai["title"], "ИИ-кредиты")
        self.period(starts_at=period.ends_at)
        ai = self.workspace(now=period.ends_at)["ai"]
        self.assertEqual((ai["spent"], ai["pending"], ai["remaining"]), (0, 0, 100))

    def test_return_to_demo_keeps_bonus_and_previous_period_usage_separate(self):
        self.grant()
        self.operation(action="package", now=START - timedelta(days=1))
        period = self.period()
        self.operation(action="landing")
        workspace = self.workspace(now=period.ends_at + timedelta(days=7))
        self.assertEqual((workspace["ai"]["spent"], workspace["ai"]["remaining"]), (3, 2))
        self.assertEqual(workspace["period"]["ai"]["spent"], 5)
        self.assertEqual(self.rows(workspace)["active_properties"]["limit"], 2)

    def test_team_budget_is_shared_and_personal_grant_remains_personal(self):
        self.grant()
        self.operation(action="package", now=START - timedelta(days=1))
        _, account = self.team()
        self.assertEqual(self.workspace(team=True)["ai"]["allocated"], 0)
        self.period(account=account, plan="business")
        self.operation(account=account, action="landing")
        response = self.overview()
        personal, team = response.context["billing_workspaces"]
        self.assertEqual((personal["ai"]["allocated"], personal["ai"]["remaining"]), (5, 2))
        self.assertEqual((team["ai"]["allocated"], team["ai"]["spent"], team["ai"]["remaining"]), (500, 5, 495))

    def test_suspended_assigned_tariff_keeps_its_limits_and_credit_history(self):
        period = self.period(plan="business")
        self.operation(action="landing")
        subscription = period.subscription
        subscription.is_suspended, subscription.suspension_reason = True, "Тест"
        subscription.save()
        workspace = self.workspace()
        self.assertEqual(self.rows(workspace)["active_properties"]["limit"], 100)
        self.assertEqual((workspace["ai"]["allocated"], workspace["ai"]["spent"], workspace["ai"]["remaining"]), (500, 5, 495))

    def test_get_with_usage_is_read_only_and_never_requests_ai_or_scans_storage(self):
        self.grant()
        self.operation()

        def reads_only(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reads_only), patch("billing.inventory.measure_file", side_effect=AssertionError("storage access")), patch("properties.services.ai_service.requests.post", side_effect=AssertionError("provider request")):
            self.assertEqual(self.overview().status_code, 200)
        self.assertContains(self.overview(), "Учёт расхода сейчас выключен")

    def test_unknown_tariff_keeps_real_counts_without_fabricating_limits_or_ai_balance(self):
        self.property()
        PlanVersion.objects.filter(plan__code="demo").update(available=False)
        response = self.overview()
        workspace = response.context["billing_workspaces"][0]
        self.assertEqual(self.rows(workspace)["active_properties"]["used"], 1)
        self.assertIsNone(self.rows(workspace)["active_properties"]["limit"])
        self.assertFalse(workspace["ai"]["known"])
        self.assertContains(response, "Остаток уточняется")
