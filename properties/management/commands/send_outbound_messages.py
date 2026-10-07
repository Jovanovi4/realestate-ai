"""Retry bot replies and new-lead notifications from the persistent outbox."""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from properties.models import OutboundMessage
from properties.services.outbound_message_service import deliver_messages, due_message_ids


class Command(BaseCommand):
    help = "Отправить ожидающие сообщения и повторить сбои. --loop запускает постоянный worker."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--retry-id", type=int)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 500:
            raise CommandError("--limit должен быть от 1 до 500")
        if options["retry_id"] is not None:
            if options["loop"]:
                raise CommandError("--retry-id нельзя использовать вместе с --loop")
            updated = OutboundMessage.objects.filter(
                pk=options["retry_id"], status=OutboundMessage.Status.FAILED,
            ).update(status=OutboundMessage.Status.PENDING, attempts=0,
                     next_attempt_at=timezone.now(), last_error="", lease_until=None, claim_token=None)
            if not updated:
                raise CommandError("Сообщение с этим ID не найдено или не имеет состояния «Ошибка»")

        while True:
            # A one-shot command uses its existing connection. Closing it here
            # would break callers running the command inside a transaction,
            # including Django's PostgreSQL TestCase.
            if options["loop"]:
                close_old_connections()
            ids = due_message_ids(options["limit"])
            sent = deliver_messages(ids)
            if ids or not options["loop"]:
                self.stdout.write(f"Проверено: {len(ids)}; отправлено: {sent}")
            if not options["loop"]:
                break
            time.sleep(2)
