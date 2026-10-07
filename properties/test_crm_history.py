from contextlib import closing
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.deletion import ProtectedError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.test import Client as BrowserClient
from django.urls import reverse
from django.utils import timezone

from .admin import DealAdmin, LeadAdmin, PropertyAdmin
from .forms import PropertyForm
from .models import (
    Agency, AgencyMembership, AuditEvent, Client, ClientInteraction, ClientReminder,
    Deal, Lead, Property, PropertyImage, Showing,
)
from .services.crm_backup_service import (
    BackupError, backup_root, create_backup, latest_backup, verify_backup,
)
from .services.crm_retention_service import add_calendar_months, retention_report


class CrmHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("history-owner")
        cls.other = User.objects.create_user("history-other")
        cls.property = Property.objects.create(owner=cls.owner, title="Исторический объект", status="published", landing_published=True, avito_export=True, cian_export=True)
        cls.client_record = Client.objects.create(owner=cls.owner, name="Клиент", phone="+79990000000")
        cls.lead = Lead.objects.create(property=cls.property, client=cls.client_record, name="Клиент", phone="+79990000000")
        cls.deal = Deal.objects.create(owner=cls.owner, client=cls.client_record, property=cls.property, lead=cls.lead, stage="viewing")
        cls.showing = Showing.objects.create(owner=cls.owner, deal=cls.deal, starts_at=timezone.now() + timedelta(days=1))
        cls.task = ClientReminder.objects.create(owner=cls.owner, client=cls.client_record, deal=cls.deal, text="Подготовить показ")
        cls.interaction = ClientInteraction.objects.create(client=cls.client_record, lead=cls.lead, text="Обсудили условия")

    def setUp(self):
        self.client.force_login(self.owner)

    def archive(self):
        return self.client.post(reverse("delete_property", args=[self.property.pk]))

    def test_archive_keeps_crm_graph_and_media_and_unpublishes(self):
        with TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            image = PropertyImage.objects.create(property=self.property, image=ContentFile(b"photo", name="photo.jpg"))
            self.property.landing_logo.save("logo.png", ContentFile(b"logo"))
            photo_name, logo_name = image.image.name, self.property.landing_logo.name
            response = self.archive()
            self.assertRedirects(response, reverse("property_list") + "?status=archived")
            self.property.refresh_from_db()
            self.assertEqual(self.property.status, "archived")
            self.assertEqual(self.property.status_before_archive, "published")
            self.assertIsNotNone(self.property.archived_at)
            self.assertFalse(self.property.landing_published)
            self.assertFalse(self.property.avito_export)
            self.assertFalse(self.property.cian_export)
            self.assertTrue((Path(directory) / photo_name).is_file())
            self.assertTrue((Path(directory) / logo_name).is_file())
            self.lead.refresh_from_db()
            self.deal.refresh_from_db()
            self.showing.refresh_from_db()
            self.task.refresh_from_db()
            self.assertEqual(self.lead.property_id, self.property.pk)
            self.assertEqual(self.lead.status, "new")
            self.assertEqual(self.deal.stage, "viewing")
            self.assertEqual(self.showing.status, "planned")
            self.assertFalse(self.task.is_done)
            self.assertTrue(ClientInteraction.objects.filter(pk=self.interaction.pk).exists())
            event = AuditEvent.objects.filter(model_name="Объект", object_pk=str(self.property.pk), action="archived").latest("pk")
            self.assertEqual(event.actor_id, self.owner.pk)

    def test_repeat_archive_does_not_reset_date_or_status_snapshot(self):
        self.archive()
        self.property.refresh_from_db()
        archived_at = self.property.archived_at
        self.archive()
        self.property.refresh_from_db()
        self.assertEqual(self.property.archived_at, archived_at)
        self.assertEqual(self.property.status_before_archive, "published")
        self.assertEqual(AuditEvent.objects.filter(model_name="Объект", object_pk=str(self.property.pk), action="archived").count(), 1)

    def test_restore_preserves_previous_status_but_never_republishes(self):
        self.archive()
        response = self.client.post(reverse("restore_property", args=[self.property.pk]))
        self.assertRedirects(response, reverse("property_detail", args=[self.property.pk]))
        self.property.refresh_from_db()
        self.assertEqual(self.property.status, "published")
        self.assertIsNone(self.property.archived_at)
        self.assertEqual(self.property.status_before_archive, "")
        self.assertFalse(self.property.landing_published)
        self.assertFalse(self.property.avito_export)
        self.assertFalse(self.property.cian_export)
        self.assertTrue(AuditEvent.objects.filter(model_name="Объект", object_pk=str(self.property.pk), actor=self.owner, action="restored").exists())

    def test_restore_unknown_old_status_falls_back_to_draft(self):
        self.archive()
        Property.objects.filter(pk=self.property.pk).update(status_before_archive="unknown")
        self.client.post(reverse("restore_property", args=[self.property.pk]))
        self.property.refresh_from_db()
        self.assertEqual(self.property.status, "draft")

    def test_archive_and_restore_require_post_and_own_workspace(self):
        for name in ("delete_property", "restore_property"):
            url = reverse(name, args=[self.property.pk])
            self.assertEqual(self.client.get(url).status_code, 405)
        self.client.force_login(self.other)
        for name in ("delete_property", "restore_property"):
            self.assertEqual(self.client.post(reverse(name, args=[self.property.pk])).status_code, 404)
        self.property.refresh_from_db()
        self.assertEqual(self.property.status, "published")

    def test_archive_and_restore_keep_csrf_protection(self):
        browser = BrowserClient(enforce_csrf_checks=True)
        browser.force_login(self.owner)
        for name in ("delete_property", "restore_property"):
            with self.subTest(action=name):
                self.assertEqual(browser.post(reverse(name, args=[self.property.pk]), {}).status_code, 403)
        browser.get(reverse("property_detail", args=[self.property.pk]))
        token = browser.cookies["csrftoken"].value
        response = browser.post(reverse("delete_property", args=[self.property.pk]), {}, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)
        response = browser.post(reverse("restore_property", args=[self.property.pk]), {}, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)

    def test_active_list_landing_list_and_public_url_exclude_archive(self):
        self.archive()
        active = self.client.get(reverse("property_list"))
        self.assertEqual(list(active.context["properties"]), [])
        self.assertContains(active, 'href="' + reverse("property_list") + '?status=archived"')
        archived = self.client.get(reverse("property_list"), {"status": "archived"})
        self.assertEqual([item.pk for item in archived.context["properties"]], [self.property.pk])
        self.assertContains(archived, "Архив объектов")
        self.assertEqual(list(self.client.get(reverse("landing_list")).context["properties"]), [])
        self.assertEqual(self.client.get(reverse("public_landing", args=[self.property.landing_slug])).status_code, 404)
        self.assertEqual(self.client.post(reverse("public_landing", args=[self.property.landing_slug]), {}).status_code, 404)
        # Even a direct bulk update cannot expose an archived public landing.
        Property.objects.filter(pk=self.property.pk).update(landing_published=True)
        self.assertEqual(self.client.get(reverse("public_landing", args=[self.property.landing_slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse("property_detail", args=[self.property.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("lead_detail", args=[self.lead.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("deal_detail", args=[self.deal.pk])).status_code, 200)

    def test_archived_detail_has_restore_and_no_dangerous_delete_text(self):
        self.archive()
        response = self.client.get(reverse("property_detail", args=[self.property.pk]))
        self.assertContains(response, "Восстановить объект")
        self.assertContains(response, "История объекта сохранена")
        self.assertNotContains(response, "Восстановить объект нельзя")
        self.assertNotContains(response, "Готовность к публикации")

    def test_regular_status_changes_use_same_archive_lifecycle(self):
        self.client.post(reverse("update_property_status", args=[self.property.pk]), {"status": "archived"})
        self.property.refresh_from_db()
        self.assertIsNotNone(self.property.archived_at)
        self.assertFalse(self.property.landing_published)
        self.client.post(reverse("update_property_status", args=[self.property.pk]), {"status": "draft"})
        self.property.refresh_from_db()
        self.assertIsNone(self.property.archived_at)
        self.assertFalse(self.property.landing_published)

    def test_save_cannot_publish_an_archived_property(self):
        self.archive()
        self.property.refresh_from_db()
        self.property.landing_published = True
        self.property.save(update_fields=["landing_published"])
        self.property.refresh_from_db()
        self.assertFalse(self.property.landing_published)
        self.assertEqual(self.property.status_before_archive, "published")

    def test_stale_partial_save_does_not_restore_or_publish_archived_property(self):
        stale = Property.objects.get(pk=self.property.pk)
        self.archive()
        stale.landing_published = True
        stale.save(update_fields=["landing_published"])
        stale.refresh_from_db()
        self.assertEqual(stale.status, "archived")
        self.assertFalse(stale.landing_published)
        self.assertEqual(stale.status_before_archive, "published")

    def test_team_agent_cannot_archive_or_restore_through_status_or_form(self):
        agency = Agency.objects.create(owner=self.owner, name="Агентство")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.other, role="agent")
        Property.objects.filter(pk=self.property.pk).update(agency=agency)
        self.property.refresh_from_db()
        self.client.force_login(self.other)
        self.assertEqual(self.archive().status_code, 404)
        self.assertEqual(self.client.post(reverse("update_property_status", args=[self.property.pk]), {"status": "archived"}).status_code, 403)
        form = PropertyForm({"status": "archived"}, instance=self.property, user=self.other)
        self.assertIn("status", form.errors)
        self.property.status = "archived"
        self.property.save(update_fields=["status"])
        self.assertEqual(self.client.post(reverse("restore_property", args=[self.property.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("update_property_status", args=[self.property.pk]), {"status": "draft"}).status_code, 403)
        form = PropertyForm({"status": "draft"}, instance=self.property, user=self.other)
        self.assertIn("status", form.errors)
        AgencyMembership.objects.filter(user=self.other).update(role="manager")
        self.assertEqual(self.client.post(reverse("restore_property", args=[self.property.pk])).status_code, 302)

    def test_property_form_rejects_simultaneous_archive_and_publication(self):
        form = PropertyForm({"status": "archived", "landing_published": "on"}, instance=self.property, user=self.owner)
        self.assertIn("landing_published", form.errors)

    def test_stale_form_cannot_restore_object_when_saving_other_fields(self):
        self.archive()
        current = Property.objects.get(pk=self.property.pk)
        form = PropertyForm({"title": "Старое окно редактора", "status": "published", "landing_published": "on"}, instance=current, user=self.owner)
        self.assertIn("status", form.errors)
        self.assertIn("отдельной кнопкой", str(form.errors["status"]))
        self.property.refresh_from_db()
        self.assertEqual(self.property.status, "archived")

    def test_orm_parent_deletion_is_protected(self):
        for model, pk in ((Property, self.property.pk), (Client, self.client_record.pk), (Deal, self.deal.pk)):
            with self.subTest(model=model), self.assertRaises(ProtectedError):
                model.objects.filter(pk=pk).delete()
        self.assertTrue(Showing.objects.filter(pk=self.showing.pk).exists())
        self.assertTrue(ClientReminder.objects.filter(pk=self.task.pk).exists())

    def test_client_bulk_delete_reports_protected_history_without_partial_deletion(self):
        empty = Client.objects.create(owner=self.owner, name="Без связей", phone="+79990000001")
        response = self.client.post(reverse("client_bulk_action"), {"action": "delete", "client_ids": [self.client_record.pk, empty.pk]}, follow=True)
        self.assertContains(response, "у них есть CRM-история")
        self.assertEqual(Client.objects.filter(pk__in=[self.client_record.pk, empty.pk]).count(), 2)
        self.assertTrue(Deal.objects.filter(pk=self.deal.pk).exists())

    def test_old_lead_delete_url_archives_without_losing_relations(self):
        response = self.client.post(reverse("lead_delete", args=[self.lead.pk]))
        self.assertRedirects(response, reverse("lead_list") + "?status=archived")
        self.lead.refresh_from_db()
        self.deal.refresh_from_db()
        self.assertEqual(self.lead.status, "archived")
        self.assertIsNotNone(self.lead.archived_at)
        self.assertEqual(self.deal.lead_id, self.lead.pk)
        self.assertEqual(self.interaction.lead_id, self.lead.pk)
        self.assertEqual(self.client.get(reverse("lead_delete", args=[self.lead.pk])).status_code, 405)

    def test_bulk_lead_delete_archives_and_status_can_restore(self):
        self.client.post(reverse("lead_bulk_status_update"), {"action": "delete", "lead_ids": [self.lead.pk]})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, "archived")
        self.client.post(reverse("lead_bulk_status_update"), {"action": "status", "status": "read", "lead_ids": [self.lead.pk]})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, "read")
        self.assertIsNone(self.lead.archived_at)

    def test_admin_disables_destructive_property_lead_deal_deletion(self):
        request = RequestFactory().get("/admin/")
        request.user = self.owner
        for model, admin_class in ((Property, PropertyAdmin), (Lead, LeadAdmin), (Deal, DealAdmin)):
            with self.subTest(model=model):
                self.assertFalse(admin_class(model, admin.site).has_delete_permission(request))

    @override_settings(CRM_ARCHIVE_REVIEW_MONTHS=3, CRM_BACKUP_ROOT="")
    def test_three_month_retention_report_is_read_only(self):
        archived_at = datetime(2026, 1, 31, 12, tzinfo=datetime_timezone.utc)
        self.archive()
        self.lead.status = "archived"
        self.lead.save(update_fields=["status"])
        Property.objects.filter(pk=self.property.pk).update(archived_at=archived_at)
        Lead.objects.filter(pk=self.lead.pk).update(archived_at=archived_at)
        before = retention_report(now=datetime(2026, 4, 30, 11, 59, tzinfo=datetime_timezone.utc))
        self.assertEqual(before["properties_due"], 0)
        report = retention_report(now=datetime(2026, 4, 30, 12, tzinfo=datetime_timezone.utc))
        self.assertEqual(report["properties_due"], 1)
        self.assertEqual(report["leads_due"], 1)
        self.assertFalse(report["automatic_deletion"])
        self.assertTrue(Property.objects.filter(pk=self.property.pk).exists())
        self.assertTrue(Lead.objects.filter(pk=self.lead.pk).exists())
        output = StringIO()
        call_command("crm_retention_report", stdout=output)
        self.assertIn("No CRM records or backups were deleted", output.getvalue())


class CalendarRetentionTests(SimpleTestCase):
    def test_end_of_month_and_year_boundary_are_calendar_months_not_90_days(self):
        self.assertEqual(add_calendar_months(datetime(2026, 1, 31), 3), datetime(2026, 4, 30))
        self.assertEqual(add_calendar_months(datetime(2025, 11, 30), 3), datetime(2026, 2, 28))
        self.assertEqual(add_calendar_months(datetime(2023, 11, 30), 3), datetime(2024, 2, 29))
        with self.assertRaises(ValueError):
            add_calendar_months(datetime(2026, 1, 1), 0)


class CrmHistoryMigrationTests(TransactionTestCase):
    def test_existing_archives_and_crm_relations_survive_migration(self):
        old_target = [("properties", "0038_account_security")]
        new_target = [("properties", "0039_crm_history_preservation")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        OldUser = old_apps.get_model("auth", "User")
        OldProperty = old_apps.get_model("properties", "Property")
        OldClient = old_apps.get_model("properties", "Client")
        OldLead = old_apps.get_model("properties", "Lead")
        OldDeal = old_apps.get_model("properties", "Deal")
        user = OldUser.objects.create(username="archive-migration-owner")
        property = OldProperty.objects.create(owner_id=user.pk, title="Старый архив", status="archived", landing_published=True, avito_export=True, cian_export=True)
        client = OldClient.objects.create(owner_id=user.pk, name="Клиент", phone="+79990000020")
        lead = OldLead.objects.create(property_id=property.pk, client_id=client.pk, name="Клиент", phone=client.phone, status="archived")
        deal = OldDeal.objects.create(owner_id=user.pk, property_id=property.pk, client_id=client.pk, lead_id=lead.pk)
        executor = MigrationExecutor(connection)
        executor.migrate(new_target)
        new_apps = executor.loader.project_state(new_target).apps
        restored_property = new_apps.get_model("properties", "Property").objects.get(pk=property.pk)
        restored_lead = new_apps.get_model("properties", "Lead").objects.get(pk=lead.pk)
        restored_deal = new_apps.get_model("properties", "Deal").objects.get(pk=deal.pk)
        self.assertEqual(restored_property.archived_at, property.updated_at)
        self.assertEqual(restored_property.status_before_archive, "draft")
        self.assertFalse(restored_property.landing_published)
        self.assertFalse(restored_property.avito_export)
        self.assertFalse(restored_property.cian_export)
        self.assertEqual(restored_lead.archived_at, lead.updated_at)
        self.assertEqual(restored_deal.lead_id, lead.pk)
        self.assertEqual(restored_deal.client_id, client.pk)
        self.assertEqual(restored_deal.property_id, property.pk)


@skipUnless(connection.vendor == "sqlite", "SQLite snapshot tests require SQLite")
class CrmBackupTests(TransactionTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.media_root = self.root / "media"
        self.output_dir = self.root / "backups"
        override = override_settings(MEDIA_ROOT=self.media_root, CRM_BACKUP_ROOT=str(self.output_dir))
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user("backup-owner")
        self.property = Property.objects.create(owner=self.user, title="Сохранённый объект")
        self.client_record = Client.objects.create(owner=self.user, name="Клиент", phone="+79990000000")
        self.lead = Lead.objects.create(property=self.property, client=self.client_record, name="Клиент", phone="+79990000000")
        self.deal = Deal.objects.create(owner=self.user, client=self.client_record, property=self.property, lead=self.lead)
        self.image = PropertyImage.objects.create(property=self.property, image=ContentFile(b"saved photo", name="photo.jpg"))

    def test_backup_restores_real_sqlite_records_relations_and_media_without_mutating_live_data(self):
        snapshot = create_backup()
        result = verify_backup(snapshot)
        self.assertEqual(result["media_references"], 1)
        self.assertEqual((snapshot / "media" / self.image.image.name).read_bytes(), b"saved photo")
        with closing(sqlite3.connect(snapshot / "database.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT property_id, client_id, lead_id FROM properties_deal").fetchone(), (self.property.pk, self.client_record.pk, self.lead.pk))
        self.property.title = "Изменённый рабочий объект"
        self.property.save(update_fields=["title"])
        verify_backup(snapshot)
        self.property.refresh_from_db()
        self.assertEqual(self.property.title, "Изменённый рабочий объект")
        with closing(sqlite3.connect(snapshot / "database.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT title FROM properties_property").fetchone()[0], "Сохранённый объект")
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        self.assertEqual((snapshot / "database.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_commands_verify_latest_completed_backup(self):
        output = StringIO()
        call_command("backup_crm", stdout=output)
        first = latest_backup()
        second = create_backup()
        self.assertNotEqual(first, second)
        self.assertEqual(latest_backup(), second)
        call_command("verify_crm_backup", latest=True, stdout=output)
        self.assertIn("Restore verified:", output.getvalue())
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_tampered_media_and_database_are_rejected(self):
        snapshot = create_backup()
        copied_image = snapshot / "media" / self.image.image.name
        copied_image.write_bytes(b"tampered")
        with self.assertRaises(BackupError):
            verify_backup(snapshot)
        with self.assertRaises(CommandError):
            call_command("verify_crm_backup", str(snapshot), stdout=StringIO())

    def test_missing_source_media_fails_without_publishing_incomplete_backup(self):
        self.image.image.delete(save=False)
        with self.assertRaises(BackupError):
            create_backup()
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_missing_manifest_and_missing_backup_are_rejected(self):
        with self.assertRaises(BackupError):
            verify_backup(self.root / "nonexistent")
        with self.assertRaises(BackupError):
            latest_backup()

    def test_rejects_public_backup_location_and_world_readable_directory(self):
        for path in (self.media_root, self.media_root / "snapshots", self.root):
            with self.subTest(path=path), self.assertRaises(BackupError):
                backup_root(path)
        self.output_dir.mkdir(mode=0o755)
        with self.assertRaises(BackupError):
            create_backup()

    def test_rejects_source_symlink_and_path_traversal_in_db_file_reference(self):
        (self.media_root / "symlink.jpg").symlink_to(self.media_root / self.image.image.name)
        with self.assertRaises(BackupError):
            create_backup()
        (self.media_root / "symlink.jpg").unlink()
        PropertyImage.objects.filter(pk=self.image.pk).update(image="../escape.jpg")
        with self.assertRaises(BackupError):
            create_backup()
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_rejects_manifest_traversal_and_malformed_metadata(self):
        snapshot = create_backup()
        manifest_path = snapshot / "manifest.json"
        original = json.loads(manifest_path.read_text())
        for payload in ([], {**original, "file_fields": ["malformed"]}, {**original, "files": {**original["files"], "../escape": {"size": 1, "sha256": "bad"}}}):
            with self.subTest(payload_type=type(payload).__name__):
                manifest_path.write_text(json.dumps(payload))
                with self.assertRaises(BackupError):
                    verify_backup(snapshot)

    @override_settings(CRM_BACKUP_REVIEW_DAYS=30)
    def test_old_backup_retention_report_does_not_delete_snapshot(self):
        snapshot = create_backup()
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["created_at"] = (timezone.now() - timedelta(days=31)).isoformat()
        manifest_path.write_text(json.dumps(manifest))
        report = retention_report()
        self.assertEqual(report["backups_due"], 1)
        self.assertTrue(snapshot.is_dir())
        verify_backup(snapshot)


class CrmBackupBackendTests(SimpleTestCase):
    def test_unsupported_backend_fails_clearly(self):
        with patch("properties.services.crm_backup_service.connections") as mock_connections:
            mock_connections.__getitem__.return_value.vendor = "postgresql"
            with self.assertRaisesMessage(BackupError, "supports SQLite only"):
                create_backup()
