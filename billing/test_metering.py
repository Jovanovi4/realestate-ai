from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import uuid

import requests
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import transaction
from django.db.models import Sum
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from properties.ai_views import save_generated_contents
from properties.models import AIContent, Agency, AgencyInvitation, AgencyMembership, Property, PropertyImage, RealtorProfile
from properties.services.ai_service import AIService, AIServiceError
from properties.services.demo_data_service import DemoDataService

from .inventory import measure_file, reconcile_account, refresh_saved_file, unassigned_media_report
from .models import AccountInventory, BillingAccount, FileReference, ResourceReservation, StoredFile, UsageEntry, UsageOperation, UsageResult
from .rules import Operation, PolicyService, collect_usage
from .services import AccountService, SubscriptionService, current_plan_version
from .tests import seed_test_catalog
from .usage import account_report, begin_operation, finish_operation, mark_stale_operations, observe_ai_generation, observe_xml_export, recover_saved_operation


def payload(content="Готовый текст", content_type="headline"):
    return {"content": content, "content_type": content_type, "prompt": "PRIVATE PROMPT", "provider": "ollama", "model": "test", "duration_ms": 1}


@override_settings(BILLING_SHADOW_ENABLED=True)
class MeteringFixture(TestCase):
    def setUp(self):
        seed_test_catalog()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = override_settings(MEDIA_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.media_root = Path(directory.name)
        self.user = get_user_model().objects.create_user("meter-owner")
        self.operator = get_user_model().objects.create_user("meter-operator", is_superuser=True, is_staff=True)
        with self.captureOnCommitCallbacks(execute=True), self.assertNoLogs("billing.observation", level="ERROR"):
            self.prop = Property.objects.create(owner=self.user, title="Объект")
        self.account = AccountService.for_resource(self.prop)
        reconcile_account(self.account)

    def confirmed(self):
        with self.captureOnCommitCallbacks(execute=True):
            RealtorProfile.objects.create(user=self.user, email="owner@example.test", email_verified_at=timezone.now())
        return self.user.demo_credit_grant

    def paid(self, account=None, code="standard"):
        return SubscriptionService.grant_period(account=account or self.account, plan_version=current_plan_version(code), actor=self.operator,
            reason="Пилот учёта", request_key=uuid.uuid4(), starts_at=timezone.now() - timedelta(minutes=1))

    def photo(self, data=b"saved-image-bytes"):
        with self.captureOnCommitCallbacks(execute=True), self.assertNoLogs("billing.observation", level="ERROR"):
            return PropertyImage.objects.create(property=self.prop, image=ContentFile(data, name="photo.jpg"))

    def generate(self, action="short_text", results=None):
        return observe_ai_generation(resource=self.prop, actor=self.user, ai_action=action,
            generate=lambda: results or [payload()], save=lambda rows: save_generated_contents(self.prop, "business", rows))


class LedgerTests(MeteringFixture):
    def test_same_operation_is_recorded_and_finished_once(self):
        self.confirmed()
        key = uuid.uuid4()
        first = begin_operation(account=self.account, actor=self.user, action=Operation.AI_GENERATE, resource=self.prop, ai_action="package", request_key=key)
        same = begin_operation(account=self.account, actor=self.user, action=Operation.AI_GENERATE, resource=self.prop, ai_action="package", request_key=key)
        self.assertEqual(first.pk, same.pk)
        content = AIContent.objects.create(property=self.prop, content_type="headline", content="Текст")
        for _ in range(2):
            finish_operation(first, result_ids=[content.pk], deltas={"ai_credits": 3})
        self.assertEqual(first.entries.count(), 1)
        self.assertEqual(first.entries.get().delta, 3)

    def test_idempotency_key_is_bound_to_workspace_resource_actor_and_action(self):
        key = uuid.uuid4()
        begin_operation(account=self.account, actor=self.user, action=Operation.AI_GENERATE, resource=self.prop, ai_action="package", request_key=key)
        with self.assertRaises(ValidationError):
            begin_operation(account=self.account, actor=self.user, action=Operation.AI_GENERATE, resource=self.prop, ai_action="landing", request_key=key)
        other = get_user_model().objects.create_user("different-account")
        with self.assertRaises(ValidationError):
            begin_operation(account=AccountService.ensure_personal(other), actor=other, action=Operation.AI_GENERATE, ai_action="package", request_key=key)

    def test_success_and_ledger_values_are_immutable(self):
        self.confirmed()
        self.generate()
        operation = UsageOperation.objects.get(ai_action="short_text")
        operation.provider_model = "changed"
        with self.assertRaises(ValidationError):
            operation.save()
        entry = operation.entries.get()
        entry.delta = 999
        with self.assertRaises(ValidationError):
            entry.save()

    def test_failed_operation_cannot_acquire_a_successful_spend_entry(self):
        operation = begin_operation(account=self.account, actor=self.user, action=Operation.AI_GENERATE, resource=self.prop, ai_action="package")
        finish_operation(operation, status=UsageOperation.Status.FAILED, error_code="generation_failed")
        with self.assertRaises(ValidationError):
            UsageEntry.objects.create(operation=operation, account=self.account, metric="ai_credits", delta=3)

    def test_running_and_unknown_work_is_observed_as_pending_not_spent(self):
        grant = self.confirmed()
        operation = begin_operation(account=self.account, actor=self.user, action=Operation.AI_GENERATE, resource=self.prop, ai_action="package")
        context = PolicyService.context(self.account)
        self.assertEqual((context.ai_used, context.ai_reserved), (0, 3))
        grant.refresh_from_db()
        self.assertEqual((grant.credits_used, grant.credits_reserved), (0, 3))
        finish_operation(operation, status=UsageOperation.Status.UNKNOWN, error_code="provider_outcome_unknown")
        self.assertEqual(PolicyService.context(self.account).ai_reserved, 3)
        grant.refresh_from_db()
        self.assertEqual((grant.credits_used, grant.credits_reserved), (0, 3))
        self.assertEqual(account_report(self.account)["unknown_operations"], 1)

    def test_failed_generation_releases_credit_reservation_once(self):
        grant = self.confirmed()
        operation = begin_operation(account=self.account, actor=self.user, action=Operation.AI_GENERATE,
            resource=self.prop, ai_action="package")
        finish_operation(operation, status=UsageOperation.Status.FAILED, error_code="generation_failed")
        finish_operation(operation, status=UsageOperation.Status.FAILED)
        grant.refresh_from_db()
        self.assertEqual((grant.credits_used, grant.credits_reserved), (0, 0))

    def test_stale_review_is_idempotent_and_never_retries_provider(self):
        operation = begin_operation(account=self.account, actor=self.user, action=Operation.AI_GENERATE, resource=self.prop, ai_action="package", now=timezone.now() - timedelta(minutes=20))
        with patch("requests.post") as post:
            self.assertEqual(mark_stale_operations(), 1)
            self.assertEqual(mark_stale_operations(), 0)
        operation.refresh_from_db()
        self.assertEqual(operation.status, UsageOperation.Status.UNKNOWN)
        post.assert_not_called()
        self.assertFalse(operation.entries.exists())

    def test_admin_journal_is_read_only_and_paginated(self):
        request = RequestFactory().get("/admin/")
        request.user = self.operator
        for model in (UsageOperation, UsageEntry, ResourceReservation, StoredFile, FileReference, AccountInventory, UsageResult):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))
        self.assertEqual(admin.site._registry[UsageOperation].list_per_page, 25)


class CommittedResourceTests(MeteringFixture):
    def test_over_quota_creation_is_allowed_and_observed_with_actor(self):
        self.client.force_login(self.user)
        form = {"title": "Новый объект", "property_type": "apartment", "deal_type": "sale", "status": "draft",
                "avito_operation": "sell", "currency": "RUB", "landing_template": "classic"}
        with self.captureOnCommitCallbacks(execute=True), self.assertNoLogs("billing.observation", level="ERROR"):
            self.client.post(reverse("create_property"), form)
            response = self.client.post(reverse("create_property"), form)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(collect_usage(self.account).active_properties, 3)
        operation = UsageOperation.objects.filter(action="create_property").latest("pk")
        self.assertFalse(operation.decision["allowed"])
        self.assertEqual(operation.actor_id, self.user.pk)
        self.assertEqual(operation.resource_type, "properties.property")
        self.assertTrue(operation.resource_id)

    def test_publication_and_archive_release_card_and_landing_not_bytes(self):
        photo = self.photo()
        with self.captureOnCommitCallbacks(execute=True):
            self.prop.landing_published = True
            self.prop.save(update_fields=["landing_published"])
        self.assertEqual(collect_usage(self.account).landings, 1)
        with self.captureOnCommitCallbacks(execute=True), self.assertNoLogs("billing.observation", level="ERROR"):
            self.prop.status = "archived"
            self.prop.save(update_fields=["status"])
        usage = collect_usage(self.account)
        self.assertEqual((usage.active_properties, usage.landings, usage.total_properties), (0, 0, 1))
        self.assertEqual(usage.storage_bytes, photo.image.size)
        ledger = {row["metric"]: row["value"] for row in self.account.usage_entries.values("metric").annotate(value=Sum("delta"))}
        self.assertEqual((ledger["active_properties"], ledger["landings"]), (0, 0))

    def test_rolled_back_creation_does_not_leave_successful_observation(self):
        before = UsageOperation.objects.count()
        with self.captureOnCommitCallbacks(execute=True), self.assertRaises(RuntimeError):
            with transaction.atomic():
                Property.objects.create(owner=self.user, title="Не сохранён")
                raise RuntimeError("rollback")
        self.assertEqual(UsageOperation.objects.count(), before)
        self.assertEqual(collect_usage(self.account).total_properties, 1)

    def test_bulk_sql_is_corrected_explicitly_by_reconciliation(self):
        with self.captureOnCommitCallbacks(execute=True):
            Property.objects.create(owner=self.user, title="Второй")
        Property.objects.filter(owner=self.user).update(status="archived", landing_published=False)
        result = reconcile_account(self.account)
        self.assertEqual(result["corrections"]["active_properties"], -2)
        self.assertEqual(reconcile_account(self.account)["corrections"], {})

    def test_signal_failure_does_not_cancel_successful_crm_save(self):
        with patch("billing.signals._observe", side_effect=RuntimeError("PRIVATE DATA")), self.assertLogs("billing.observation", level="ERROR") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                created = Property.objects.create(owner=self.user, title="Сохранён")
        self.assertTrue(Property.objects.filter(pk=created.pk).exists())
        self.assertNotIn("PRIVATE DATA", str(logs.output))

    @override_settings(BILLING_SHADOW_ENABLED=False)
    def test_disabled_shadow_mode_leaves_views_working_without_new_metering(self):
        before = UsageOperation.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            Property.objects.create(owner=self.user, title="Без учёта")
        self.generate()
        self.assertEqual(UsageOperation.objects.count(), before)
        self.assertEqual(AIContent.objects.count(), 1)


class StorageMeterTests(MeteringFixture):
    def test_actual_saved_sizes_not_incoming_upload_sizes_are_counted(self):
        photo = self.photo(b"1234567")
        self.assertEqual(collect_usage(self.account).storage_bytes, 7)
        self.assertEqual(self.account.stored_files.get().size_bytes, 7)
        self.assertEqual(self.account.usage_entries.filter(metric="storage_bytes").aggregate(value=Sum("delta"))["value"], 7)
        self.assertEqual(photo.image.size, 7)

    def test_shared_name_in_same_workspace_is_not_counted_twice(self):
        first = self.photo()
        with self.captureOnCommitCallbacks(execute=True):
            PropertyImage.objects.create(property=self.prop, image=first.image.name)
        self.assertEqual(self.account.stored_files.count(), 1)
        self.assertEqual(FileReference.objects.count(), 2)
        self.assertEqual(collect_usage(self.account).storage_bytes, first.image.size)

    def test_removing_database_reference_does_not_claim_storage_was_deleted(self):
        photo = self.photo(b"1234567")
        name = photo.image.name
        with self.captureOnCommitCallbacks(execute=True):
            photo.delete()
        self.assertEqual(FileReference.objects.count(), 0)
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(collect_usage(self.account).storage_bytes, 7)
        default_storage.delete(name)
        refresh_saved_file(self.account, name)
        self.assertEqual(collect_usage(self.account).storage_bytes, 0)

    def test_failed_storage_delete_keeps_byte_usage(self):
        photo = self.photo(b"1234567")
        name = photo.image.name
        with self.captureOnCommitCallbacks(execute=True):
            photo.delete()
        with patch.object(default_storage, "delete", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                default_storage.delete(name)
            refresh_saved_file(self.account, name)
        self.assertEqual(collect_usage(self.account).storage_bytes, 7)

    def test_logo_clear_keeps_physical_orphan_attributed_to_original_account(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.prop.landing_logo.save("logo.png", ContentFile(b"logo-bytes"))
        name = self.prop.landing_logo.name
        with self.captureOnCommitCallbacks(execute=True):
            self.prop.landing_logo = ""
            self.prop.save(update_fields=["landing_logo"])
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(collect_usage(self.account).storage_bytes, 10)
        self.assertFalse(FileReference.objects.exists())

    def test_unknown_metadata_is_not_zero_and_flags_incomplete_inventory(self):
        self.photo()
        with patch("billing.inventory.measure_file", return_value=(None, "unknown", "storage_metadata_error")):
            result = reconcile_account(self.account)
        self.assertFalse(result["files_complete"])
        self.assertIsNone(collect_usage(self.account).storage_bytes)
        self.assertEqual(PolicyService.evaluate(self.account, self.user, Operation.UPLOAD_FILES, quantity=1).code, "usage_unknown")

    def test_confirmed_missing_file_is_zero_but_repair_issue_is_reported(self):
        photo = self.photo()
        default_storage.delete(photo.image.name)
        result = reconcile_account(self.account)
        self.assertTrue(result["files_complete"])
        self.assertEqual(result["issue_count"], 1)
        self.assertEqual(collect_usage(self.account).storage_bytes, 0)

    def test_unassigned_files_are_reported_not_removed_or_assigned_randomly(self):
        name = default_storage.save("properties/unassigned.jpg", ContentFile(b"unassigned"))
        report = unassigned_media_report()
        self.assertEqual((report["unassigned_files"], report["unassigned_bytes"]), (1, 10))
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(collect_usage(self.account).storage_bytes, 0)

    def test_demo_counts_card_photos_and_landing_not_ai_credits(self):
        with self.captureOnCommitCallbacks(execute=True), self.assertNoLogs("billing.observation", level="ERROR"):
            demo = DemoDataService.create_for_user(self.user)
        expected = sum(image.image.size for image in demo.images.all())
        usage = collect_usage(self.account)
        self.assertEqual((usage.active_properties, usage.landings, usage.storage_bytes), (2, 1, expected))
        self.assertFalse(UsageEntry.objects.filter(metric="ai_credits").exists())

    def test_explicit_resource_reassignment_moves_byte_attribution_without_deleting_file(self):
        photo = self.photo(b"1234567")
        other = get_user_model().objects.create_user("new-owner")
        with self.captureOnCommitCallbacks(execute=True), self.assertNoLogs("billing.observation", level="ERROR"):
            self.prop.owner = other
            self.prop.save(update_fields=["owner"])
        new_account = AccountService.for_resource(self.prop)
        self.assertEqual(collect_usage(self.account).storage_bytes, 0)
        self.assertEqual(collect_usage(new_account).storage_bytes, 7)
        self.assertTrue(default_storage.exists(photo.image.name))

    def test_full_reconciliation_can_repair_a_bulk_sql_scope_reassignment(self):
        photo = self.photo(b"1234567")
        other = get_user_model().objects.create_user("sql-new-owner")
        new_account = AccountService.ensure_personal(other)
        Property.objects.filter(pk=self.prop.pk).update(owner=other)
        call_command("reconcile_billing_usage", stdout=StringIO())
        self.assertEqual(collect_usage(self.account).storage_bytes, 0)
        self.assertEqual(collect_usage(new_account).storage_bytes, 7)
        self.assertTrue(default_storage.exists(photo.image.name))

    def test_file_returning_to_its_former_workspace_is_measured_again(self):
        photo = self.photo(b"1234567")
        other = get_user_model().objects.create_user("round-trip-owner")
        with self.captureOnCommitCallbacks(execute=True), self.assertNoLogs("billing.observation", level="ERROR"):
            self.prop.owner = other
            self.prop.save(update_fields=["owner"])
        new_account = AccountService.for_resource(self.prop)
        with self.captureOnCommitCallbacks(execute=True), self.assertNoLogs("billing.observation", level="ERROR"):
            self.prop.owner = self.user
            self.prop.save(update_fields=["owner"])
        self.assertEqual(collect_usage(self.account).storage_bytes, 7)
        self.assertEqual(collect_usage(new_account).storage_bytes, 0)
        self.assertEqual(self.account.stored_files.get().state, "present")
        self.assertTrue(default_storage.exists(photo.image.name))


class AIObservationTests(MeteringFixture):
    def test_http_retry_replays_saved_result_without_provider_or_extra_credit(self):
        grant = self.confirmed()
        self.client.force_login(self.user)
        key = str(uuid.uuid4())
        url = reverse("ai_inline_generate", args=[self.prop.pk])
        with patch("properties.ai_views.AIService.generate_inline_content", return_value=("headline", payload())) as provider:
            first = self.client.post(url, {"target": "marketing_headline"}, HTTP_IDEMPOTENCY_KEY=key)
            second = self.client.post(url, {"target": "marketing_headline"}, HTTP_IDEMPOTENCY_KEY=key)
            conflict = self.client.post(url, {"target": "short_description"}, HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual((first.status_code, second.status_code, conflict.status_code), (200, 200, 409))
        self.assertEqual(first.json(), second.json())
        provider.assert_called_once()
        self.assertEqual(AIContent.objects.count(), 1)
        self.assertEqual(UsageEntry.objects.filter(metric="ai_credits").count(), 1)
        grant.refresh_from_db()
        self.assertEqual((grant.credits_used, grant.credits_reserved), (1, 0))

    def test_pending_retry_never_reaches_provider(self):
        self.confirmed()
        self.client.force_login(self.user)
        key = uuid.uuid4()
        from properties.ai_views import ai_request_identity
        from django.test import RequestFactory
        url = reverse("ai_inline_generate", args=[self.prop.pk])
        request = RequestFactory().post(url, {"target": "marketing_headline"}, HTTP_IDEMPOTENCY_KEY=str(key))
        identity = ai_request_identity(request)
        begin_operation(account=self.account, actor=self.user, action=Operation.AI_GENERATE, resource=self.prop,
            ai_action="short_text", **identity)
        with patch("properties.ai_views.AIService.generate_inline_content") as provider:
            response = self.client.post(url, {"target": "marketing_headline"}, HTTP_IDEMPOTENCY_KEY=str(key))
        self.assertEqual(response.status_code, 409)
        provider.assert_not_called()

    def test_complete_package_is_one_operation_not_three_content_charges(self):
        self.confirmed()
        results = [payload(content_type=kind) for kind in ("headline", "short_description", "full_description")]
        contents = self.generate("package", results)
        operation = UsageOperation.objects.get(ai_action="package")
        self.assertEqual(len(contents), 3)
        self.assertEqual(operation.result_ids, [content.pk for content in contents])
        self.assertEqual(operation.entries.get().delta, 3)
        self.assertEqual(PolicyService.context(self.account).ai_used, 3)
        self.assertNotIn("PRIVATE PROMPT", json.dumps(operation.decision))
        self.assertNotIn("Готовый текст", json.dumps(operation.result_ids))

    def test_ai_can_exceed_demo_quota_in_shadow_mode_without_overdrawing_grant(self):
        grant = self.confirmed()
        self.generate("landing")
        self.generate("landing")
        operation = UsageOperation.objects.filter(ai_action="landing").latest("pk")
        self.assertEqual(operation.status, UsageOperation.Status.SUCCEEDED)
        self.assertFalse(operation.decision["allowed"])
        self.assertEqual(PolicyService.context(self.account).ai_used, 10)
        grant.refresh_from_db()
        self.assertEqual((grant.credits_allocated, grant.credits_used, grant.credits_reserved), (5, 5, 0))
        self.assertFalse(operation.budget_reserved)

    def test_paid_period_does_not_mix_demo_or_future_month_spend(self):
        self.confirmed()
        self.generate("package")
        period = self.paid()
        self.generate("full_description")
        context = PolicyService.context(self.account)
        self.assertEqual((context.ai_allocated, context.ai_used), (100, 2))
        SubscriptionService.grant_period(account=self.account, plan_version=current_plan_version("standard"), actor=self.operator,
            reason="Продление", request_key=uuid.uuid4(), starts_at=period.ends_at)
        self.assertEqual(PolicyService.context(self.account).ai_used, 2)
        self.assertEqual(PolicyService.context(self.account, now=period.ends_at).ai_used, 0)

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="test-key-not-real")
    def test_openai_usage_metadata_is_saved_without_a_second_api_request(self):
        self.confirmed()
        response = Mock()
        response.json.return_value = {"id": "resp_test", "model": "actual-model", "status": "completed", "output_text": "Готовый текст",
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30, "input_tokens_details": {"cached_tokens": 5}}}
        self.client.force_login(self.user)
        with patch("properties.services.ai_service.requests.post", return_value=response) as post:
            result = self.client.post(reverse("ai_inline_generate", args=[self.prop.pk]), {"target": "marketing_headline"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(post.call_count, 1)
        operation = UsageOperation.objects.get(ai_action="short_text")
        self.assertEqual((operation.input_tokens, operation.output_tokens, operation.cached_input_tokens, operation.total_tokens), (20, 10, 5, 30))
        self.assertEqual((operation.provider_response_id, operation.provider_model), ("resp_test", "actual-model"))

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="test-key-not-real")
    def test_incomplete_bundle_keeps_provider_usage_but_consumes_no_user_credit(self):
        response = Mock()
        response.json.return_value = {"status": "completed", "output_text": "[[headline]]Один текст", "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}
        self.client.force_login(self.user)
        with patch("properties.services.ai_service.requests.post", return_value=response):
            result = self.client.post(reverse("ai_bundle_generate", args=[self.prop.pk]), {"bundle": "package"})
        self.assertEqual(result.status_code, 400)
        operation = UsageOperation.objects.get(ai_action="package")
        self.assertEqual((operation.status, operation.total_tokens), ("failed", 30))
        self.assertFalse(operation.entries.exists())
        self.assertFalse(AIContent.objects.exists())

    @override_settings(AI_PROVIDER="openai", OPENAI_API_KEY="test-key-not-real")
    def test_timeout_is_unknown_not_success_or_free_retry(self):
        self.confirmed()
        self.client.force_login(self.user)
        with patch("properties.services.ai_service.requests.post", side_effect=requests.Timeout("PRIVATE PROVIDER ERROR")) as post:
            response = self.client.post(reverse("ai_bundle_generate", args=[self.prop.pk]), {"bundle": "package"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(post.call_count, 1)
        operation = UsageOperation.objects.get(ai_action="package")
        self.assertEqual((operation.status, operation.error_code), ("unknown", "provider_outcome_unknown"))
        self.assertNotIn("PRIVATE PROVIDER ERROR", operation.error_code)
        self.assertIsNone(operation.total_tokens)
        self.assertFalse(operation.entries.exists())

    def test_save_failure_rolls_back_entire_bundle_and_spends_nothing(self):
        results = [payload(content_type=kind) for kind in ("headline", "short_description", "full_description")]
        create = AIContent.objects.create
        calls = 0
        def flaky_save(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("database save failed")
            return create(**kwargs)
        with patch.object(AIContent.objects, "create", side_effect=flaky_save), self.assertRaises(RuntimeError):
            self.generate("package", results)
        self.assertFalse(AIContent.objects.exists())
        self.assertFalse(UsageEntry.objects.filter(metric="ai_credits").exists())
        self.assertEqual(UsageOperation.objects.get(ai_action="package").status, "failed")

    def test_legacy_ai_rows_are_not_charged_by_reconciliation(self):
        AIContent.objects.create(property=self.prop, content_type="headline", content="Старый текст")
        reconcile_account(self.account)
        self.assertFalse(UsageEntry.objects.filter(metric="ai_credits").exists())
        self.assertFalse(UsageOperation.objects.exclude(ai_action="").exists())

    def test_local_audit_has_zero_provider_requests_and_zero_credits(self):
        self.confirmed()
        with patch("requests.post") as post:
            observe_ai_generation(resource=self.prop, actor=self.user, ai_action="audit",
                generate=lambda: [{**AIService.audit_property(self.prop, False), "content_type": "audit"}],
                save=lambda rows: save_generated_contents(self.prop, "business", rows))
        post.assert_not_called()
        self.assertEqual(PolicyService.context(self.account).ai_used, 0)
        self.assertEqual(UsageOperation.objects.get(ai_action="audit").planned_credits, 0)

    def test_metering_start_failure_does_not_prevent_generation(self):
        with patch("billing.usage.begin_operation", side_effect=RuntimeError("metering unavailable")), self.assertLogs("billing.observation", level="ERROR"):
            results = self.generate()
        self.assertEqual(len(results), 1)
        self.assertEqual(AIContent.objects.count(), 1)

    def test_completion_gap_recovers_from_durable_results_without_api_retry(self):
        self.confirmed()
        with patch("billing.usage.finish_operation", side_effect=RuntimeError("metering completion unavailable")), self.assertLogs("billing.observation", level="ERROR"):
            self.generate()
        operation = UsageOperation.objects.get(ai_action="short_text")
        self.assertEqual(operation.status, "running")
        self.assertEqual(operation.saved_results.count(), 1)
        self.assertFalse(operation.entries.exists())
        with patch("requests.post") as post:
            self.assertTrue(recover_saved_operation(operation))
            self.assertFalse(recover_saved_operation(operation))
        post.assert_not_called()
        self.assertEqual(operation.entries.get().delta, 1)
        context = PolicyService.context(self.account)
        self.assertEqual((context.ai_used, context.ai_reserved), (1, 0))

    def test_unknown_provider_usage_remains_null_not_zero(self):
        self.generate()
        operation = UsageOperation.objects.get(ai_action="short_text")
        self.assertIsNone(operation.input_tokens)
        self.assertIsNone(operation.total_tokens)
        self.assertEqual(account_report(self.account)["provider_usage_unknown"], 1)

    def test_repeat_completion_cannot_replace_results_or_spend(self):
        self.generate()
        operation = UsageOperation.objects.get(ai_action="short_text")
        with self.assertRaises(ValidationError):
            finish_operation(operation, result_ids=[999999], deltas={"ai_credits": 1})
        with self.assertRaises(ValidationError):
            finish_operation(operation, result_ids=operation.result_ids, deltas={"ai_credits": 99})
        self.assertEqual(operation.entries.get().delta, 1)


class AgencyAndReportTests(MeteringFixture):
    def test_founder_conversion_preserves_paid_period_usage_and_demo_residual(self):
        grant = self.confirmed()
        self.generate("package")
        period = self.paid(code="business")
        self.generate("full_description")
        original_account_id = self.account.pk
        self.client.force_login(self.user)
        with self.captureOnCommitCallbacks(execute=True), self.assertNoLogs("billing.observation", level="ERROR"):
            response = self.client.post(reverse("agency_create"), {"name": "Агентство"})
        self.assertEqual(response.status_code, 302)
        self.prop.refresh_from_db()
        agency_account = AccountService.for_resource(self.prop)
        self.assertEqual(agency_account.pk, original_account_id)
        self.assertEqual(PolicyService.context(agency_account).period_id, period.pk)
        self.assertEqual(PolicyService.context(agency_account).ai_used, 2)
        personal = AccountService.ensure_personal(self.user)
        grant.refresh_from_db()
        self.assertEqual(grant.account_id, personal.pk)
        self.assertEqual(PolicyService.context(personal).ai_used, 3)
        self.assertEqual(collect_usage(personal).total_properties, 0)
        self.assertEqual(collect_usage(agency_account).total_properties, 1)

    def test_joining_membership_does_not_transfer_personal_subscription(self):
        personal_period = self.paid()
        owner = get_user_model().objects.create_user("agency-founder")
        with self.captureOnCommitCallbacks(execute=True):
            agency = Agency.objects.create(owner=owner, name="Общая база")
            AgencyMembership.objects.create(agency=agency, user=owner, role="owner")
            AgencyMembership.objects.create(agency=agency, user=self.user, role="agent")
        self.assertEqual(PolicyService.context(self.account).period_id, personal_period.pk)
        self.prop.refresh_from_db()
        self.assertIsNone(self.prop.agency_id)

    def test_invite_acceptance_and_expiry_reconcile_seat_reservations(self):
        with self.captureOnCommitCallbacks(execute=True):
            agency = Agency.objects.create(owner=self.user, name="Команда")
            AgencyMembership.objects.create(agency=agency, user=self.user, role="owner")
            invitation = AgencyInvitation.objects.create(agency=agency, created_by=self.user, expires_at=timezone.now() + timedelta(days=7))
        account = AccountService.ensure_agency(agency)
        self.assertEqual((collect_usage(account).occupied_seats, collect_usage(account).invited_seats), (1, 1))
        member = get_user_model().objects.create_user("new-member")
        with self.captureOnCommitCallbacks(execute=True), transaction.atomic():
            AgencyMembership.objects.create(agency=agency, user=member, role="agent")
            invitation.accepted_by = member
            invitation.save(update_fields=["accepted_by"])
        usage = collect_usage(account)
        self.assertEqual((usage.occupied_seats, usage.invited_seats), (2, 0))
        with self.captureOnCommitCallbacks(execute=True):
            AgencyInvitation.objects.create(agency=agency, created_by=self.user, expires_at=timezone.now() + timedelta(days=7))
        future = timezone.now() + timedelta(days=8)
        result = reconcile_account(account, now=future)
        self.assertEqual(result["usage"]["invited_seats"], 0)

    def test_combined_xml_is_observed_separately_without_blocking_demo(self):
        owner = get_user_model().objects.create_user("xml-founder")
        agency = Agency.objects.create(owner=owner, name="Экспорт")
        AgencyMembership.objects.create(agency=agency, user=owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.user, role="agent")
        shared = Property.objects.create(owner=self.user, agency=agency, title="Общий")
        observe_xml_export([self.prop, shared], self.user, "avito")
        operations = UsageOperation.objects.filter(action="xml_export")
        self.assertEqual(operations.count(), 2)
        self.assertTrue(all(operation.status == "succeeded" and not operation.decision["allowed"] for operation in operations))
        self.assertEqual(len(set(operations.values_list("account_id", flat=True))), 2)

    def test_reconcile_command_is_repeatable_and_report_is_read_only(self):
        output = StringIO()
        call_command("reconcile_billing_usage", account_id=self.account.pk, stdout=output)
        before = (UsageOperation.objects.count(), UsageEntry.objects.count(), BillingAccount.objects.count())
        call_command("reconcile_billing_usage", account_id=self.account.pk, stdout=StringIO())
        call_command("billing_usage_report", account_id=self.account.pk, stdout=StringIO())
        self.assertEqual(before, (UsageOperation.objects.count(), UsageEntry.objects.count(), BillingAccount.objects.count()))
        self.assertEqual(json.loads(output.getvalue())["mode"], "shadow")

    @override_settings(BILLING_SHADOW_ENABLED=False)
    def test_report_exposes_disabled_recording_and_inventory_timestamp(self):
        report = account_report(self.account)
        self.assertFalse(report["recording_enabled"])
        self.assertTrue(report["inventory"]["file_sizes_known"])
        self.assertIsNotNone(report["inventory"]["last_updated_at"])


class StorageSafetyTests(SimpleTestCase):
    def test_unsafe_relative_names_are_unknown_without_accessing_storage(self):
        storage = Mock()
        for name in ("/etc/passwd", "../file.jpg", "a/../file.jpg", "a\\file.jpg", "a//file.jpg"):
            with self.subTest(name=name):
                self.assertEqual(measure_file(name, storage), (None, "unknown", "unsafe_storage_name"))
        storage.size.assert_not_called()
        storage.exists.assert_not_called()
