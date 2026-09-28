from django.core.management.base import BaseCommand, CommandError

from properties.services.crm_backup_service import BackupError
from properties.services.crm_retention_service import retention_report


class Command(BaseCommand):
    help = "Report archives due for retention review. Read-only: never deletes or anonymizes records/backups."

    def handle(self, *args, **options):
        try:
            result = retention_report()
        except (ValueError, BackupError) as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Review after {result['archive_months']} calendar months: {result['properties_due']} properties, {result['leads_due']} leads.")
        if result["backup_configured"]:
            self.stdout.write(f"Backups older than {result['backup_days']} days: {result['backups_due']}.")
        else:
            self.stdout.write("Backup retention not inspected: set CRM_BACKUP_ROOT.")
        self.stdout.write("Read-only report. No CRM records or backups were deleted.")
