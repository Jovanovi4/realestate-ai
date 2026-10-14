from datetime import timedelta
from tempfile import TemporaryDirectory

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing.enforcement import TariffDenied, require_new_action
from billing.rules import Operation, PolicyService
from billing.services import AccountService
from billing.models import StoredFile

from .agency_access import agency_for, membership_for
from .models import (
    Agency, AgencyInvitation, AgencyMembership, Client, Lead, OutboundMessage,
    Property, TelegramDelivery, TelegramFeed, TelegramPublication, TelegramSubscriber,
)
from .services.telegram_public_service import is_visible, process_update


@override_settings(BILLING_ENFORCEMENT_ENABLED=False)
class AgencyClosureTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("closure-owner")
        self.agent = User.objects.create_user("closure-agent")
        self.outsider = User.objects.create_user("closure-outsider")
        self.agency = Agency.objects.create(owner=self.owner, name="Тестовая команда")
        AgencyMembership.objects.create(agency=self.agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=self.agency, user=self.agent, role="agent")
        self.property = Property.objects.create(
            owner=self.owner, agency=self.agency, title="Общий объект", status="available",
            landing_published=True, telegram_published=True,
        )
        self.private = Property.objects.create(owner=self.owner, title="Личный объект")
        self.client_record = Client.objects.create(owner=self.owner, agency=self.agency, name="Клиент", phone="+79990001122")
        self.lead = Lead.objects.create(property=self.property, client=self.client_record, assigned_to=self.owner,
                                        name="Клиент", phone="+79990001122")
        self.invite = AgencyInvitation.objects.create(
            agency=self.agency, created_by=self.owner, expires_at=timezone.now() + timedelta(days=7),
        )
        self.feed = TelegramFeed.objects.create(agency=self.agency)
        self.subscriber = TelegramSubscriber.objects.create(feed=self.feed, chat_id=9876, user_id=9876, active=True)
        self.publication = TelegramPublication.objects.create(feed=self.feed, property=self.property)
        self.delivery = TelegramDelivery.objects.create(publication=self.publication, subscriber=self.subscriber)
        self.outbound = OutboundMessage.objects.create(
            dedupe_key="closure:listing", channel=OutboundMessage.Channel.PUBLIC_TELEGRAM,
            payload={"method": "listing", "data": {"feed_id": self.feed.pk, "property_id": self.property.pk, "chat_id": 9876}},
        )
        self.account = AccountService.ensure_agency(self.agency)
        self.client.force_login(self.owner)

    def preview(self):
        response = self.client.get(reverse("agency_close"))
        self.assertEqual(response.status_code, 200)
        return response.context["preview_token"]

    def test_preview_is_owner_only_and_does_not_change_data(self):
        token = self.preview()
        self.assertTrue(token)
        response = self.client.get(reverse("agency_close"))
        self.assertContains(response, "Общий объект", count=0)
        self.assertEqual(response.context["snapshot"]["members"], 2)
        self.assertEqual(response.context["snapshot"]["leads"], 1)
        self.assertEqual(response.context["snapshot"]["landings"], 1)
        self.assertEqual(response.context["snapshot"]["telegram_subscribers"], 1)
        self.agency.refresh_from_db()
        self.assertIsNone(self.agency.closed_at)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("agency_close")).status_code, 404)
        self.assertEqual(self.client.post(reverse("agency_close"), {"preview_token": token,
                                                            "confirm_name": self.agency.name}).status_code, 404)

    def test_settings_show_closure_explanations_in_help(self):
        response = self.client.get(reverse("agency_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-ui-help aria-label="Пояснение:', count=6)
        self.assertContains(response, 'class="ui-help-content" hidden', count=6)
        self.assertContains(response, "Доступ сотрудников прекратится.")
        self.assertContains(response, "Окончательное удаление CRM-записей и файлов выполняется отдельно")
        self.assertContains(response, "Работа команды остановится, данные останутся в архиве.")
        self.assertNotContains(response, '<p class="detail-muted mt-3">Доступ сотрудников прекратится.')

    def test_confirmation_requires_exact_name_and_fresh_preview(self):
        token = self.preview()
        self.assertEqual(self.client.post(reverse("agency_close"), {
            "preview_token": token, "confirm_name": "другое имя",
        }).status_code, 400)
        AgencyInvitation.objects.create(agency=self.agency, created_by=self.owner,
                                        expires_at=timezone.now() + timedelta(days=7))
        self.assertEqual(self.client.post(reverse("agency_close"), {
            "preview_token": token, "confirm_name": self.agency.name,
        }).status_code, 409)
        self.agency.refresh_from_db()
        self.assertIsNone(self.agency.closed_at)

    def test_rename_invalidates_closure_preview(self):
        token = self.preview()
        self.agency.name = "Новое название"
        self.agency.save(update_fields=["name"])
        response = self.client.post(reverse("agency_close"), {
            "preview_token": token, "confirm_name": self.agency.name,
        })
        self.assertEqual(response.status_code, 409)
        self.agency.refresh_from_db()
        self.assertIsNone(self.agency.closed_at)

    def test_close_preserves_archive_and_blocks_work_publication_and_bot(self):
        token = self.preview()
        self.assertRedirects(self.client.post(reverse("agency_close"), {
            "preview_token": token, "confirm_name": self.agency.name,
        }), reverse("agency_archive", args=[self.agency.pk]))
        self.agency.refresh_from_db()
        self.invite.refresh_from_db()
        self.delivery.refresh_from_db()
        self.outbound.refresh_from_db()
        self.subscriber.refresh_from_db()
        self.assertIsNotNone(self.agency.closed_at)
        self.assertEqual(self.agency.closure_snapshot["members"], 2)
        self.assertEqual(self.agency.memberships.count(), 0)
        self.assertIsNone(membership_for(self.owner))
        self.assertIsNone(agency_for(self.agent))
        self.assertIsNotNone(self.invite.revoked_at)
        self.assertEqual(self.delivery.attempts, 5)
        self.assertEqual(self.outbound.status, OutboundMessage.Status.CANCELLED)
        self.assertTrue(self.subscriber.active)
        self.assertFalse(is_visible(self.property))
        self.assertEqual(self.client.get(reverse("public_landing", args=[self.property.landing_slug])).status_code, 404)
        self.assertEqual(self.client.post(reverse("public_landing", args=[self.property.landing_slug]), {
            "name": "Поздний отклик", "phone": "+79990003344",
        }).status_code, 404)
        self.assertEqual(Lead.objects.filter(property=self.property).count(), 1)
        self.assertEqual(self.client.get(reverse("property_detail", args=[self.property.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("property_detail", args=[self.private.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("agency_join", args=[self.invite.code])).status_code, 404)
        self.assertEqual(self.client.get(reverse("agency_archive", args=[self.agency.pk])).status_code, 200)
        self.assertTrue(Property.objects.filter(pk=self.property.pk, agency=self.agency).exists())
        self.assertTrue(Lead.objects.filter(pk=self.lead.pk).exists())
        self.assertTrue(Agency.objects.filter(pk=self.agency.pk).exists())
        self.assertTrue(type(self.account).objects.filter(pk=self.account.pk, agency=self.agency).exists())
        self.assertEqual(self.client.post(reverse("telegram_publish", args=[self.property.pk]), {"action": "publish"}).status_code, 404)
        self.assertContains(self.client.get(reverse("agency_history") + f"?archive={self.agency.pk}"), "Команда")
        self.assertEqual(PolicyService.evaluate(self.account, self.owner, Operation.EDIT_CRM).code, "team_closed")
        with self.assertRaises(TariffDenied):
            require_new_action(self.owner, Operation.EDIT_CRM, agency=self.agency)
        reply = process_update({"update_id": 999, "message": {"from": {"id": 555},
            "chat": {"id": 555, "type": "private"}, "text": f"/start f{self.feed.code.hex}"}})
        self.assertIn("не доступна", reply[0][1]["text"])
        self.assertFalse(TelegramSubscriber.objects.filter(feed=self.feed, chat_id=555).exists())

    def test_archive_export_is_owner_only_and_former_member_can_join_new_team(self):
        token = self.preview()
        self.client.post(reverse("agency_close"), {"preview_token": token, "confirm_name": self.agency.name})
        export_url = reverse("agency_archive_export", args=[self.agency.pk, "clients"])
        result = self.client.get(export_url)
        self.assertEqual(result.status_code, 200)
        self.assertIn("Клиент", b"".join(result.streaming_content).decode("utf-8"))
        records_url = reverse("agency_archive_records", args=[self.agency.pk, "clients"])
        self.assertContains(self.client.get(records_url), "Клиент")
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("agency_archive", args=[self.agency.pk])).status_code, 404)
        self.assertEqual(self.client.get(export_url).status_code, 404)
        self.assertEqual(self.client.get(records_url).status_code, 404)
        new_agency = Agency.objects.create(owner=self.outsider, name="Новая команда")
        AgencyMembership.objects.create(agency=new_agency, user=self.outsider, role="owner")
        invite = AgencyInvitation.objects.create(agency=new_agency, created_by=self.outsider,
                                                 expires_at=timezone.now() + timedelta(days=7))
        self.assertRedirects(self.client.post(reverse("agency_join", args=[invite.code])), reverse("agency_dashboard"))
        self.assertEqual(membership_for(self.agent).agency_id, new_agency.pk)

    def test_owner_can_create_new_team_after_closure(self):
        token = self.preview()
        self.client.post(reverse("agency_close"), {"preview_token": token, "confirm_name": self.agency.name})
        self.assertRedirects(self.client.post(reverse("agency_create"), {"name": "Вторая команда"}), reverse("agency_dashboard"))
        self.assertEqual(Agency.objects.filter(owner=self.owner, closed_at__isnull=True).count(), 1)
        self.assertEqual(Agency.objects.filter(owner=self.owner, closed_at__isnull=False).count(), 1)
        self.assertEqual(membership_for(self.owner).agency.name, "Вторая команда")

    def test_file_download_is_limited_to_archive_owner(self):
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            name = default_storage.save("team-archive/photo.txt", ContentFile(b"saved media"))
            stored = StoredFile.objects.create(account=self.account, name=name, size_bytes=11,
                                               state=StoredFile.State.PRESENT)
            token = self.preview()
            self.client.post(reverse("agency_close"), {"preview_token": token, "confirm_name": self.agency.name})
            self.assertContains(self.client.get(reverse("agency_archive_files", args=[self.agency.pk])), name)
            file_url = reverse("agency_archive_file", args=[self.agency.pk, stored.pk])
            response = self.client.get(file_url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), b"saved media")
            self.client.force_login(self.agent)
            self.assertEqual(self.client.get(file_url).status_code, 404)
