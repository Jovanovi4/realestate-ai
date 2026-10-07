"""CRM publishing control and authenticated Telegram buyer webhook."""

import json
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from billing.enforcement import TariffDenied, require_resource_action, require_telegram_publication
from billing.rules import Operation
from .agency_access import workspace_queryset
from .models import Property
from .services.outbound_message_service import deliver_messages_safely, enqueue_public_actions
from .services.telegram_public_service import VISIBLE_STATUSES, process_update, publish


class TelegramPublishView(LoginRequiredMixin, View):
    @transaction.atomic
    def post(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects.select_for_update(), request.user), pk=pk)
        destination = f"{reverse('property_detail', kwargs={'pk': pk})}?tab=publication"
        if request.POST.get("action") == "publish":
            try:
                require_resource_action(request.user, property, Operation.EDIT_CRM)
                require_telegram_publication(request.user, property)
            except TariffDenied as error:
                messages.error(request, error.message)
                return redirect(destination)
            if property.status not in VISIBLE_STATUSES or property.is_demo or not property.owner_id:
                messages.error(request, "Сначала переведите реальный объект в статус «Опубликован», «Свободен» или «На показах».")
            elif not (settings.TELEGRAM_PUBLIC_ENABLED and settings.TELEGRAM_PUBLIC_BOT_TOKEN and settings.TELEGRAM_PUBLIC_BOT_USERNAME and settings.TELEGRAM_PUBLIC_WEBHOOK_SECRET and settings.ACCOUNT_PUBLIC_BASE_URL.startswith("https://")):
                messages.error(request, "Публичный Telegram-бот ещё не настроен.")
            else:
                publication = publish(property)
                if publication:
                    messages.success(request, f"Объект опубликован в боте. В очередь отправки добавлено {publication.deliveries.count()} сообщений.")
                else:
                    messages.info(request, "Объект уже опубликован в боте.")
        elif request.POST.get("action") == "unpublish":
            if property.telegram_published:
                property.telegram_published = False
                property.save(update_fields=["telegram_published", "updated_at"])
                messages.success(request, "Объявление снято с публикации в боте.")
        return redirect(destination)


@csrf_exempt
@require_POST
def telegram_public_webhook(request):
    if not (settings.TELEGRAM_PUBLIC_ENABLED and settings.TELEGRAM_PUBLIC_BOT_TOKEN and settings.TELEGRAM_PUBLIC_WEBHOOK_SECRET):
        return HttpResponse(status=503)
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secrets.compare_digest(supplied, settings.TELEGRAM_PUBLIC_WEBHOOK_SECRET):
        return HttpResponse(status=403)
    if len(request.body) > 65536:
        return HttpResponse(status=413)
    try:
        update = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)
    if not isinstance(update, dict):
        return HttpResponse(status=400)
    with transaction.atomic():
        actions = process_update(update)
        queued_ids = enqueue_public_actions(update.get("update_id"), actions)
    # Immediate best effort keeps the bot responsive. A failed attempt remains
    # in the outbox for the worker; the incoming update is never replayed.
    deliver_messages_safely(queued_ids)
    return JsonResponse({"ok": True})
