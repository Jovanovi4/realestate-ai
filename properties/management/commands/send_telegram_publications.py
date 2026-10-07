"""Deliver queued buyer announcements in bounded batches."""

import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, transaction
from django.utils import timezone

from properties.models import TelegramConversation, TelegramDelivery, TelegramInboundUpdate, TelegramPublication
from properties.services.telegram_public_service import feed_matches_property, is_visible, send_listing


class Command(BaseCommand):
    help = "Отправить объявления подписчикам публичного бота. --loop проверяет очередь каждую минуту."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--loop", action="store_true")

    def handle(self, *args, **options):
        if options["limit"] < 1 or options["limit"] > 100:
            raise CommandError("--limit должен быть от 1 до 100")
        if options["loop"] and options["dry_run"]:
            raise CommandError("--dry-run нельзя использовать вместе с --loop")
        while True:
            if options["loop"]:
                close_old_connections()
            sent = self.send_pending(options["limit"], dry_run=options["dry_run"], allow_disabled=options["loop"])
            if sent or (not options["loop"] and not options["dry_run"]):
                self.stdout.write(f"Отправлено: {sent}")
            if not options["loop"]:
                break
            time.sleep(60)

    def send_pending(self, limit, *, dry_run, allow_disabled):
        pending = TelegramDelivery.objects.filter(sent_at__isnull=True, attempts__lt=5).order_by("id")
        if dry_run:
            self.stdout.write(f"Ожидают отправки: {pending.count()}")
            return 0
        if not (settings.TELEGRAM_PUBLIC_ENABLED and settings.TELEGRAM_PUBLIC_BOT_TOKEN):
            if allow_disabled:
                return 0
            raise CommandError("Публичный Telegram-бот не настроен")
        TelegramConversation.objects.filter(pending_at__lt=timezone.now() - timedelta(days=1)).update(
            pending_property=None, pending_phone="", pending_at=None,
        )
        TelegramInboundUpdate.objects.filter(created_at__lt=timezone.now() - timedelta(days=30)).delete()
        sent = 0
        last_sent_by_chat = {}
        for delivery_id in list(pending.values_list("id", flat=True)[:limit]):
            with transaction.atomic():
                delivery = TelegramDelivery.objects.select_for_update().select_related(
                    "subscriber", "publication__property", "publication__feed",
                ).get(pk=delivery_id)
                if delivery.sent_at or delivery.attempts >= 5:
                    continue
                property = delivery.publication.property
                newest = TelegramPublication.objects.filter(property=property).order_by("-pk").values_list("pk", flat=True).first()
                if (not delivery.subscriber.active or not is_visible(property)
                        or not feed_matches_property(delivery.publication.feed, property)
                        or newest != delivery.publication_id):
                    delivery.attempts = 5
                    delivery.last_error = "Публикация больше не актуальна"
                    delivery.save(update_fields=["attempts", "last_error"])
                    continue
                chat_id = delivery.subscriber.chat_id
                elapsed = time.monotonic() - last_sent_by_chat.get(chat_id, -100)
                if elapsed < 1:
                    time.sleep(1 - elapsed)
                try:
                    send_listing(delivery.subscriber.chat_id, property)
                except Exception as error:
                    delivery.attempts += 1
                    delivery.last_error = type(error).__name__[:255]
                    delivery.save(update_fields=["attempts", "last_error"])
                    continue
                delivery.sent_at = timezone.now()
                delivery.attempts += 1
                delivery.last_error = ""
                delivery.save(update_fields=["sent_at", "attempts", "last_error"])
                sent += 1
                last_sent_by_chat[chat_id] = time.monotonic()
            time.sleep(0.04)
        return sent
