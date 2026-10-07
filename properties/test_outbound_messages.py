"""Recovery behavior for the persisted outbound queue."""

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import requests
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Lead, OutboundMessage, Property, RealtorProfile, TelegramInboundUpdate, TelegramSubscriber
from .services.lead_notification_service import LeadNotificationService
from .services.outbound_message_service import deliver_message, enqueue_public_actions
from .services.telegram_public_service import property_start_payload
from .services.overdue_task_notification_service import OverdueTaskNotificationService


BOT_SETTINGS = {
    "TELEGRAM_PUBLIC_ENABLED": True,
    "TELEGRAM_PUBLIC_BOT_TOKEN": "test-public-token",
    "TELEGRAM_PUBLIC_BOT_USERNAME": "test_public_bot",
    "TELEGRAM_PUBLIC_WEBHOOK_SECRET": "test-webhook-secret",
    "ACCOUNT_PUBLIC_BASE_URL": "https://example.test",
}


def telegram_http_error_response(token):
    response = requests.Response()
    response.status_code = 500
    response.reason = "Server Error"
    response.url = f"https://api.telegram.org/bot{token}/sendMessage"
    response.request = requests.Request("POST", response.url).prepare()
    return response


@override_settings(**BOT_SETTINGS)
class OutboundMessageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("outbox-owner", password="password")
        self.property = Property.objects.create(
            owner=self.owner, title="Тестовый объект", status="available", telegram_published=True,
        )

    def webhook(self, update_id):
        return self.client.post(
            reverse("telegram_public_webhook"),
            data=json.dumps({"update_id": update_id, "message": {
                "chat": {"id": 1001, "type": "private"},
                "from": {"id": 1001, "first_name": "Анна"},
                "text": f"/start {property_start_payload(self.property)}",
            }}),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="test-webhook-secret",
        )

    def test_failed_bot_reply_survives_duplicate_webhook_and_worker_retries(self):
        with patch("properties.services.outbound_message_service._send_public", side_effect=requests.Timeout("private URL")):
            self.assertEqual(self.webhook(900).status_code, 200)
            self.assertEqual(self.webhook(900).status_code, 200)
        self.assertEqual(TelegramInboundUpdate.objects.filter(update_id=900).count(), 1)
        self.assertEqual(TelegramSubscriber.objects.count(), 1)
        self.assertEqual(OutboundMessage.objects.count(), 2)
        self.assertTrue(all(message.status == OutboundMessage.Status.PENDING and message.attempts == 1
                            for message in OutboundMessage.objects.all()))
        self.assertTrue(all(message.last_error == "Timeout" for message in OutboundMessage.objects.all()))

        OutboundMessage.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        with patch("properties.services.outbound_message_service._send_public") as sender:
            call_command("send_outbound_messages", stdout=StringIO())
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(OutboundMessage.objects.filter(status=OutboundMessage.Status.SENT).count(), 2)
        self.assertEqual(OutboundMessage.objects.filter(attempts=2).count(), 2)

    @override_settings(TELEGRAM_PUBLIC_BOT_TOKEN="PRIVATE_TEST_TOKEN")
    def test_provider_error_does_not_store_or_log_public_bot_token(self):
        response = telegram_http_error_response("PRIVATE_TEST_TOKEN")
        with patch("properties.services.telegram_public_service.requests.post", return_value=response):
            with self.assertLogs("properties.services.outbound_message_service", level="WARNING") as captured:
                self.assertEqual(self.webhook(901).status_code, 200)
        errors = " ".join(OutboundMessage.objects.values_list("last_error", flat=True))
        self.assertIn("HTTP 500", errors)
        self.assertNotIn("PRIVATE_TEST_TOKEN", errors)
        self.assertNotIn("api.telegram.org", errors)
        self.assertNotIn("PRIVATE_TEST_TOKEN", " ".join(captured.output))
        self.assertNotIn("api.telegram.org", " ".join(captured.output))
        self.assertTrue(all(record.exc_info is None for record in captured.records))

    @override_settings(TELEGRAM_NOTIFICATIONS_ENABLED=True, TELEGRAM_BOT_TOKEN="PRIVATE_TEST_TOKEN",
                       EMAIL_NOTIFICATIONS_ENABLED=False)
    def test_new_lead_telegram_error_does_not_store_or_log_bot_token(self):
        profile = RealtorProfile.objects.create(user=self.owner, telegram_chat_id="1001")
        lead = Lead.objects.create(property=self.property, name="Клиент", phone="+79990000001")
        response = telegram_http_error_response("PRIVATE_TEST_TOKEN")
        with patch("properties.services.outbound_message_service.requests.post", return_value=response):
            with self.assertLogs("properties.services.outbound_message_service", level="WARNING") as captured:
                with self.captureOnCommitCallbacks(execute=True):
                    LeadNotificationService.notify_new_lead(lead, profile, "https://example.test/leads/1/")
        message = OutboundMessage.objects.get(dedupe_key=f"lead:{lead.pk}:staff_telegram")
        self.assertEqual(message.last_error, "HTTPError HTTP 500")
        self.assertNotIn("PRIVATE_TEST_TOKEN", " ".join(captured.output))
        self.assertNotIn("api.telegram.org", " ".join(captured.output))
        self.assertTrue(all(record.exc_info is None for record in captured.records))

    def test_five_failures_are_visible_and_operator_can_retry_one(self):
        with patch("properties.services.outbound_message_service._send_public", side_effect=requests.Timeout()):
            self.webhook(902)
            for _ in range(4):
                OutboundMessage.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
                call_command("send_outbound_messages", stdout=StringIO())
        self.assertEqual(OutboundMessage.objects.filter(status=OutboundMessage.Status.FAILED, attempts=5).count(), 2)
        first = OutboundMessage.objects.first()
        with patch("properties.services.outbound_message_service._send_public"):
            call_command("send_outbound_messages", "--retry-id", str(first.pk), stdout=StringIO())
        first.refresh_from_db()
        self.assertEqual(first.status, OutboundMessage.Status.SENT)
        self.assertEqual(first.attempts, 1)
        self.assertEqual(OutboundMessage.objects.filter(status=OutboundMessage.Status.FAILED).count(), 1)

    def test_stale_listing_is_cancelled_before_retry(self):
        with patch("properties.services.outbound_message_service._send_public"):
            self.webhook(903)
        subscriber = TelegramSubscriber.objects.get()
        queued = enqueue_public_actions(904, [("listing", {
            "chat_id": 1001, "property_id": self.property.pk, "feed_id": subscriber.feed_id,
        })])[0]
        self.property.telegram_published = False
        self.property.save(update_fields=["telegram_published"])
        with patch("properties.services.telegram_public_service._api") as sender:
            deliver_message(queued)
        sender.assert_not_called()
        message = OutboundMessage.objects.get(pk=queued)
        self.assertEqual(message.status, OutboundMessage.Status.CANCELLED)
        self.assertEqual(message.payload, {})

    def test_enqueue_rolls_back_with_incoming_update(self):
        try:
            with transaction.atomic():
                TelegramInboundUpdate.objects.create(update_id=905)
                enqueue_public_actions(905, [("sendMessage", {"chat_id": 1001, "text": "Привет"})])
                raise ValueError("rollback")
        except ValueError:
            pass
        self.assertFalse(TelegramInboundUpdate.objects.filter(update_id=905).exists())
        self.assertFalse(OutboundMessage.objects.filter(dedupe_key="public-update:905:0").exists())

    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=True, DEFAULT_FROM_EMAIL="noreply@example.test")
    def test_new_lead_email_is_queued_and_retried(self):
        profile = RealtorProfile.objects.create(user=self.owner, email="agent@example.test")
        lead = Lead.objects.create(property=self.property, name="Клиент", phone="+79990000001")
        with patch("properties.services.outbound_message_service.send_mail", side_effect=OSError("private details")):
            with self.captureOnCommitCallbacks(execute=True):
                LeadNotificationService.notify_new_lead(lead, profile, "https://example.test/leads/1/")
        message = OutboundMessage.objects.get(dedupe_key=f"lead:{lead.pk}:email")
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)
        self.assertEqual(message.last_error, "OSError")
        self.assertNotIn("private details", message.last_error)
        OutboundMessage.objects.filter(pk=message.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        with patch("properties.services.outbound_message_service.send_mail", return_value=1) as send:
            call_command("send_outbound_messages", stdout=StringIO())
        message.refresh_from_db()
        self.assertEqual(message.status, OutboundMessage.Status.SENT)
        self.assertEqual(message.payload, {})
        self.assertEqual(send.call_args.kwargs["recipient_list"], ["agent@example.test"])

    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=True, DEFAULT_FROM_EMAIL="noreply@example.test")
    def test_landing_keeps_lead_and_queued_notification_when_email_fails(self):
        RealtorProfile.objects.create(user=self.owner, email="agent@example.test")
        self.property.landing_published = True
        self.property.save(update_fields=["landing_published"])
        with patch("properties.services.outbound_message_service.send_mail", side_effect=OSError("SMTP down")):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("public_landing", args=[self.property.landing_slug]), {
                    "contact_purpose": "viewing", "name": "Мария", "phone": "+79990000002",
                    "personal_data_consent": "on",
                })
        self.assertEqual(response.status_code, 200)
        lead = Lead.objects.get(name="Мария")
        message = OutboundMessage.objects.get(dedupe_key=f"lead:{lead.pk}:email")
        self.assertEqual(message.status, OutboundMessage.Status.PENDING)
        self.assertEqual(message.attempts, 1)

    @override_settings(TELEGRAM_NOTIFICATIONS_ENABLED=True, TELEGRAM_BOT_TOKEN="PRIVATE_TEST_TOKEN")
    def test_overdue_notification_error_log_does_not_include_bot_token(self):
        response = telegram_http_error_response("PRIVATE_TEST_TOKEN")
        with patch("properties.services.overdue_task_notification_service.requests.post",
                   return_value=response):
            with self.assertLogs("properties.services.overdue_task_notification_service", level="WARNING") as captured:
                self.assertFalse(OverdueTaskNotificationService._send_telegram("1001", "Тест", user_id=self.owner.pk))
        self.assertIn(f"user {self.owner.pk} (HTTPError)", " ".join(captured.output))
        self.assertNotIn("PRIVATE_TEST_TOKEN", " ".join(captured.output))
        self.assertNotIn("api.telegram.org", " ".join(captured.output))
        self.assertTrue(all(record.exc_info is None for record in captured.records))
