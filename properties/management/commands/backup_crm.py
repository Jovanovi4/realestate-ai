from django.core.management.base import BaseCommand, CommandError

from properties.services.crm_backup_service import BackupError, create_backup


class Command(BaseCommand):
    help = "Create a private SQLite + media backup and verify an isolated restore. Never deletes old backups."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", help="Private backup directory outside media/static.")

    def handle(self, *args, **options):
        try:
            path = create_backup(options["output_dir"])
        except BackupError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f"Verified CRM backup: {path}"))
