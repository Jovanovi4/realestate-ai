from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from ...services.account_email_service import send_pending_account_emails


class Command(BaseCommand):
    help = "Send queued account verification/recovery emails (run every minute)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be positive")
        try:
            counts = send_pending_account_emails(limit=options["limit"])
        except ImproperlyConfigured as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Отправлено: {counts['sent']}; отменено: {counts['discarded']}; ошибок: {counts['failed']}.")
