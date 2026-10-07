import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing.catalog import POLICY_VERSION
from billing.models import Plan, PlanVersion
from .models import (
    Agency, AgencyMembership, Client, ClientReminder, Lead, Property, RealtorProfile,
    TelegramConversation, TelegramDelivery, TelegramFeed, TelegramPublication, TelegramSubscriber,
)
from .services.telegram_public_service import (
    feed_unsubscribe_callback_data, property_callback_data, property_start_payload, send_actions, send_listing,
)


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
        self.assertEqual(response.url, f"{reverse('property_detail', args=[self.property.pk])}?tab=publication")
        self.property.refresh_from_db()
        self.assertTrue(self.property.telegram_published)
        return response

    def subscribe(self):
        self.assertEqual(self.update(1, self.user_message(f"/start {property_start_payload(self.property)}")).status_code, 200)
        return TelegramSubscriber.objects.get(chat_id=1001)

    def subscribe_to_personal_and_team(self):
        self.publish()
        personal = self.subscribe()
        agency = Agency.objects.create(owner=self.owner, name="Команда Север")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        team_property = Property.objects.create(owner=self.owner, agency=agency, title="Дом команды", status="available")
        self.client.post(reverse("telegram_publish", args=[team_property.pk]), {"action": "publish"})
        team_feed = TelegramFeed.objects.get(agency=agency)
        self.update(2, self.user_message(f"/start f{team_feed.code.hex}"))
        return personal, TelegramSubscriber.objects.get(feed=team_feed, chat_id=1001), team_property

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
        self.assertContains(self.client.get(reverse("property_detail", args=[self.property.pk])), f"start={property_start_payload(self.property)}")

    def test_old_bot_links_and_buttons_cannot_target_reused_pilot_ids(self):
        self.publish()
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(1, self.user_message(f"/start p{self.property.pk}"))
        self.assertIn("устарела", post.call_args.kwargs["json"]["text"])
        self.assertFalse(TelegramSubscriber.objects.exists())

        subscriber = TelegramSubscriber.objects.create(feed=TelegramFeed.objects.get(owner=self.owner), chat_id=1001, user_id=1001)
        self.update(2, self.callback(f"reply:{self.property.pk}"))
        self.assertFalse(TelegramConversation.objects.exists())
        self.update(3, self.callback(f"sub:off:{subscriber.feed_id}:0"))
        self.update(4, self.callback("sub:all"))
        subscriber.refresh_from_db()
        self.assertTrue(subscriber.active)

        self.update(5, self.callback(property_callback_data("reply", self.property)))
        self.update(6, self.user_message("+79990000001"))
        self.update(7, self.callback(f"consent:{self.property.pk}"))
        self.update(8, self.callback(f"cancel:{self.property.pk}"))
        self.assertEqual(TelegramConversation.objects.get(chat_id=1001).pending_property, self.property)
        self.assertFalse(Lead.objects.exists())

        tampered = property_start_payload(self.property)[:-1] + (
            "0" if property_start_payload(self.property)[-1] != "0" else "1"
        )
        self.update(9, self.user_message(f"/start {tampered}", user_id=2002))
        self.assertFalse(TelegramSubscriber.objects.filter(chat_id=2002).exists())

    @override_settings(BILLING_ENFORCEMENT_ENABLED=False)
    def test_telegram_entitlement_is_enforced_without_general_billing_limits(self):
        self.publish()  # Legacy Demo terms allow the current pilot.
        demo = PlanVersion.objects.get(plan__code=Plan.Code.DEMO, version=1)
        terms = {name: getattr(demo, name) for name in PlanVersion.TERMS}
        PlanVersion.objects.create(**{**terms, "version": 2, "policy_version": POLICY_VERSION, "telegram_publications_enabled": False})
        other = Property.objects.create(owner=self.owner, title="Следующий объект", status="available")
        detail = self.client.get(reverse("property_detail", args=[other.pk]))
        self.assertContains(detail, "Публикация в Telegram недоступна на вашем тарифе.")
        self.assertContains(detail, 'name="action" value="publish" disabled')
        self.client.post(reverse("telegram_publish", args=[other.pk]), {"action": "publish"})
        other.refresh_from_db()
        self.assertFalse(other.telegram_published)
        self.assertFalse(TelegramPublication.objects.filter(property=other).exists())
        self.client.post(reverse("telegram_publish", args=[self.property.pk]), {"action": "unpublish"})
        self.property.refresh_from_db()
        self.assertFalse(self.property.telegram_published)

    def test_personal_feed_link_is_stable_and_subscribes_before_first_publication(self):
        self.client.force_login(self.owner)
        integration_url = reverse("integration_telegram")
        first = self.client.get(integration_url)
        feed = TelegramFeed.objects.get(owner=self.owner)
        link = f"https://t.me/buyer_test_bot?start=f{feed.code.hex}"
        self.assertContains(first, link)
        self.assertContains(self.client.get(integration_url), link)
        self.assertNotContains(self.client.get(reverse("edit_profile")), link)
        self.assertContains(self.client.get(reverse("property_detail", args=[self.property.pk])), link)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(1, self.user_message(f"/start f{feed.code.hex}"))
        self.assertEqual(TelegramSubscriber.objects.get(chat_id=1001).feed, feed)
        self.assertEqual(len(post.call_args_list), 1)
        self.assertIn("Пока объявлений нет", post.call_args.kwargs["json"]["text"])
        self.publish()
        self.assertEqual(TelegramDelivery.objects.filter(subscriber__chat_id=1001).count(), 1)

    def test_team_feed_link_and_object_link_subscribe_to_their_own_feeds(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда Север")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        team_property = Property.objects.create(owner=self.owner, agency=agency, title="Дом команды", status="available")
        self.client.force_login(self.owner)
        self.publish()
        self.client.post(reverse("telegram_publish", args=[team_property.pk]), {"action": "publish"})
        personal_feed = TelegramFeed.objects.get(owner=self.owner)
        team_feed = TelegramFeed.objects.get(agency=agency)
        team_link = f"https://t.me/buyer_test_bot?start=f{team_feed.code.hex}"
        self.assertNotEqual(personal_feed.code, team_feed.code)
        self.assertContains(self.client.get(reverse("integration_telegram") + "?workspace=team"), team_link)
        self.assertNotContains(self.client.get(reverse("agency_dashboard")), team_link)
        self.assertContains(self.client.get(reverse("property_detail", args=[team_property.pk])), team_link)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(1, self.user_message(f"/start f{team_feed.code.hex}"))
        self.assertEqual(list(TelegramSubscriber.objects.filter(chat_id=1001).values_list("feed_id", flat=True)), [team_feed.pk])
        messages = [call.kwargs["json"].get("text", "") for call in post.call_args_list]
        self.assertTrue(any("Команда Север" in message for message in messages))
        self.assertTrue(any("Дом команды" in message for message in messages))
        self.assertFalse(any("Квартира у парка" in message for message in messages))
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(2, self.user_message(f"/start {property_start_payload(self.property)}", user_id=2002))
        self.assertEqual(list(TelegramSubscriber.objects.filter(chat_id=2002).values_list("feed_id", flat=True)), [personal_feed.pk])
        messages = [call.kwargs["json"].get("text", "") for call in post.call_args_list]
        self.assertTrue(any("Квартира у парка" in message for message in messages))
        self.assertFalse(any("Дом команды" in message for message in messages))

    def test_crm_feed_overview_shows_delivery_progress_without_publish_actions(self):
        self.client.force_login(self.owner)
        self.client.get(reverse("integration_telegram"))
        feed = TelegramFeed.objects.get(owner=self.owner)
        TelegramSubscriber.objects.create(feed=feed, chat_id=1001, user_id=1001, active=True)
        TelegramSubscriber.objects.create(feed=feed, chat_id=1002, user_id=1002, active=True)
        self.publish()
        publication = TelegramPublication.objects.get(property=self.property)
        detail_url = f"{reverse('property_detail', args=[self.property.pk])}?tab=publication"

        pending = self.client.get(reverse("integration_telegram"))
        self.assertContains(pending, "Лента объявлений")
        self.assertContains(pending, "Объектов: 1")
        self.assertContains(pending, "Подписчиков: 2")
        self.assertContains(pending, detail_url)
        self.assertContains(pending, "В очереди")
        self.assertNotContains(pending, 'name="action" value="unpublish"')

        first, second = list(publication.deliveries.order_by("pk"))
        first.sent_at = timezone.now()
        first.attempts = 1
        first.save(update_fields=["sent_at", "attempts"])
        second.attempts = 5
        second.last_error = "Timeout"
        second.save(update_fields=["attempts", "last_error"])
        failed = self.client.get(reverse("integration_telegram"))
        self.assertContains(failed, "Есть ошибки")
        self.assertContains(failed, "Отправлено: 1 · ожидает: 0 · ошибок: 1")

        second.last_error = "Подписка остановлена"
        second.save(update_fields=["last_error"])
        sent = self.client.get(reverse("integration_telegram"))
        self.assertContains(sent, 'class="telegram-feed-status telegram-feed-status--sent">Отправлено')
        self.assertNotContains(sent, 'class="telegram-feed-status telegram-feed-status--error">Есть ошибки')

    def test_crm_feed_overview_keeps_personal_and_team_listings_separate(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда Север")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        team_property = Property.objects.create(owner=self.owner, agency=agency, title="Дом команды", status="available")
        self.client.force_login(self.owner)
        self.publish()
        self.client.post(reverse("telegram_publish", args=[team_property.pk]), {"action": "publish"})
        personal = self.client.get(reverse("integration_telegram"))
        team = self.client.get(reverse("integration_telegram") + "?workspace=team")
        self.assertContains(personal, "Квартира у парка")
        self.assertNotContains(personal, "Дом команды")
        self.assertContains(team, "Дом команды")
        self.assertNotContains(team, "Квартира у парка")
        self.assertContains(team, "Без получателей")

        outsider = User.objects.create_user("unrelated-owner", password="password")
        self.client.force_login(outsider)
        outsider_page = self.client.get(reverse("integration_telegram") + "?workspace=team")
        self.assertEqual(outsider_page.context["telegram_workspace"], "personal")
        self.assertNotContains(outsider_page, 'aria-label="Пространство Telegram"')
        self.assertNotContains(outsider_page, "Квартира у парка")
        self.assertNotContains(outsider_page, "Дом команды")

    def test_crm_feed_overview_handles_transfer_pagination_and_disabled_bot(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда Север")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        self.client.force_login(self.owner)
        self.publish()
        self.property.agency = agency
        self.property.save(update_fields=["agency", "updated_at"])
        team = self.client.get(reverse("integration_telegram") + "?workspace=team")
        self.assertContains(team, "Квартира у парка")
        self.assertContains(team, "Без рассылки")
        self.assertNotContains(self.client.get(reverse("integration_telegram")), "Квартира у парка")

        for number in range(11):
            Property.objects.create(owner=self.owner, title=f"Личный объект {number}", status="available", telegram_published=True)
        first_page = self.client.get(reverse("integration_telegram"))
        second_page = self.client.get(reverse("integration_telegram") + "?workspace=personal&telegram_page=2")
        self.assertContains(first_page, "1 / 2")
        self.assertEqual(first_page.context["telegram_overview"]["page"].number, 1)
        self.assertEqual(second_page.context["telegram_overview"]["page"].number, 2)
        with override_settings(TELEGRAM_PUBLIC_ENABLED=False):
            disabled = self.client.get(reverse("integration_telegram"))
        self.assertContains(disabled, "Публичный бот пока не подключён")
        self.assertContains(disabled, "Личный объект")
        self.assertNotContains(disabled, "https://t.me/buyer_test_bot?start=f")

    def test_object_link_shows_requested_object_even_when_feed_has_newer_listing(self):
        self.publish()
        newer = Property.objects.create(owner=self.owner, title="Новый объект", status="available")
        self.client.post(reverse("telegram_publish", args=[newer.pk]), {"action": "publish"})
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(1, self.user_message(f"/start {property_start_payload(self.property)}"))
        self.assertEqual(TelegramSubscriber.objects.filter(chat_id=1001).count(), 1)
        messages = [call.kwargs["json"].get("text", "") for call in post.call_args_list]
        self.assertTrue(any("Квартира у парка" in message for message in messages))
        self.assertFalse(any("Новый объект" in message for message in messages))

    def test_unknown_feed_link_does_not_create_a_subscription(self):
        self.update(1, self.user_message("/start f" + "0" * 32))
        self.update(2, self.user_message("/start fnot-a-uuid"))
        self.assertFalse(TelegramSubscriber.objects.exists())

    def test_subscriptions_list_and_stop_allow_one_feed_to_be_disabled(self):
        personal, team, team_property = self.subscribe_to_personal_and_team()
        personal_new = Property.objects.create(owner=self.owner, title="Новое личное", status="available")
        team_new = Property.objects.create(owner=self.owner, agency=team.feed.agency, title="Новое команды", status="available")
        self.client.post(reverse("telegram_publish", args=[personal_new.pk]), {"action": "publish"})
        self.client.post(reverse("telegram_publish", args=[team_new.pk]), {"action": "publish"})
        personal_delivery = TelegramDelivery.objects.get(publication__property=personal_new)
        team_delivery = TelegramDelivery.objects.get(publication__property=team_new)
        self.update(3, self.callback(property_callback_data("reply", self.property)))
        self.assertEqual(TelegramConversation.objects.get(chat_id=1001).pending_property, self.property)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(4, self.user_message("/subscriptions"))
        panel = post.call_args.kwargs["json"]
        self.assertIn("Ваши подписки (2)", panel["text"])
        self.assertIn(f"риелтора №{self.owner.pk}", panel["text"])
        self.assertIn("Команда Север", panel["text"])
        callbacks = [button[0]["callback_data"] for button in panel["reply_markup"]["inline_keyboard"]]
        self.assertIn(feed_unsubscribe_callback_data(personal.feed_id, 0), callbacks)
        self.assertIn(feed_unsubscribe_callback_data(team.feed_id, 0), callbacks)
        self.assertIn("sub:v2:all", callbacks)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(5, self.user_message("/start"))
        self.assertIn("Ваши подписки (2)", post.call_args.kwargs["json"]["text"])
        self.update(6, self.user_message("/stop"))
        self.assertEqual(TelegramSubscriber.objects.filter(chat_id=1001, active=True).count(), 2)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(7, self.callback(feed_unsubscribe_callback_data(personal.feed_id, 0)))
        self.assertTrue(any(call.kwargs.get("json", {}).get("reply_markup") == {"remove_keyboard": True} for call in post.call_args_list))
        personal.refresh_from_db()
        team.refresh_from_db()
        personal_delivery.refresh_from_db()
        team_delivery.refresh_from_db()
        self.assertFalse(personal.active)
        self.assertTrue(team.active)
        self.assertEqual(personal_delivery.attempts, 5)
        self.assertEqual(personal_delivery.last_error, "Подписка остановлена")
        self.assertEqual(team_delivery.attempts, 0)
        self.assertIsNone(TelegramConversation.objects.get(chat_id=1001).pending_property_id)
        self.update(8, self.callback(property_callback_data("reply", self.property)))
        self.assertIsNone(TelegramConversation.objects.get(chat_id=1001).pending_property_id)
        self.update(9, self.callback(property_callback_data("reply", team_property)))
        self.assertEqual(TelegramConversation.objects.get(chat_id=1001).pending_property, team_property)
        call_command("send_telegram_publications", "--limit", "10", stdout=StringIO())
        personal_delivery.refresh_from_db()
        team_delivery.refresh_from_db()
        self.assertIsNone(personal_delivery.sent_at)
        self.assertIsNotNone(team_delivery.sent_at)
        self.update(10, self.user_message(f"/start {property_start_payload(self.property)}"))
        personal.refresh_from_db()
        self.assertTrue(personal.active)
        self.assertEqual(TelegramSubscriber.objects.filter(feed=personal.feed, chat_id=1001).count(), 1)
        self.assertEqual(TelegramDelivery.objects.get(pk=personal_delivery.pk).attempts, 5)

    def test_stop_with_one_feed_and_explicit_stop_all(self):
        self.publish()
        personal = self.subscribe()
        self.update(2, self.user_message("/stop"))
        personal.refresh_from_db()
        self.assertFalse(personal.active)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(3, self.user_message("/subscriptions"))
        self.assertIn("Вы пока не подписаны", post.call_args.kwargs["json"]["text"])
        self.update(4, self.user_message(f"/start {property_start_payload(self.property)}"))
        self.assertTrue(TelegramSubscriber.objects.get(pk=personal.pk).active)
        agency = Agency.objects.create(owner=self.owner, name="Команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        feed = TelegramFeed.objects.create(agency=agency)
        self.update(5, self.user_message(f"/start f{feed.code.hex}"))
        self.update(6, self.callback("sub:v2:all"))
        self.assertFalse(TelegramSubscriber.objects.filter(chat_id=1001, active=True).exists())
        self.update(7, self.user_message(f"/start {property_start_payload(self.property)}"))
        self.update(8, self.user_message("/stop all"))
        self.assertFalse(TelegramSubscriber.objects.filter(chat_id=1001, active=True).exists())

    def test_subscription_buttons_cannot_disable_another_buyers_feed(self):
        self.publish()
        subscriber = self.subscribe()
        self.update(2, self.callback(feed_unsubscribe_callback_data(subscriber.feed_id, 0), user_id=2002))
        self.update(3, self.callback("sub:v2:all", user_id=2002))
        subscriber.refresh_from_db()
        self.assertTrue(subscriber.active)

    def test_subscription_list_paginates_without_hiding_later_feeds(self):
        for number in range(9):
            owner = User.objects.create_user(f"feed-owner-{number}", first_name=f"Риелтор {number}")
            feed = TelegramFeed.objects.create(owner=owner)
            TelegramSubscriber.objects.create(feed=feed, chat_id=1001, user_id=1001)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(1, self.user_message("/subscriptions"))
        first = post.call_args.kwargs["json"]
        self.assertIn("Ваши подписки (9)", first["text"])
        self.assertIn("Страница 1 из 2", first["text"])
        self.assertIn("sub:v2:page:1", str(first["reply_markup"]))
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(2, self.callback("sub:v2:page:1"))
        second = [call.kwargs["json"] for call in post.call_args_list if call.args[0].endswith("/sendMessage")][0]
        self.assertIn("Страница 2 из 2", second["text"])
        self.assertEqual(len([row for row in second["reply_markup"]["inline_keyboard"] if row[0]["callback_data"].startswith("sub:off:")]), 1)

    def test_old_feed_does_not_show_or_send_property_moved_to_team(self):
        self.publish()
        self.subscribe()
        later = Property.objects.create(owner=self.owner, title="Переехавший объект", status="available")
        self.client.post(reverse("telegram_publish", args=[later.pk]), {"action": "publish"})
        delivery = TelegramDelivery.objects.get(publication__property=later)
        agency = Agency.objects.create(owner=self.owner, name="Новая команда")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        later.agency = agency
        later.save(update_fields=["agency"])
        personal_feed = TelegramFeed.objects.get(owner=self.owner)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(2, self.user_message(f"/start f{personal_feed.code.hex}", user_id=2002))
        messages = [call.kwargs["json"].get("text", "") for call in post.call_args_list]
        self.assertTrue(any("Квартира у парка" in message for message in messages))
        self.assertFalse(any("Переехавший объект" in message for message in messages))
        call_command("send_telegram_publications", "--limit", "10", stdout=StringIO())
        delivery.refresh_from_db()
        self.assertIsNone(delivery.sent_at)
        self.assertEqual(delivery.attempts, 5)
        team_feed = TelegramFeed.objects.create(agency=agency)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(3, self.user_message(f"/start f{team_feed.code.hex}", user_id=3003))
        self.assertEqual(TelegramSubscriber.objects.get(chat_id=3003).feed, team_feed)
        self.assertTrue(any("Переехавший объект" in call.kwargs["json"].get("text", "") for call in post.call_args_list))

    def test_subscribe_consent_and_lead_are_idempotent(self):
        self.publish()
        subscriber = self.subscribe()
        self.assertEqual(subscriber.feed.owner, self.owner)
        self.assertEqual(self.update(2, self.callback(property_callback_data("reply", self.property))).status_code, 200)
        self.assertEqual(self.update(3, self.user_message(contact={"phone_number": "+7 999 000-00-01", "user_id": 1001})).status_code, 200)
        self.assertEqual(Lead.objects.count(), 0)
        self.assertEqual(TelegramConversation.objects.get(chat_id=1001).pending_phone, "+79990000001")
        self.assertEqual(self.update(4, self.callback(property_callback_data("consent", self.property))).status_code, 200)
        self.assertEqual(self.update(4, self.callback(property_callback_data("consent", self.property))).status_code, 200)
        self.assertEqual(Lead.objects.count(), 1)
        lead = Lead.objects.get()
        self.assertEqual(lead.source, "telegram_bot")
        self.assertEqual(lead.assigned_to, self.owner)
        self.assertEqual(lead.telegram_subscriber, subscriber)
        self.assertIsNotNone(lead.personal_data_consent_at)
        self.assertEqual(lead.client.source, "telegram_bot")
        self.assertEqual(ClientReminder.objects.filter(lead=lead, is_done=False).count(), 1)
        self.assertAlmostEqual((lead.next_action_due_at - lead.created_at).total_seconds(), 1800, delta=3)

    def test_two_feeds_create_leads_for_their_own_responsible_realtors(self):
        agency = Agency.objects.create(owner=self.owner, name="Команда Север")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.other, role="agent")
        team_property = Property.objects.create(owner=self.other, agency=agency, title="Дом команды", status="available")
        owner_profile = RealtorProfile.objects.create(user=self.owner, email="owner@example.test")
        member_profile = RealtorProfile.objects.create(user=self.other, email="member@example.test")
        self.publish()
        self.client.post(reverse("telegram_publish", args=[team_property.pk]), {"action": "publish"})
        personal_subscriber = self.subscribe()
        team_feed = TelegramFeed.objects.get(agency=agency)
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(2, self.user_message(f"/start f{team_feed.code.hex}"))
        self.assertEqual(TelegramSubscriber.objects.filter(chat_id=1001, active=True).count(), 2)
        team_messages = [call.kwargs["json"].get("text", "") for call in post.call_args_list]
        self.assertTrue(any("Дом команды" in message for message in team_messages))
        self.assertFalse(any("Квартира у парка" in message for message in team_messages))

        self.update(3, self.callback(property_callback_data("reply", self.property)))
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(4, self.user_message("+79990000001"))
        consent_panel = post.call_args.kwargs["json"]
        keyboard = consent_panel["reply_markup"]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["callback_data"], property_callback_data("consent", self.property))
        self.assertTrue(any(button.get("url", "").endswith(reverse("personal_data_consent")) for row in keyboard for button in row))
        self.assertTrue(any(button.get("url", "").endswith(reverse("privacy_policy")) for row in keyboard for button in row))
        self.assertFalse(Lead.objects.exists())
        self.update(5, self.callback(property_callback_data("consent", team_property)))
        self.assertFalse(Lead.objects.exists())
        with patch("properties.services.telegram_public_service.LeadNotificationService.notify_new_lead") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.update(6, self.callback(property_callback_data("consent", self.property)))
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[1], owner_profile)
        personal_lead = Lead.objects.get(property=self.property)

        self.update(7, self.callback(property_callback_data("reply", team_property)))
        self.update(8, self.user_message("+79990000001"))
        self.update(9, self.callback(property_callback_data("cancel", self.property)))
        self.assertEqual(TelegramConversation.objects.get(chat_id=1001).pending_property, team_property)
        with patch("properties.services.telegram_public_service.LeadNotificationService.notify_new_lead") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.update(10, self.callback(property_callback_data("consent", team_property)))
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args[1], member_profile)
        team_lead = Lead.objects.get(property=team_property)
        self.assertEqual(personal_lead.assigned_to, self.owner)
        self.assertEqual(team_lead.assigned_to, self.other)
        self.assertEqual(personal_lead.source, "telegram_bot")
        self.assertEqual(team_lead.source, "telegram_bot")
        self.assertEqual(personal_lead.client.source, "telegram_bot")
        self.assertEqual(team_lead.client.source, "telegram_bot")
        self.assertIsNone(personal_lead.client.agency_id)
        self.assertEqual(team_lead.client.agency, agency)
        self.assertNotEqual(personal_lead.client, team_lead.client)
        self.assertEqual(personal_lead.telegram_subscriber, personal_subscriber)
        self.assertEqual(team_lead.telegram_subscriber.feed, team_feed)
        for lead in (personal_lead, team_lead):
            self.assertIsNotNone(lead.personal_data_consent_at)
            self.assertEqual(lead.personal_data_consent_version, settings.PERSONAL_DATA_CONSENT_VERSION)
            self.assertEqual(lead.action_tasks.get(is_done=False).owner, lead.assigned_to)
        third_owner = User.objects.create_user("third-feed-owner", password="password")
        third_property = Property.objects.create(
            owner=third_owner, title="Объект чужой ленты", status="available", telegram_published=True,
        )
        self.update(11, self.callback(property_callback_data("reply", third_property)))
        self.assertIsNone(TelegramConversation.objects.get(chat_id=1001).pending_property_id)
        self.assertEqual(Lead.objects.count(), 2)

    def test_duplicate_webhook_event_does_not_repeat_reply_or_create_lead(self):
        self.publish()
        self.subscribe()
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            self.update(2, self.callback(property_callback_data("reply", self.property)))
            self.update(2, self.callback(property_callback_data("reply", self.property)))
        self.assertEqual(post.call_count, 2)  # Callback answer and one phone request.
        self.update(3, self.user_message("+79990000001"))
        with patch("properties.services.telegram_public_service.LeadNotificationService.notify_new_lead") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.update(4, self.callback(property_callback_data("consent", self.property)))
                self.update(4, self.callback(property_callback_data("consent", self.property)))
                self.update(5, self.callback(property_callback_data("consent", self.property)))
            notify.assert_called_once()
        self.assertEqual(Lead.objects.count(), 1)
        self.assertEqual(ClientReminder.objects.filter(lead=Lead.objects.get(), is_done=False).count(), 1)

    def test_queued_listing_is_rechecked_before_bot_sends_it(self):
        self.publish()
        subscriber = self.subscribe()
        feed = subscriber.feed
        action = ("listing", {"chat_id": 1001, "property_id": self.property.pk, "feed_id": feed.pk})
        agency = Agency.objects.create(owner=self.owner, name="Другая лента")
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        team_feed = TelegramFeed.objects.create(agency=agency)
        TelegramSubscriber.objects.create(feed=team_feed, chat_id=1001, user_id=1001)
        wrong_feed_action = ("listing", {"chat_id": 1001, "property_id": self.property.pk, "feed_id": team_feed.pk})
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            send_actions([wrong_feed_action])
        post.assert_not_called()
        self.property.telegram_published = False
        self.property.save(update_fields=["telegram_published"])
        with patch("properties.services.telegram_public_service.requests.post", return_value=self.api_response) as post:
            send_actions([action])
        post.assert_not_called()

    def test_foreign_contact_and_removed_listing_do_not_create_lead(self):
        self.publish()
        self.subscribe()
        self.update(2, self.callback(property_callback_data("reply", self.property)))
        self.update(3, self.user_message(contact={"phone_number": "+79990000001", "user_id": 9009}))
        self.assertEqual(TelegramConversation.objects.get(chat_id=1001).pending_phone, "")
        self.update(4, self.user_message("+79990000001"))
        self.client.force_login(self.owner)
        self.client.post(reverse("telegram_publish", args=[self.property.pk]), {"action": "unpublish"})
        self.update(5, self.callback(property_callback_data("consent", self.property)))
        self.assertEqual(Lead.objects.count(), 0)

    def test_reserved_property_is_automatically_removed_from_bot(self):
        self.publish()
        self.subscribe()
        self.property.status = "reserved"
        self.property.save(update_fields=["status"])
        self.property.refresh_from_db()
        self.assertFalse(self.property.telegram_published)
        self.update(2, self.callback(property_callback_data("reply", self.property)))
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
        self.update(10, self.callback(property_callback_data("reply", team_property)))
        self.assertFalse(TelegramConversation.objects.filter(chat_id=1001).exists())
        existing = Client.objects.create(owner=self.owner, name="Анна", phone="+79990000001", source="recommendation")
        self.update(2, self.callback(property_callback_data("reply", self.property)))
        self.update(3, self.user_message("+79990000001"))
        self.update(4, self.callback(property_callback_data("consent", self.property)))
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
        group = self.user_message(f"/start {property_start_payload(self.property)}")
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

    def test_plain_start_never_subscribes_to_an_unselected_feed(self):
        self.publish()
        self.update(1, self.user_message("/start"))
        self.assertFalse(TelegramSubscriber.objects.filter(chat_id=1001, active=True).exists())
        other_property = Property.objects.create(owner=self.other, title="Другой риелтор", status="available")
        self.client.force_login(self.other)
        self.client.post(reverse("telegram_publish", args=[other_property.pk]), {"action": "publish"})
        self.update(2, self.user_message("/start", user_id=2002))
        self.assertFalse(TelegramSubscriber.objects.filter(chat_id=2002).exists())
