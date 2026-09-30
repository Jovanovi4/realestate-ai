"""Buyer-facing Telegram catalogue and lead intake, separate from staff alerts."""

import json
import logging
from datetime import timedelta
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from billing.enforcement import TariffDenied, require_new_action
from billing.rules import Operation
from ..models import (
    AgencyMembership, Client, ClientInteraction, Lead, Property, RealtorProfile, TelegramConversation, TelegramDelivery,
    TelegramFeed, TelegramInboundUpdate, TelegramPublication, TelegramSubscriber,
)
from .client_import_service import canonical_phone
from .lead_notification_service import LeadNotificationService


logger = logging.getLogger(__name__)
VISIBLE_STATUSES = {"published", "available", "showing"}


def is_visible(property):
    return property.telegram_published and property.status in VISIBLE_STATUSES and not property.is_demo


def feed_for(property):
    if property.agency_id:
        return TelegramFeed.objects.get_or_create(agency=property.agency)[0]
    return TelegramFeed.objects.get_or_create(owner=property.owner)[0]


def publish(property):
    """Queue one announcement per current subscriber after an explicit CRM action."""
    feed = feed_for(property)
    if property.telegram_published:
        return None
    property.telegram_published = True
    property.save(update_fields=["telegram_published", "updated_at"])
    publication = TelegramPublication.objects.create(property=property, feed=feed)
    TelegramDelivery.objects.bulk_create([
        TelegramDelivery(publication=publication, subscriber=subscriber)
        for subscriber in feed.subscribers.filter(active=True).iterator()
    ], batch_size=500)
    return publication


def _keyboard(property):
    return {"inline_keyboard": [[{"text": "Откликнуться", "callback_data": f"reply:{property.pk}"}]]}


def listing_text(property):
    parts = [property.marketing_headline or property.title]
    if property.price is not None:
        parts.append(f"{property.price:,.0f} {property.currency}")
    facts = []
    if property.area:
        facts.append(f"{property.area} м²")
    if property.rooms:
        facts.append(f"{property.rooms} комн.")
    if property.address:
        facts.append(property.address)
    if facts:
        parts.append(" · ".join(facts))
    description = (property.short_description or property.description).strip()
    if description:
        parts.append(description[:450])
    return "\n".join(parts)[:950]


def _api(method, payload, *, photo=None):
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_PUBLIC_BOT_TOKEN}/{method}"
    if photo:
        with photo.open("rb") as source:
            response = requests.post(url, data=payload, files={"photo": source}, timeout=15)
    else:
        response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()
    if not response.json().get("ok"):
        raise ValueError("Telegram rejected the request")


def send_listing(chat_id, property):
    if not is_visible(property):
        return False
    payload = {"chat_id": chat_id, "reply_markup": json.dumps(_keyboard(property))}
    image = property.primary_image
    if image and image.image:
        photo_url = urljoin(settings.ACCOUNT_PUBLIC_BASE_URL.rstrip("/") + "/", image.image.url)
        if photo_url.startswith("https://"):
            try:
                _api("sendPhoto", {**payload, "photo": photo_url, "caption": listing_text(property)})
                return True
            except (requests.RequestException, ValueError) as error:
                logger.warning("Could not send Telegram listing photo URL for property %s (%s); trying local file", property.pk, type(error).__name__)
        try:
            _api("sendPhoto", {**payload, "caption": listing_text(property)}, photo=image.image)
            return True
        except (OSError, requests.RequestException, ValueError) as error:
            logger.warning("Could not send Telegram listing photo for property %s (%s); using text", property.pk, type(error).__name__)
    _api("sendMessage", {**payload, "text": listing_text(property)})
    return True


def _display_name(user):
    return " ".join(filter(None, [str(user.get("first_name", "")), str(user.get("last_name", ""))]))[:150]


def _subscribe(feed, chat_id, user):
    subscriber, _ = TelegramSubscriber.objects.update_or_create(
        feed=feed, chat_id=chat_id,
        defaults={"user_id": user["id"], "display_name": _display_name(user), "active": True},
    )
    return subscriber


def _create_lead(property, subscriber, phone):
    responsible = property.owner
    if property.agency_id and not AgencyMembership.objects.filter(agency=property.agency, user=responsible).exists():
        responsible = property.agency.owner
    require_new_action(responsible, Operation.EDIT_CRM, agency=property.agency)
    lookup = {"agency": property.agency, "phone": phone}
    if not property.agency_id:
        lookup["owner"] = responsible
    client, _ = Client.objects.get_or_create(
        **lookup,
        defaults={"owner": responsible, "name": subscriber.display_name or "Клиент Telegram", "source": "telegram_bot"},
    )
    lead = Lead.objects.create(
        property=property, client=client, telegram_subscriber=subscriber,
        assigned_to=responsible, name=subscriber.display_name or client.name,
        phone=phone, source="telegram_bot",
        personal_data_consent_at=timezone.now(),
        personal_data_consent_version=settings.PERSONAL_DATA_CONSENT_VERSION,
        interest_type="long_rent" if property.deal_type == "rent" else "buy",
        next_action_due_at=timezone.now() + timedelta(minutes=30),
    )
    ClientInteraction.objects.create(
        client=client, lead=lead, interaction_type="note",
        text=f"Отклик через Telegram-бота на объект «{property.title}».",
    )
    profile = RealtorProfile.objects.filter(user=responsible).first()
    lead_url = settings.ACCOUNT_PUBLIC_BASE_URL.rstrip("/") + reverse("lead_detail", kwargs={"pk": lead.pk})
    transaction.on_commit(lambda: LeadNotificationService.notify_new_lead(lead, profile, lead_url))
    return lead


def process_update(update):
    """Return Bot API actions; a unique update ID prevents duplicate CRM writes."""
    update_id = update.get("update_id")
    if type(update_id) is not int or not 0 <= update_id < 2**63:
        return []
    with transaction.atomic():
        _, created = TelegramInboundUpdate.objects.get_or_create(update_id=update_id)
        if not created:
            return []
        callback = update.get("callback_query")
        message = update.get("message")
        if callback:
            user = callback.get("from") or {}
            origin = callback.get("message") or {}
            chat = origin.get("chat") or {}
            chat_id = chat.get("id")
            actions = [("answerCallbackQuery", {"callback_query_id": callback.get("id")})]
            if chat.get("type") != "private" or chat_id != user.get("id"):
                return actions
            data = callback.get("data") or ""
            if data.startswith("cancel:") and data[7:].isdigit():
                TelegramConversation.objects.filter(chat_id=chat_id).update(pending_property=None, pending_phone="", pending_at=None)
                return actions + [("sendMessage", {"chat_id": chat_id, "text": "Заявка отменена. Вы можете откликнуться на другое объявление.", "reply_markup": {"remove_keyboard": True}})]
            if data.startswith("consent:") and data[8:].isdigit():
                conversation = TelegramConversation.objects.select_for_update().filter(chat_id=chat_id, pending_property_id=int(data[8:])).first()
                if not conversation or not conversation.pending_phone or not conversation.pending_at or conversation.pending_at < timezone.now() - timedelta(days=1):
                    return actions + [("sendMessage", {"chat_id": chat_id, "text": "Срок подтверждения истёк. Откликнитесь на объявление ещё раз."})]
                property = conversation.pending_property
                if not is_visible(property):
                    return actions + [("sendMessage", {"chat_id": chat_id, "text": "Объявление больше не доступно."})]
                subscriber = TelegramSubscriber.objects.filter(feed=feed_for(property), chat_id=chat_id, user_id=user.get("id"), active=True).first()
                if not subscriber:
                    return actions
                phone, pending_at = conversation.pending_phone, conversation.pending_at
                claimed = TelegramConversation.objects.filter(
                    pk=conversation.pk, pending_property=property, pending_phone=phone, pending_at=pending_at,
                ).update(pending_property=None, pending_phone="", pending_at=None)
                if not claimed:
                    return actions
                if not Lead.objects.filter(telegram_subscriber=subscriber, property=property, created_at__gte=timezone.now() - timedelta(minutes=10)).exists():
                    try:
                        _create_lead(property, subscriber, phone)
                    except TariffDenied:
                        TelegramConversation.objects.filter(pk=conversation.pk).update(
                            pending_property=property, pending_phone=phone, pending_at=pending_at,
                        )
                        return actions + [("sendMessage", {"chat_id": chat_id, "text": "Сейчас не удалось принять заявку. Попробуйте позже."})]
                return actions + [("sendMessage", {"chat_id": chat_id, "text": "Спасибо! Заявка передана риелтору. Он свяжется с вами по указанному номеру.", "reply_markup": {"remove_keyboard": True}})]
            if not data.startswith("reply:") or not data[6:].isdigit():
                return actions
            property = Property.objects.filter(pk=int(data[6:])).first()
            if not property or not is_visible(property):
                return actions + [("sendMessage", {"chat_id": chat_id, "text": "Объявление больше не доступно."})]
            subscriber = TelegramSubscriber.objects.filter(feed=feed_for(property), chat_id=chat_id, active=True).first()
            if not subscriber:
                return actions + [("sendMessage", {"chat_id": chat_id, "text": "Откройте ссылку на объявление и запустите бота, чтобы подписаться."})]
            TelegramConversation.objects.update_or_create(
                chat_id=chat_id, defaults={"pending_property": property, "pending_phone": "", "pending_at": timezone.now()},
            )
            return actions + [("sendMessage", {
                "chat_id": chat_id,
                "text": f"Чтобы риелтор связался с вами по объекту «{property.title}», отправьте свой телефон.",
                "reply_markup": {"keyboard": [[{"text": "Поделиться телефоном", "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True},
            })]
        if not message:
            return []
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat.get("type") != "private" or chat_id != user.get("id"):
            return []
        text = (message.get("text") or "").strip()
        if text.startswith("/stop"):
            TelegramSubscriber.objects.filter(chat_id=chat_id).update(active=False)
            TelegramConversation.objects.filter(chat_id=chat_id).update(pending_property=None, pending_phone="", pending_at=None)
            return [("sendMessage", {"chat_id": chat_id, "text": "Рассылка объявлений остановлена. Чтобы подписаться снова, откройте ссылку на объект."})]
        if text.startswith("/start"):
            TelegramConversation.objects.filter(chat_id=chat_id).update(pending_property=None, pending_phone="", pending_at=None)
            token = text.partition(" ")[2].strip()
            if token.startswith("p") and token[1:].isdigit():
                property = Property.objects.filter(pk=int(token[1:])).first()
                if property and is_visible(property):
                    _subscribe(feed_for(property), chat_id, user)
                    return [("sendMessage", {"chat_id": chat_id, "text": "Вы подписаны на новые объекты. Откликнуться можно кнопкой под объявлением; остановить рассылку — /stop."}),
                            ("listing", {"chat_id": chat_id, "property_id": property.pk})]
            if not token:
                feeds = list(TelegramFeed.objects.filter(
                    publications__property__telegram_published=True,
                    publications__property__status__in=VISIBLE_STATUSES,
                    publications__property__is_demo=False,
                ).distinct()[:2])
                if len(feeds) == 1:
                    _subscribe(feeds[0], chat_id, user)
                    latest = TelegramPublication.objects.filter(
                        feed=feeds[0], property__telegram_published=True,
                        property__status__in=VISIBLE_STATUSES, property__is_demo=False,
                    ).order_by("-pk").first()
                    return [("sendMessage", {"chat_id": chat_id, "text": "Вы подписаны на новые объекты. Откликнуться можно кнопкой под объявлением; остановить рассылку — /stop."}),
                            ("listing", {"chat_id": chat_id, "property_id": latest.property_id})]
            return [("sendMessage", {"chat_id": chat_id, "text": "Откройте ссылку на опубликованный объект, чтобы получать объявления и оставлять заявки."})]
        conversation = TelegramConversation.objects.select_for_update().filter(chat_id=chat_id).first()
        if not conversation or not conversation.pending_property_id or not conversation.pending_at or conversation.pending_at < timezone.now() - timedelta(days=1):
            return []
        contact = message.get("contact") or {}
        if contact and contact.get("user_id") != user.get("id"):
            return [("sendMessage", {"chat_id": chat_id, "text": "Пожалуйста, отправьте свой номер или введите его вручную."})]
        if not contact and not text:
            return []
        try:
            phone = canonical_phone(contact.get("phone_number", "") if contact else text)
        except (ValidationError, AttributeError, TypeError):
            return [("sendMessage", {"chat_id": chat_id, "text": "Не удалось прочитать номер. Отправьте свой контакт кнопкой или введите номер в международном формате."})]
        property = conversation.pending_property
        subscriber = TelegramSubscriber.objects.filter(feed=feed_for(property), chat_id=chat_id, active=True).first()
        if not is_visible(property) or not subscriber:
            conversation.pending_property = None
            conversation.pending_phone = ""
            conversation.pending_at = None
            conversation.save(update_fields=["pending_property", "pending_phone", "pending_at", "updated_at"])
            return [("sendMessage", {"chat_id": chat_id, "text": "Объявление больше не доступно."})]
        conversation.pending_phone = phone
        conversation.save(update_fields=["pending_phone", "updated_at"])
        base = settings.ACCOUNT_PUBLIC_BASE_URL.rstrip("/")
        return [("sendMessage", {
            "chat_id": chat_id,
            "text": f"Телефон: {phone}. Чтобы передать его риелтору и создать заявку, подтвердите согласие на обработку персональных данных.",
            "reply_markup": {"inline_keyboard": [
                [{"text": "Согласен, отправить заявку", "callback_data": f"consent:{property.pk}"}],
                [{"text": "Согласие", "url": base + reverse("personal_data_consent")}, {"text": "Политика", "url": base + reverse("privacy_policy")}],
                [{"text": "Отмена", "callback_data": f"cancel:{property.pk}"}],
            ]},
        })]


def send_actions(actions):
    for method, payload in actions:
        try:
            if method == "listing":
                property = Property.objects.filter(pk=payload["property_id"]).first()
                if property:
                    send_listing(payload["chat_id"], property)
            else:
                _api(method, payload)
        except (requests.RequestException, OSError, ValueError) as error:
            logger.error("Could not deliver public Telegram bot action %s (%s)", method, type(error).__name__)
