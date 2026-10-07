import time

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from ...services.account_email_service import send_pending_account_emails


class Command(BaseCommand):
    help = "Send queued account verification/recovery emails (run every minute)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--loop", action="store_true")

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be positive")
        while True:
            if options["loop"]:
                close_old_connections()
            try:
                counts = send_pending_account_emails(limit=options["limit"])
            except ImproperlyConfigured as error:
                raise CommandError(str(error)) from error
            if any(counts.values()) or not options["loop"]:
                self.stdout.write(f"Отправлено: {counts['sent']}; отменено: {counts['discarded']}; ошибок: {counts['failed']}.")
            if not options["loop"]:
                break
            time.sleep(60)
