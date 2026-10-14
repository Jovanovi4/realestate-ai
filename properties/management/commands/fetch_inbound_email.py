"""Import a dedicated IMAP folder into the CRM intake review queue."""

import os

from django.core.management.base import BaseCommand, CommandError

from properties.models import PublicIntakeLink
from properties.services.inbound_email_service import import_mailbox


class Command(BaseCommand):
    help = "Import new messages from a dedicated IMAP folder; credentials come only from environment."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        host = os.environ.get("INBOUND_IMAP_HOST", "").strip()
        username = os.environ.get("INBOUND_IMAP_USER", "").strip()
        password = os.environ.get("INBOUND_IMAP_PASSWORD", "")
        folder = os.environ.get("INBOUND_IMAP_FOLDER", "REAICRM").strip()
        link_code = os.environ.get("INBOUND_IMAP_LINK_CODE", "").strip()
        if not all((host, username, password, folder, link_code)):
            raise CommandError("Укажите INBOUND_IMAP_HOST, USER, PASSWORD, FOLDER и LINK_CODE в окружении.")
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("--limit должен быть от 1 до 1000.")
        try:
            link = PublicIntakeLink.objects.select_related("agency").get(code=link_code)
        except (PublicIntakeLink.DoesNotExist, ValueError) as error:
            raise CommandError("Ссылка рабочего пространства не найдена.") from error
        try:
            count = import_mailbox(
                link=link, host=host, username=username, password=password,
                folder=folder, limit=options["limit"],
            )
        except Exception as error:
            # IMAP exception text can echo account details or server responses.
            raise CommandError(f"Получение почты не удалось ({type(error).__name__}).") from None
        self.stdout.write(self.style.SUCCESS(f"Новых сообщений для разбора: {count}"))
