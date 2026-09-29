from copy import deepcopy
from datetime import datetime, timedelta, timezone as datetime_timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.apps import apps
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from properties.models import (
    AIContent, Agency, AgencyInvitation, AgencyMembership, Client, Lead,
    Property, PropertyImage, RealtorProfile,
)

from .catalog import DEFAULT_PLANS, default_ai_costs, validate_ai_costs, validate_snapshot
from .models import BillingAccount, BillingPeriod, DemoCreditGrant, Plan, PlanVersion, ResourceReservation, Subscription
from .rules import AccessState, Operation, PolicyService, UsageSnapshot, collect_usage
from .services import AccountService, SubscriptionService, current_plan_version, month_boundary, next_month_boundary, resource_scope, utc_datetime


UTC = datetime_timezone.utc
START = datetime(2026, 1, 31, 12, 15, tzinfo=UTC)
NOW = START + timedelta(days=1)


def seed_test_catalog():
    # Transactional migration tests elsewhere can flush the seeded catalog.
    for code, values in DEFAULT_PLANS.items():
        plan, _ = Plan.objects.get_or_create(code=code, defaults={"name": values["name"]})
        PlanVersion.objects.get_or_create(plan=plan, version=1, defaults={
            **{f"{key}_limit": values[key] for key in (
                "active_properties", "total_properties", "landings", "seats", "storage_bytes", "ai_credits", "demo_ai_credits",
            )},
            "xml_export_enabled": values["xml_export"], "team_enabled": values["team"],
            "monthly_price_minor": 0 if code == "demo" else None,
        })


def version_copy(version, **changes):
    values = {name: deepcopy(getattr(version, name)) for name in PlanVersion.TERMS}
    values["version"] += 1
    return PlanVersion(**{**values, **changes})


class CalendarTests(SimpleTestCase):
    def test_calendar_anchor_does_not_drift(self):
        self.assertEqual(month_boundary(START, 1), START.replace(month=2, day=28))
        self.assertEqual(month_boundary(START, 2), START.replace(month=3))
        leap = START.replace(year=2028)
        self.assertEqual(month_boundary(leap, 1).day, 29)
        self.assertEqual(month_boundary(leap, 2).day, 31)

    def test_next_boundary_at_and_between_calendar_boundaries(self):
        self.assertEqual(next_month_boundary(START, START), month_boundary(START, 1))
        self.assertEqual(next_month_boundary(month_boundary(START, 1), START), month_boundary(START, 2))
        self.assertEqual(next_month_boundary(START.replace(month=3, day=2), START), month_boundary(START, 2))

    def test_dates_are_normalized_to_utc(self):
        local = START.astimezone(datetime_timezone(timedelta(hours=3)))
        self.assertEqual(utc_datetime(local), START)
        self.assertEqual(month_boundary(local, 2), START.replace(month=3))

    def test_invalid_calendar_arguments_rejected(self):
        for value in (datetime(2026, 1, 1), None, "2026-01-01"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                utc_datetime(value)
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                month_boundary(START, value)
        with self.assertRaises(ValueError):
            next_month_boundary(START - timedelta(seconds=1), START)

    def test_usage_rejects_negative_fractional_and_boolean_counters(self):
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                UsageSnapshot(storage_bytes=value)

    def test_ai_costs_are_complete_nonnegative_integer_weights(self):
        validate_ai_costs(default_ai_costs())
        for costs in ({}, {**default_ai_costs(), "audit": 1}, {**default_ai_costs(), "package": True}, {**default_ai_costs(), "landing": 0}):
            with self.subTest(costs=costs), self.assertRaises(ValidationError):
                validate_ai_costs(costs)


class BillingFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_test_catalog()
        User = get_user_model()
        cls.owner = User.objects.create_user("billing-owner")
        cls.manager = User.objects.create_user("billing-manager")
        cls.agent = User.objects.create_user("billing-agent")
        cls.outsider = User.objects.create_user("billing-outsider")
        cls.operator = User.objects.create_user("billing-operator", is_staff=True, is_superuser=True)
        cls.agency = Agency.objects.create(name="Тестовое агентство", owner=cls.owner)
        cls.owner_membership = AgencyMembership.objects.create(agency=cls.agency, user=cls.owner, role="owner")
        cls.manager_membership = AgencyMembership.objects.create(agency=cls.agency, user=cls.manager, role="manager")
        cls.agent_membership = AgencyMembership.objects.create(agency=cls.agency, user=cls.agent, role="agent")
        cls.personal = AccountService.ensure_personal(cls.owner)
        cls.team = AccountService.ensure_agency(cls.agency)
        cls.demo = current_plan_version(Plan.Code.DEMO)
        cls.standard = current_plan_version(Plan.Code.STANDARD)
        cls.business = current_plan_version(Plan.Code.BUSINESS)

    def grant(self, *, account=None, version=None, start=START, end=None, key=None, **kwargs):
        return SubscriptionService.grant_period(
            account=account or self.personal, plan_version=version or self.standard,
            actor=self.operator, reason="Пилот для тестирования", request_key=key or uuid.uuid4(),
            starts_at=start, ends_at=end, **kwargs,
        )

    def property(self, *, team=False, owner=None, **kwargs):
        return Property.objects.create(owner=owner or self.owner, agency=self.agency if team else None, title="Тестовый объект", **kwargs)

    def invitation(self, **kwargs):
        return AgencyInvitation.objects.create(**{
            "agency": self.agency, "created_by": self.owner,
            "expires_at": NOW + timedelta(days=7), **kwargs,
        })

    def verify_owner_email(self):
        return RealtorProfile.objects.create(user=self.owner, email="owner@example.test", email_verified_at=NOW)


class CatalogAndModelTests(BillingFixture):
    def test_migration_seed_is_repeatable_and_does_not_provision_users(self):
        migration = import_module("billing.migrations.0002_seed_plan_catalog")
        before = (BillingAccount.objects.count(), Subscription.objects.count(), DemoCreditGrant.objects.count())
        for _ in range(2):
            migration.seed_catalog(apps, SimpleNamespace(connection=connection))
        self.assertEqual(Plan.objects.count(), 3)
        self.assertEqual(PlanVersion.objects.count(), 3)
        self.assertEqual(before, (BillingAccount.objects.count(), Subscription.objects.count(), DemoCreditGrant.objects.count()))
        self.assertEqual((self.demo.active_properties_limit, self.demo.demo_ai_credits_limit), (2, 5))
        self.assertEqual((self.standard.ai_credits_limit, self.business.seats_limit), (100, 5))

    def test_account_requires_exactly_one_workspace(self):
        for values in ({}, {"personal_user": self.outsider, "agency": self.agency}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                BillingAccount.objects.create(**values)

    def test_account_scope_constraint_also_protects_bulk_inserts(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            BillingAccount.objects.bulk_create([BillingAccount()])

    def test_provisioning_is_idempotent_and_does_not_move_resources(self):
        prop = self.property()
        self.assertEqual(AccountService.ensure_personal(self.owner).pk, self.personal.pk)
        self.assertEqual(AccountService.ensure_agency(self.agency).pk, self.team.pk)
        prop.refresh_from_db()
        self.assertIsNone(prop.agency_id)
        self.assertNotEqual(self.personal.pk, self.team.pk)

    def test_payer_is_not_owner_and_can_be_changed_independently(self):
        self.personal.payer = self.outsider
        self.personal.save()
        self.assertEqual(self.personal.owner_id, self.owner.pk)
        self.assertFalse(PolicyService.evaluate(self.personal, self.outsider, Operation.VIEW).allowed)

    def test_account_and_plan_history_cannot_be_cascade_deleted(self):
        period = self.grant()
        for record in (self.personal, period.subscription, self.standard, self.standard.plan):
            with self.subTest(record=type(record).__name__), self.assertRaises(ProtectedError):
                record.delete()

    def test_tariff_terms_are_immutable_but_availability_is_editable(self):
        self.standard.active_properties_limit = 21
        with self.assertRaises(ValidationError):
            self.standard.save()
        self.standard.refresh_from_db()
        self.standard.available = False
        self.standard.save(update_fields=["available"])
        self.assertIsNone(current_plan_version(Plan.Code.STANDARD))

    def test_plan_code_is_immutable(self):
        self.standard.plan.code = "business"
        with self.assertRaises(ValidationError):
            self.standard.plan.save()

    def test_existing_terms_cannot_be_replaced_with_a_new_instance_using_same_pk(self):
        replacement = version_copy(self.standard, ai_credits_limit=1000)
        replacement.pk = self.standard.pk
        with self.assertRaises(ValidationError):
            replacement.save()

    def test_catalog_versions_are_unique_and_latest_available_is_selected(self):
        second = version_copy(self.standard, ai_credits_limit=120)
        second.save()
        self.assertEqual(current_plan_version(Plan.Code.STANDARD).pk, second.pk)
        with self.assertRaises(ValidationError):
            version_copy(self.standard).save()
        second.available = False
        second.save()
        self.assertEqual(current_plan_version(Plan.Code.STANDARD).pk, self.standard.pk)

    def test_inconsistent_limits_invalid_currency_and_zero_paid_price_rejected(self):
        for changes in (
            {"seats_limit": 0}, {"total_properties_limit": 1}, {"landings_limit": 21},
            {"monthly_price_minor": 0}, {"monthly_price_minor": 0, "currency": "RUB"},
            {"monthly_price_minor": 150000, "currency": "rub"}, {"currency": "RUB"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                version_copy(self.standard, **changes).save()

    def test_paid_plans_have_no_sales_price_until_explicitly_versioned(self):
        self.assertIsNone(self.standard.monthly_price_minor)
        self.assertFalse(self.standard.sale_ready)
        priced = version_copy(self.standard, monthly_price_minor=150000, currency="RUB")
        priced.save()
        self.assertTrue(priced.sale_ready)
        self.assertFalse(self.demo.sale_ready)

    def test_snapshot_is_complete_and_independent_of_mutable_dicts(self):
        snapshot = self.standard.snapshot()
        validate_snapshot(snapshot)
        snapshot["ai_costs"]["package"] = 99
        self.assertEqual(self.standard.ai_costs["package"], 3)
        for bad in ({}, {**snapshot, "schema": True}, {**snapshot, "limits": {}}, {**snapshot, "features": {"team": 1, "xml_export": True}}):
            with self.subTest(snapshot=bad), self.assertRaises(ValidationError):
                validate_snapshot(bad)

    def test_subscription_account_is_immutable_and_suspension_needs_reason(self):
        subscription = self.grant().subscription
        subscription.account = self.team
        with self.assertRaises(ValidationError):
            subscription.save()
        subscription.refresh_from_db()
        subscription.is_suspended = True
        with self.assertRaises(ValidationError):
            subscription.save()

    def test_period_snapshots_and_credit_budget_are_created_automatically(self):
        period = self.grant()
        self.assertEqual(period.terms_snapshot, self.standard.snapshot())
        self.assertEqual(period.ai_credits_allocated, 100)
        self.assertEqual(period.ends_at, month_boundary(START, 1))
        self.assertEqual(period.source, BillingPeriod.Source.PILOT)

    def test_period_terms_and_grant_budget_cannot_be_edited(self):
        period = self.grant()
        period.ends_at += timedelta(days=1)
        with self.assertRaises(ValidationError):
            period.save()
        period.refresh_from_db()
        period.ai_credits_allocated = 200
        with self.assertRaises(ValidationError):
            period.save()

    def test_period_requires_positive_duration_and_aware_dates(self):
        for end in (START, START - timedelta(seconds=1), START.replace(tzinfo=None)):
            with self.subTest(end=end), self.assertRaises(ValidationError):
                self.grant(end=end)

    def test_period_requires_real_foreign_keys_and_matching_snapshot(self):
        with self.assertRaises(ValidationError):
            BillingPeriod(starts_at=START, ends_at=month_boundary(START, 1), reason="Пилот").save()
        period = self.grant()
        with self.assertRaises(ValidationError):
            BillingPeriod.objects.create(subscription=period.subscription, plan_version=self.standard, starts_at=period.ends_at, ends_at=month_boundary(START, 2), terms_snapshot=self.business.snapshot(), reason="Пилот")

    def test_ai_budget_constraints_protect_model_and_database_writes(self):
        period = self.grant()
        period.ai_credits_used, period.ai_credits_reserved = 99, 2
        with self.assertRaises(ValidationError):
            period.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            BillingPeriod.objects.filter(pk=period.pk).update(ai_credits_used=99, ai_credits_reserved=2)

    def test_admin_does_not_allow_manual_quota_edits_or_history_deletion(self):
        request = RequestFactory().get("/admin/")
        request.user = self.operator
        for model in (BillingAccount, Subscription, BillingPeriod, DemoCreditGrant):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))
        readonly = admin.site._registry[PlanVersion].get_readonly_fields(request, self.standard)
        self.assertIn("ai_credits_limit", readonly)
        self.assertIn("monthly_price_minor", readonly)
        self.assertNotIn("available", readonly)


class SubscriptionTests(BillingFixture):
    def test_only_billing_operator_can_grant_period(self):
        for actor in (self.owner, self.outsider, None):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                SubscriptionService.grant_period(account=self.personal, plan_version=self.standard, actor=actor, reason="Пилот", request_key=uuid.uuid4(), starts_at=START)
        self.assertFalse(Subscription.objects.exists())

    def test_invalid_key_naive_dates_reason_and_payment_source_rejected(self):
        common = dict(account=self.personal, plan_version=self.standard, actor=self.operator, reason="Пилот", request_key=uuid.uuid4(), starts_at=START)
        for change in ({"request_key": "invalid"}, {"starts_at": START.replace(tzinfo=None)}, {"reason": " "}, {"source": "paid"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                SubscriptionService.grant_period(**{**common, **change})
        self.assertFalse(Subscription.objects.exists())

    def test_demo_and_retired_version_cannot_be_granted(self):
        with self.assertRaises(ValidationError):
            self.grant(version=self.demo)
        self.standard.available = False
        self.standard.save()
        with self.assertRaises(ValidationError):
            self.grant()

    def test_idempotent_grant_does_not_reset_usage_or_require_available_version(self):
        key = uuid.uuid4()
        period = self.grant(key=key)
        period.ai_credits_used = 10
        period.save(update_fields=["ai_credits_used"])
        self.standard.available = False
        self.standard.save()
        repeat = self.grant(key=key)
        self.assertEqual(repeat.pk, period.pk)
        self.assertEqual(repeat.ai_credits_used, 10)
        self.assertEqual(BillingPeriod.objects.count(), 1)

    def test_request_key_cannot_be_reused_for_another_account_or_plan(self):
        key = uuid.uuid4()
        self.grant(key=key)
        for changes in ({"account": self.team}, {"version": self.business}, {"start": START + timedelta(seconds=1)}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.grant(key=key, **changes)

    def test_live_periods_cannot_overlap_but_adjacent_periods_can(self):
        first = self.grant()
        with self.assertRaises(ValidationError):
            self.grant(start=START + timedelta(days=1))
        second = self.grant(start=first.ends_at)
        self.assertEqual(second.ends_at, month_boundary(START, 2))

    def test_early_renewal_and_scheduled_downgrade_preserve_current_usage(self):
        first = self.grant(version=self.business)
        first.ai_credits_used = 450
        first.save(update_fields=["ai_credits_used"])
        subscription = first.subscription
        subscription.scheduled_plan_version = self.standard
        subscription.cancel_at_period_end = True
        subscription.save()
        second = self.grant(start=first.ends_at)
        current = PolicyService.context(self.personal, now=NOW)
        self.assertEqual((current.period_id, current.ai_used, current.ai_allocated), (first.pk, 450, 500))
        future = PolicyService.context(self.personal, now=first.ends_at)
        self.assertEqual((future.period_id, future.ai_used, future.ai_allocated), (second.pk, 0, 100))
        self.assertEqual(future.terms["plan_code"], "standard")

    def test_half_open_period_grace_and_fallback_boundaries(self):
        period = self.grant()
        self.assertEqual(PolicyService.context(self.personal, now=START - timedelta(microseconds=1)).state, AccessState.DEMO)
        self.assertEqual(PolicyService.context(self.personal, now=START).state, AccessState.ACTIVE)
        self.assertEqual(PolicyService.context(self.personal, now=period.ends_at).state, AccessState.GRACE)
        grace_end = period.ends_at + timedelta(days=7)
        self.assertEqual(PolicyService.context(self.personal, now=grace_end - timedelta(microseconds=1)).state, AccessState.GRACE)
        context = PolicyService.context(self.personal, now=grace_end)
        self.assertEqual((context.state, context.terms["plan_code"]), (AccessState.RESTRICTED, "demo"))

    def test_period_snapshot_survives_catalog_change_or_retirement(self):
        period = self.grant()
        changed = version_copy(self.standard, ai_credits_limit=120)
        changed.save()
        self.standard.available = False
        self.standard.save()
        context = PolicyService.context(self.personal, now=NOW)
        self.assertEqual(context.terms["limits"]["ai_credits"], 100)
        self.assertEqual(context.period_id, period.pk)

    def test_reactivation_after_grace_has_a_new_anchor(self):
        first = self.grant()
        start = first.ends_at + timedelta(days=10)
        second = self.grant(start=start)
        second.subscription.refresh_from_db()
        self.assertEqual(second.subscription.anchor_at, start)
        self.assertEqual(second.ends_at, month_boundary(start, 1))

    def test_renewal_in_grace_uses_original_boundary_when_granted_from_old_end(self):
        first = self.grant()
        self.assertEqual(PolicyService.context(self.personal, now=first.ends_at + timedelta(days=2)).state, AccessState.GRACE)
        second = self.grant(start=first.ends_at)
        self.assertEqual(second.ends_at, month_boundary(START, 2))
        self.assertEqual(second.subscription.anchor_at, START)

    def test_void_requires_reason_is_irreversible_and_allows_replacement(self):
        first = self.grant()
        first.voided_at = NOW
        with self.assertRaises(ValidationError):
            first.save()
        first.void_reason = "Ошибочное назначение"
        first.save()
        replacement = self.grant()
        self.assertNotEqual(first.pk, replacement.pk)
        first.voided_at = None
        with self.assertRaises(ValidationError):
            first.save()

    def test_suspended_subscription_cannot_receive_new_access(self):
        period = self.grant()
        subscription = period.subscription
        subscription.is_suspended, subscription.suspension_reason = True, "Проверка оператором"
        subscription.save()
        with self.assertRaises(ValidationError):
            self.grant(start=period.ends_at)


class DemoGrantTests(BillingFixture):
    def test_email_confirmation_required_and_failure_does_not_issue_grant(self):
        with self.assertRaises(ValidationError):
            SubscriptionService.ensure_demo_grant(self.owner)
        self.assertFalse(DemoCreditGrant.objects.exists())

    def test_demo_grant_is_lifetime_and_not_reset_by_email_reconfirmation(self):
        profile = self.verify_owner_email()
        grant = SubscriptionService.ensure_demo_grant(self.owner)
        grant.credits_used = 3
        grant.save()
        profile.email = "another@example.test"
        profile.save()
        self.assertIsNone(profile.email_verified_at)
        same = SubscriptionService.ensure_demo_grant(self.owner)
        self.assertEqual((same.pk, same.credits_used, same.credits_allocated), (grant.pk, 3, 5))
        self.assertEqual(DemoCreditGrant.objects.count(), 1)

    def test_paid_period_does_not_add_demo_credits_and_fallback_preserves_residual(self):
        self.verify_owner_email()
        grant = SubscriptionService.ensure_demo_grant(self.owner)
        grant.credits_used = 3
        grant.save()
        period = self.grant()
        current = PolicyService.context(self.personal, now=NOW)
        self.assertEqual((current.ai_allocated, current.ai_used), (100, 0))
        fallback = PolicyService.context(self.personal, now=period.ends_at + timedelta(days=7))
        self.assertEqual((fallback.ai_allocated, fallback.ai_used), (5, 3))

    def test_team_never_uses_owners_personal_demo_grant(self):
        self.verify_owner_email()
        SubscriptionService.ensure_demo_grant(self.owner)
        self.assertEqual(PolicyService.context(self.team, now=NOW).ai_allocated, 0)
        self.assertFalse(PolicyService.evaluate(self.team, self.owner, Operation.AI_GENERATE, ai_action="short_text", now=NOW).allowed)

    def test_demo_grant_rejects_other_owner_agency_changed_budget_and_overdraft(self):
        snapshot = self.demo.snapshot()
        with self.assertRaises(ValidationError):
            DemoCreditGrant.objects.create(user=self.owner, account=self.team, terms_snapshot=snapshot, credits_allocated=5)
        self.verify_owner_email()
        grant = SubscriptionService.ensure_demo_grant(self.owner)
        for changes in ({"credits_allocated": 6}, {"credits_used": 4, "credits_reserved": 2}):
            grant.refresh_from_db()
            for name, value in changes.items():
                setattr(grant, name, value)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                grant.save()

    def test_new_demo_version_does_not_refill_or_reprice_existing_grant(self):
        self.verify_owner_email()
        grant = SubscriptionService.ensure_demo_grant(self.owner)
        costs = default_ai_costs()
        costs["package"] = 4
        changed = version_copy(self.demo, demo_ai_credits_limit=10, ai_costs=costs)
        changed.save()
        context = PolicyService.context(self.personal, now=NOW)
        self.assertEqual(context.ai_allocated, 5)
        self.assertEqual(context.terms["ai_costs"]["package"], 3)
        self.assertEqual(SubscriptionService.ensure_demo_grant(self.owner).pk, grant.pk)


class PolicyTests(BillingFixture):
    def test_scope_follows_resource_not_actor_or_combined_workspace(self):
        private = self.property()
        shared = self.property(team=True, owner=self.agent)
        lead = Lead.objects.create(property=shared, name="Клиент", phone="123")
        image = PropertyImage.objects.create(property=shared, image="properties/test.webp")
        content = AIContent.objects.create(property=shared, content_type="headline", content="Черновик")
        client = Client.objects.create(owner=self.agent, agency=self.agency, name="Клиент", phone="456")
        self.assertEqual(AccountService.for_resource(private).pk, self.personal.pk)
        for resource in (shared, lead, image, content, client, self.agent_membership, self.invitation()):
            with self.subTest(resource=type(resource).__name__):
                self.assertEqual(AccountService.for_resource(resource).pk, self.team.pk)
                self.assertFalse(AccountService.matches(self.personal, resource))
        with self.assertRaises(ValueError):
            resource_scope(self.owner)

    def test_record_counts_include_drafts_sold_rented_demo_but_not_archived_active(self):
        for status in ("draft", "sold", "rented", "archived"):
            self.property(status=status, landing_published=True, is_demo=True)
        self.property(team=True, landing_published=True)
        self.property(owner=self.outsider)
        usage = collect_usage(self.personal, now=NOW)
        self.assertEqual((usage.active_properties, usage.total_properties, usage.landings), (3, 4, 3))
        self.assertEqual(collect_usage(self.team, now=NOW).total_properties, 1)
        self.assertIsNone(usage.storage_bytes)

    def test_only_valid_pending_invites_reserve_seats(self):
        self.invitation()
        self.invitation(expires_at=NOW)
        self.invitation(accepted_by=self.agent)
        usage = collect_usage(self.team, now=NOW)
        self.assertEqual((usage.occupied_seats, usage.invited_seats), (3, 1))

    def test_evaluation_is_read_only_and_does_not_create_accounts_or_subscriptions(self):
        counts = (BillingAccount.objects.count(), Subscription.objects.count(), BillingPeriod.objects.count(), DemoCreditGrant.objects.count())
        for _ in range(2):
            PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_PROPERTY, now=NOW)
        self.assertEqual(counts, (BillingAccount.objects.count(), Subscription.objects.count(), BillingPeriod.objects.count(), DemoCreditGrant.objects.count()))

    def test_authorization_is_preserved_even_for_payers_and_superusers(self):
        self.personal.payer = self.outsider
        self.personal.save()
        for actor in (self.agent, self.outsider, self.operator, AnonymousUser(), None):
            with self.subTest(actor=actor):
                result = PolicyService.evaluate(self.personal, actor, Operation.EDIT_CRM, now=NOW)
                self.assertFalse(result.allowed)
                self.assertEqual(result.code, "forbidden")
        wrong = PolicyService.evaluate(self.personal, self.owner, Operation.EDIT_CRM, resource=self.property(team=True), now=NOW)
        self.assertEqual(wrong.code, "wrong_workspace")

    def test_existing_data_edit_and_csv_remain_accessible_when_over_quota(self):
        for _ in range(3):
            self.property()
        self.assertFalse(PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_PROPERTY, now=NOW).allowed)
        for operation in (Operation.VIEW, Operation.CSV_EXPORT, Operation.EDIT_CRM, Operation.REMOVE_FILES):
            with self.subTest(operation=operation):
                self.assertTrue(PolicyService.evaluate(self.personal, self.owner, operation, now=NOW).allowed)

    def test_active_and_total_limits_and_reservations_are_checked(self):
        base = dict(active_properties=1, total_properties=1)
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_PROPERTY, usage=UsageSnapshot(**base), now=NOW).allowed)
        for usage in (UsageSnapshot(**base, active_properties_reserved=1), UsageSnapshot(active_properties=0, total_properties=49, total_properties_reserved=1), UsageSnapshot(active_properties=1, total_properties=50)):
            with self.subTest(usage=usage):
                result = PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_PROPERTY, usage=usage, now=NOW)
                self.assertEqual(result.code, "quota_exceeded")
        self.assertFalse(PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_PROPERTY, quantity=2, usage=UsageSnapshot(**base), now=NOW).allowed)

    def test_archive_allowed_and_restore_needs_capacity_without_republishing(self):
        prop = self.property(status="archived", landing_published=False)
        full = UsageSnapshot(active_properties=2, total_properties=50, landings=0)
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.ARCHIVE_PROPERTY, resource=prop, usage=full, now=NOW).allowed)
        self.assertFalse(PolicyService.evaluate(self.personal, self.owner, Operation.RESTORE_PROPERTY, resource=prop, usage=full, now=NOW).allowed)
        free = UsageSnapshot(active_properties=1, total_properties=50, landings=0)
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.RESTORE_PROPERTY, resource=prop, usage=free, now=NOW).allowed)
        prop.refresh_from_db()
        self.assertEqual(prop.status, "archived")
        self.assertFalse(prop.landing_published)

    def test_landings_new_publication_and_existing_retry(self):
        first = self.property(landing_published=True)
        second = self.property()
        self.assertFalse(PolicyService.evaluate(self.personal, self.owner, Operation.PUBLISH_LANDING, resource=second, now=NOW).allowed)
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.PUBLISH_LANDING, resource=first, now=NOW).allowed)
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.UNPUBLISH_LANDING, resource=first, now=NOW).allowed)

    def test_public_landing_checks_publication_and_pauses_all_on_unselected_excess(self):
        first = self.property(landing_published=True)
        self.assertTrue(PolicyService.evaluate(self.personal, None, Operation.PUBLIC_LANDING, resource=first, now=NOW).allowed)
        second = self.property(landing_published=True)
        for prop in (first, second):
            result = PolicyService.evaluate(self.personal, None, Operation.PUBLIC_LANDING, resource=prop, now=NOW)
            self.assertEqual(result.code, "landing_selection_required")
        third = self.property()
        self.assertEqual(PolicyService.evaluate(self.personal, None, Operation.PUBLIC_LANDING, resource=third, now=NOW).code, "landing_unavailable")

    def test_unknown_storage_fails_closed_and_known_bytes_include_reservations(self):
        unknown = PolicyService.evaluate(self.personal, self.owner, Operation.UPLOAD_FILES, quantity=1, now=NOW)
        self.assertEqual((unknown.code, unknown.used), ("usage_unknown", None))
        limit = self.demo.storage_bytes_limit
        usage = UsageSnapshot(storage_bytes=limit - 2, storage_reserved_bytes=1)
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.UPLOAD_FILES, quantity=1, usage=usage, now=NOW).allowed)
        self.assertFalse(PolicyService.evaluate(self.personal, self.owner, Operation.UPLOAD_FILES, quantity=2, usage=usage, now=NOW).allowed)

    def test_ai_weight_counts_operation_not_generated_content_rows(self):
        period = self.grant()
        prop = self.property()
        for _ in range(7):
            AIContent.objects.create(property=prop, content_type="headline", content="Текст")
        result = PolicyService.evaluate(self.personal, self.owner, Operation.AI_GENERATE, resource=prop, ai_action="package", now=NOW)
        self.assertEqual((result.allowed, result.ai_cost, result.used, result.required), (True, 3, 0, 3))
        period.refresh_from_db()
        self.assertEqual((period.ai_credits_used, period.ai_credits_reserved), (0, 0))

    def test_ai_uses_period_spend_reservations_and_bulk_quantity(self):
        period = self.grant()
        period.ai_credits_used, period.ai_credits_reserved = 95, 3
        period.save()
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.AI_GENERATE, ai_action="full_description", now=NOW).allowed)
        result = PolicyService.evaluate(self.personal, self.owner, Operation.AI_GENERATE, ai_action="package", now=NOW)
        self.assertEqual((result.allowed, result.code, result.used), (False, "quota_exceeded", 98))
        self.assertFalse(PolicyService.evaluate(self.personal, self.owner, Operation.AI_GENERATE, ai_action="short_text", quantity=3, now=NOW).allowed)

    def test_demo_generation_needs_grant_but_local_audit_is_free(self):
        self.assertFalse(PolicyService.evaluate(self.personal, self.owner, Operation.AI_GENERATE, ai_action="short_text", now=NOW).allowed)
        result = PolicyService.evaluate(self.personal, self.owner, Operation.AI_GENERATE, ai_action="audit", now=NOW)
        self.assertEqual((result.allowed, result.ai_cost), (True, 0))
        self.verify_owner_email()
        SubscriptionService.ensure_demo_grant(self.owner)
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.AI_GENERATE, ai_action="landing", now=NOW).allowed)

    def test_xml_requires_paid_period_but_csv_is_always_available(self):
        self.assertFalse(PolicyService.evaluate(self.personal, self.owner, Operation.XML_EXPORT, now=NOW).allowed)
        self.grant()
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.XML_EXPORT, now=NOW).allowed)
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.CSV_EXPORT, now=NOW).allowed)

    def test_grace_allows_existing_crm_and_public_landing_but_no_growth_ai_or_xml(self):
        period = self.grant()
        prop = self.property(landing_published=True)
        now = period.ends_at + timedelta(days=1)
        for operation in (Operation.EDIT_CRM, Operation.CSV_EXPORT, Operation.ARCHIVE_PROPERTY, Operation.UNPUBLISH_LANDING):
            with self.subTest(operation=operation):
                self.assertTrue(PolicyService.evaluate(self.personal, self.owner, operation, resource=prop, now=now).allowed)
        self.assertTrue(PolicyService.evaluate(self.personal, None, Operation.PUBLIC_LANDING, resource=prop, now=now).allowed)
        for operation in (Operation.CREATE_PROPERTY, Operation.UPLOAD_FILES, Operation.AI_GENERATE, Operation.XML_EXPORT):
            with self.subTest(operation=operation):
                self.assertEqual(PolicyService.evaluate(self.personal, self.owner, operation, ai_action="short_text", now=now).code, "grace_no_new_resources")

    def test_team_members_share_period_and_cannot_spend_personal_quota(self):
        period = self.grant(account=self.team, version=self.business)
        period.ai_credits_used = 498
        period.save()
        prop = self.property(team=True, owner=self.agent)
        for actor in (self.owner, self.manager, self.agent):
            with self.subTest(actor=actor):
                decision = PolicyService.evaluate(self.team, actor, Operation.AI_GENERATE, resource=prop, ai_action="package", now=NOW)
                self.assertEqual((decision.allowed, decision.used, decision.limit), (False, 498, 500))
        self.assertFalse(PolicyService.evaluate(self.personal, self.agent, Operation.AI_GENERATE, resource=prop, ai_action="short_text", now=NOW).allowed)

    def test_team_fallback_retains_reading_but_only_owner_edits(self):
        prop = self.property(team=True)
        for actor in (self.manager, self.agent):
            with self.subTest(actor=actor):
                self.assertTrue(PolicyService.evaluate(self.team, actor, Operation.VIEW, resource=prop, now=NOW).allowed)
                self.assertTrue(PolicyService.evaluate(self.team, actor, Operation.CSV_EXPORT, now=NOW).allowed)
                self.assertEqual(PolicyService.evaluate(self.team, actor, Operation.EDIT_CRM, now=NOW).code, "team_read_only")
        self.assertTrue(PolicyService.evaluate(self.team, self.owner, Operation.EDIT_CRM, now=NOW).allowed)
        self.assertFalse(PolicyService.evaluate(self.team, self.owner, Operation.INVITE_MEMBER, now=NOW).allowed)
        self.agent.refresh_from_db()
        self.assertTrue(self.agent.is_active)

    def test_team_roles_still_apply_with_business(self):
        self.grant(account=self.team, version=self.business)
        prop = self.property(team=True)
        self.assertFalse(PolicyService.evaluate(self.team, self.agent, Operation.ARCHIVE_PROPERTY, resource=prop, now=NOW).allowed)
        self.assertTrue(PolicyService.evaluate(self.team, self.manager, Operation.ARCHIVE_PROPERTY, resource=prop, now=NOW).allowed)
        self.assertTrue(PolicyService.evaluate(self.team, self.manager, Operation.INVITE_MEMBER, invited_role="agent", now=NOW).allowed)
        self.assertFalse(PolicyService.evaluate(self.team, self.manager, Operation.INVITE_MEMBER, invited_role="manager", now=NOW).allowed)
        self.assertFalse(PolicyService.evaluate(self.team, self.manager, Operation.REMOVE_MEMBER, resource=self.agent_membership, now=NOW).allowed)
        self.assertTrue(PolicyService.evaluate(self.team, self.owner, Operation.REMOVE_MEMBER, resource=self.agent_membership, now=NOW).allowed)
        self.assertFalse(PolicyService.evaluate(self.team, self.owner, Operation.REMOVE_MEMBER, resource=self.owner_membership, now=NOW).allowed)
        self.assertTrue(PolicyService.evaluate(self.team, self.owner, Operation.CHANGE_MEMBER_ROLE, resource=self.agent_membership, invited_role="manager", now=NOW).allowed)
        self.assertFalse(PolicyService.evaluate(self.team, self.owner, Operation.CHANGE_MEMBER_ROLE, resource=self.agent_membership, invited_role="owner", now=NOW).allowed)

    def test_agency_creation_requires_business_and_does_not_create_team_itself(self):
        account = AccountService.ensure_personal(self.outsider)
        self.assertFalse(PolicyService.evaluate(account, self.outsider, Operation.CREATE_AGENCY, now=NOW).allowed)
        self.grant(account=account, version=self.business)
        self.assertTrue(PolicyService.evaluate(account, self.outsider, Operation.CREATE_AGENCY, now=NOW).allowed)
        self.grant(version=self.business)
        self.assertFalse(PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_AGENCY, now=NOW).allowed)
        self.assertFalse(Agency.objects.filter(owner=self.outsider).exists())

    def test_invite_capacity_acceptance_replaces_reserved_seat(self):
        self.grant(account=self.team, version=self.business)
        invitation = self.invitation()
        self.invitation()
        self.assertFalse(PolicyService.evaluate(self.team, self.owner, Operation.INVITE_MEMBER, now=NOW).allowed)
        accepted = PolicyService.evaluate(self.team, self.outsider, Operation.ACCEPT_INVITE, resource=invitation, now=NOW)
        self.assertEqual((accepted.allowed, accepted.used, accepted.required), (True, 5, 0))
        self.invitation()
        self.assertFalse(PolicyService.evaluate(self.team, self.outsider, Operation.ACCEPT_INVITE, resource=invitation, now=NOW).allowed)

    def test_expired_accepted_and_existing_member_invites_are_rejected(self):
        self.grant(account=self.team, version=self.business)
        expired = self.invitation(expires_at=NOW)
        accepted = self.invitation(accepted_by=self.agent)
        valid = self.invitation()
        for actor, invitation in ((self.outsider, expired), (self.outsider, accepted), (self.agent, valid)):
            with self.subTest(actor=actor, invitation=invitation.pk):
                self.assertEqual(PolicyService.evaluate(self.team, actor, Operation.ACCEPT_INVITE, resource=invitation, now=NOW).code, "invalid_invitation")
        self.assertEqual(PolicyService.evaluate(self.team, self.agent, Operation.ACCEPT_INVITE, resource=accepted, now=NOW).code, "already_accepted")

    def test_revoked_members_do_not_regain_access_from_an_accepted_invite(self):
        accepted = self.invitation(accepted_by=self.agent)
        self.agent_membership.delete()
        self.assertFalse(PolicyService.evaluate(self.team, self.agent, Operation.ACCEPT_INVITE, resource=accepted, now=NOW).allowed)
        self.assertFalse(PolicyService.evaluate(self.team, self.agent, Operation.VIEW, now=NOW).allowed)

    def test_suspended_access_preserves_data_read_and_csv_not_new_actions(self):
        period = self.grant()
        subscription = period.subscription
        subscription.is_suspended, subscription.suspension_reason = True, "Проверка"
        subscription.save()
        for operation in (Operation.VIEW, Operation.CSV_EXPORT, Operation.MANAGE_BILLING):
            self.assertTrue(PolicyService.evaluate(self.personal, self.owner, operation, now=NOW).allowed)
        self.assertEqual(PolicyService.evaluate(self.personal, self.owner, Operation.EDIT_CRM, now=NOW).code, "subscription_suspended")
        self.assertEqual(PolicyService.context(self.personal, now=NOW).state, AccessState.SUSPENDED)

    def test_invalid_configuration_fails_closed_but_data_export_remains(self):
        self.demo.available = False
        self.demo.save()
        result = PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_PROPERTY, now=NOW)
        self.assertEqual(result.code, "configuration_error")
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.CSV_EXPORT, now=NOW).allowed)

    def test_corrupt_period_snapshot_denies_billable_operations_not_export(self):
        period = self.grant()
        BillingPeriod.objects.filter(pk=period.pk).update(terms_snapshot={"schema": 999})
        self.assertEqual(PolicyService.evaluate(self.personal, self.owner, Operation.AI_GENERATE, ai_action="package", now=NOW).code, "configuration_error")
        self.assertTrue(PolicyService.evaluate(self.personal, self.owner, Operation.CSV_EXPORT, now=NOW).allowed)

    def test_unknown_record_usage_does_not_get_treated_as_zero(self):
        result = PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_PROPERTY, usage=UsageSnapshot(), now=NOW)
        self.assertEqual((result.code, result.used), ("usage_unknown", None))

    def test_invalid_action_quantity_usage_or_missing_resource_rejected(self):
        for quantity in (0, -1, True, 1.5):
            self.assertEqual(PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_PROPERTY, quantity=quantity).code, "invalid_quantity")
        self.assertEqual(PolicyService.evaluate(self.personal, self.owner, "unknown").code, "invalid_operation")
        self.assertEqual(PolicyService.evaluate(self.personal, self.owner, Operation.CREATE_PROPERTY, usage={"active_properties": 0}, now=NOW).code, "invalid_usage")
        self.assertEqual(PolicyService.evaluate(self.personal, self.owner, Operation.RESTORE_PROPERTY, now=NOW).code, "invalid_resource")
        self.assertEqual(PolicyService.evaluate(self.personal, self.owner, Operation.AI_GENERATE, ai_action="unknown", now=NOW).code, "invalid_ai_action")

    def test_current_views_record_denial_without_enforcing_it(self):
        account = AccountService.ensure_personal(self.outsider)
        for _ in range(2):
            self.property(owner=self.outsider)
        self.assertFalse(PolicyService.evaluate(account, self.outsider, Operation.CREATE_PROPERTY, now=NOW).allowed)
        self.client.force_login(self.outsider)
        response = self.client.post(reverse("create_property"), {"title": "Новый объект", "property_type": "apartment",
            "deal_type": "sale", "status": "draft", "avito_operation": "sell", "currency": "RUB", "landing_template": "classic"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Property.objects.filter(owner=self.outsider, agency__isnull=True).count(), 3)
        self.assertFalse(ResourceReservation.objects.filter(account=account).latest("pk").decision["allowed"])
