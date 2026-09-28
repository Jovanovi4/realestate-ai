from django.core.management.base import BaseCommand, CommandError

from properties.services.crm_backup_service import BackupError, latest_backup, verify_backup


class Command(BaseCommand):
    help = "Restore a CRM backup into a temporary directory and verify DB, relations and media. Never changes the live site."

    def add_arguments(self, parser):
        parser.add_argument("snapshot", nargs="?")
        parser.add_argument("--latest", action="store_true")
        parser.add_argument("--output-dir", help="Directory for --latest, or CRM_BACKUP_ROOT.")

    def handle(self, *args, **options):
        if bool(options["snapshot"]) == bool(options["latest"]):
            raise CommandError("Provide exactly one snapshot path or --latest.")
        try:
            path = latest_backup(options["output_dir"]) if options["latest"] else options["snapshot"]
            result = verify_backup(path)
        except BackupError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f"Restore verified: {result['tables']} tables, {result['media_files']} media files, {result['media_references']} referenced files."))
