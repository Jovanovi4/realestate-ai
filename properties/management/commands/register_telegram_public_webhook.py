"""Register the separate buyer bot's HTTPS webhook after deployment."""

import json

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse


class Command(BaseCommand):
    help = "Зарегистрировать HTTPS-webhook публичного Telegram-бота. Требует настроенных переменных окружения."

    def handle(self, *args, **options):
        base = settings.ACCOUNT_PUBLIC_BASE_URL.rstrip("/")
        if not (settings.TELEGRAM_PUBLIC_ENABLED and settings.TELEGRAM_PUBLIC_BOT_TOKEN and settings.TELEGRAM_PUBLIC_WEBHOOK_SECRET and base.startswith("https://")):
            raise CommandError("Включите публичного бота и задайте HTTPS ACCOUNT_PUBLIC_BASE_URL, TELEGRAM_PUBLIC_BOT_TOKEN и TELEGRAM_PUBLIC_WEBHOOK_SECRET")
        url = base + reverse("telegram_public_webhook")
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_PUBLIC_BOT_TOKEN}/setWebhook",
                data={
                    "url": url,
                    "secret_token": settings.TELEGRAM_PUBLIC_WEBHOOK_SECRET,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                },
                timeout=15,
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                raise CommandError("Telegram отклонил регистрацию webhook")
        except (requests.RequestException, ValueError) as error:
            raise CommandError(f"Не удалось зарегистрировать webhook ({type(error).__name__})") from None
        self.stdout.write(self.style.SUCCESS(f"Webhook зарегистрирован: {url}"))
