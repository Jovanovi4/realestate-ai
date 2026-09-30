import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Agency, AgencyMembership, Client, ClientReminder, Lead, Property,
    TelegramConversation, TelegramDelivery, TelegramPublication, TelegramSubscriber,
)
from .services.telegram_public_service import send_listing


BOT_SETTINGS = {
    "TELEGRAM_PUBLIC_ENABLED": True,
    "TELEGRAM_PUBLIC_BOT_TOKEN": "test-token",
    "TELEGRAM_PUBLIC_BOT_USERNAME": "buyer_test_bot",
    "TELEGRAM_PUBLIC_WEBHOOK_SECRET": "test-webhook-secret",
    "ACCOUNT_PUBLIC_BASE_URL": "https://example.test",
}


@override_settings(**BOT_SETTINGS)
class TelegramPublicTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("telegram-owner", password="password")
        self.other = User.objects.create_user("telegram-other", password="password")
        self.property = Property.objects.create(owner=self.owner, title="Квартира у парка", status="available", price=100000)
        self.webhook = reverse("telegram_public_webhook")
        self.api_response = Mock()
        self.api_response.json.return_value = {"ok": True}
        self.api_response.raise_for_status.return_value = None
        self.api = patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response)
        self.api.start()
        self.addCleanup(self.api.stop)

    def update(self, number, payload, *, secret="test-webhook-secret"):
        return self.client.post(
            self.webhook, data=json.dumps({"update_id": number, **payload}),
            content_type="application/json", HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret,
        )

    @staticmethod
    def user_message(text=None, contact=None, user_id=1001):
        message = {"chat": {"id": user_id, "type": "private"}, "from": {"id": user_id, "first_name": "Анна"}}
        if text is not None:
            message["text"] = text
        if contact is not None:
            message["contact"] = contact
        return {"message": message}

    @staticmethod
    def callback(data, user_id=1001):
        return {"callback_query": {
            "id": "callback-id", "from": {"id": user_id}, "data": data,
            "message": {"chat": {"id": user_id, "type": "private"}},
        }}

    def publish(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("telegram_publish", args=[self.property.pk]), {"action": "publish"})
        self.assertEqual(response.status_code, 302)
        self.property.refresh_from_db()
        self.assertTrue(self.property.telegram_published)
        return response

    def subscribe(self):
        self.assertEqual(self.update(1, self.user_message(f"/start p{self.property.pk}")).status_code, 200)
        return TelegramSubscriber.objects.get(chat_id=1001)

    def test_webhook_requires_configuration_and_secret(self):
        self.assertEqual(self.update(1, self.user_message("/start"), secret="wrong").status_code, 403)
        with override_settings(TELEGRAM_PUBLIC_ENABLED=False):
            self.assertEqual(self.update(1, self.user_message("/start")).status_code, 503)
        self.assertEqual(TelegramSubscriber.objects.count(), 0)

    def test_only_owner_can_publish_real_available_property(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("telegram_publish", args=[self.property.pk]), {"action": "publish"}).status_code, 404)
        self.client.force_login(self.owner)
        self.property.status = "draft"
        self.property.save(update_fields=["status"])
        self.client.post(reverse("telegram_publish", args=[self.property.pk]), {"action": "publish"})
        self.property.refresh_from_db()
        self.assertFalse(self.property.telegram_published)
        self.property.status = "available"
        self.property.save(update_fields=["status"])
        self.publish()
        self.client.post(reverse("telegram_publish", args=[self.property.pk]), {"action": "publish"})
        self.assertEqual(TelegramPublication.objects.count(), 1)
        self.assertContains(self.client.get(reverse("property_detail", args=[self.property.pk])), f"start=p{self.property.pk}")

    def test_subscribe_consent_and_lead_are_idempotent(self):
        self.publish()
        subscriber = self.subscribe()
        self.assertEqual(subscriber.feed.owner, self.owner)
        self.assertEqual(self.update(2, self.callback(f"reply:{self.property.pk}")).status_code, 200)
        self.assertEqual(self.update(3, self.user_message(contact={"phone_number": "+7 999 000-00-01", "user_id": 1001})).status_code, 200)
        self.assertEqual(Lead.objects.count(), 0)
        self.assertEqual(TelegramConversation.objects.get(chat_id=1001).pending_phone, "+79990000001")
        self.assertEqual(self.update(4, self.callback(f"consent:{self.property.pk}")).status_code, 200)
        self.assertEqual(self.update(4, self.callback(f"consent:{self.property.pk}")).status_code, 200)
        self.assertEqual(Lead.objects.count(), 1)
        lead = Lead.objects.get()
        self.assertEqual(lead.source, "telegram_bot")
        self.assertEqual(lead.assigned_to, self.owner)
        self.assertEqual(lead.telegram_subscriber, subscriber)
        self.assertIsNotNone(lead.personal_data_consent_at)
        self.assertEqual(lead.client.source, "telegram_bot")
        self.assertEqual(ClientReminder.objects.filter(lead=lead, is_done=False).count(), 1)
        self.assertAlmostEqual((lead.next_action_due_at - lead.created_at).total_seconds(), 1800, delta=3)

    def test_foreign_contact_and_removed_listing_do_not_create_lead(self):
        self.publish()
        self.subscribe()
        self.update(2, self.callback(f"reply:{self.property.pk}"))
        self.update(3, self.user_message(contact={"phone_number": "+79990000001", "user_id": 9009}))
        self.assertEqual(TelegramConversation.objects.get(chat_id=1001).pending_phone, "")
        self.update(4, self.user_message("+79990000001"))
        self.client.force_login(self.owner)
        self.client.post(reverse("telegram_publish", args=[self.property.pk]), {"action": "unpublish"})
        self.update(5, self.callback(f"consent:{self.property.pk}"))
        self.assertEqual(Lead.objects.count(), 0)

    def test_reserved_property_is_automatically_removed_from_bot(self):
        self.publish()
        self.subscribe()
        self.property.status = "reserved"
        self.property.save(update_fields=["status"])
        self.property.refresh_from_db()
        self.assertFalse(self.property.telegram_published)
        self.update(2, self.callback(f"reply:{self.property.pk}"))
        self.assertFalse(TelegramConversation.objects.filter(chat_id=1001).exists())

    def test_existing_client_keeps_original_source_and_team_audience_is_isolated(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        team_property = Property.objects.create(owner=self.owner, agency=agency, title="Дом", status="available")
        self.publish()
        self.subscribe()
        self.client.force_login(self.owner)
        self.client.post(reverse("telegram_publish", args=[team_property.pk]), {"action": "publish"})
        self.assertEqual(TelegramDelivery.objects.count(), 0)
        self.update(10, self.callback(f"reply:{team_property.pk}"))
        self.assertFalse(TelegramConversation.objects.filter(chat_id=1001).exists())
        existing = Client.objects.create(owner=self.owner, name="Анна", phone="+79990000001", source="recommendation")
        self.update(2, self.callback(f"reply:{self.property.pk}"))
        self.update(3, self.user_message("+79990000001"))
        self.update(4, self.callback(f"consent:{self.property.pk}"))
        existing.refresh_from_db()
        self.assertEqual(existing.source, "recommendation")
        self.assertEqual(Lead.objects.get().client, existing)

    def test_delivery_queue_sends_once_and_skips_withdrawn_listing(self):
        self.publish()
        self.subscribe()
        second = Property.objects.create(owner=self.owner, title="Второй объект", status="available")
        self.client.force_login(self.owner)
        self.client.post(reverse("telegram_publish", args=[second.pk]), {"action": "publish"})
        delivery = TelegramDelivery.objects.get(publication__property=second)
        output = StringIO()
        call_command("send_telegram_publications", "--limit", "10", stdout=output)
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.sent_at)
        self.assertEqual(delivery.attempts, 1)
        call_command("send_telegram_publications", "--limit", "10", stdout=output)
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempts, 1)
        third = Property.objects.create(owner=self.owner, title="Третий объект", status="available")
        self.client.post(reverse("telegram_publish", args=[third.pk]), {"action": "publish"})
        self.client.post(reverse("telegram_publish", args=[third.pk]), {"action": "unpublish"})
        call_command("send_telegram_publications", "--limit", "10", stdout=output)
        cancelled = TelegramDelivery.objects.get(publication__property=third)
        self.assertIsNone(cancelled.sent_at)
        self.assertEqual(cancelled.attempts, 5)

    @patch.object(Property, "primary_image", new_callable=PropertyMock)
    def test_listing_photo_uses_public_https_url_without_media_disk(self, primary_image):
        self.publish()
        primary_image.return_value = SimpleNamespace(image=SimpleNamespace(url="/media/properties/photo.jpg"))
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.assertTrue(send_listing(1001, self.property))
        self.assertTrue(post.call_args.args[0].endswith("/sendPhoto"))
        self.assertEqual(post.call_args.kwargs["json"]["photo"], "https://example.test/media/properties/photo.jpg")

    def test_group_updates_are_ignored(self):
        self.publish()
        group = self.user_message(f"/start p{self.property.pk}")
        group["message"]["chat"] = {"id": -100, "type": "supergroup"}
        self.update(1, group)
        self.assertEqual(TelegramSubscriber.objects.count(), 0)

    @patch("properties.management.commands.register_telegram_public_webhook.requests.post")
    def test_webhook_registration_uses_separate_bot_and_secret(self, post):
        post.return_value = self.api_response
        output = StringIO()
        call_command("register_telegram_public_webhook", stdout=output)
        self.assertIn("https://api.telegram.org/bottest-token/setWebhook", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["data"]["url"], "https://example.test" + self.webhook)
        self.assertEqual(post.call_args.kwargs["data"]["secret_token"], "test-webhook-secret")
        self.assertNotIn("test-token", output.getvalue())

    def test_plain_start_subscribes_only_when_one_public_feed_exists(self):
        self.publish()
        self.update(1, self.user_message("/start"))
        self.assertEqual(TelegramSubscriber.objects.filter(chat_id=1001, active=True).count(), 1)
        other_property = Property.objects.create(owner=self.other, title="Другой риелтор", status="available")
        self.client.force_login(self.other)
        self.client.post(reverse("telegram_publish", args=[other_property.pk]), {"action": "publish"})
        self.update(2, self.user_message("/start", user_id=2002))
        self.assertFalse(TelegramSubscriber.objects.filter(chat_id=2002).exists())
