import calendar
from datetime import timedelta
import json

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from properties.models import Lead, Property
from properties.services.crm_backup_service import BackupError, backup_root


def add_calendar_months(value, months):
    if months < 1:
        raise ValueError("CRM_ARCHIVE_REVIEW_MONTHS must be positive.")
    index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(index, 12)
    month = zero_based_month + 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def retention_report(*, now=None):
    now = now or timezone.now()
    months = settings.CRM_ARCHIVE_REVIEW_MONTHS
    if months < 1 or settings.CRM_BACKUP_REVIEW_DAYS < 1:
        raise ValueError("CRM retention settings must be positive.")
    result = {"archive_months": months, "automatic_deletion": False, "properties_due": 0, "leads_due": 0, "backups_due": 0, "backup_days": settings.CRM_BACKUP_REVIEW_DAYS, "backup_configured": bool(settings.CRM_BACKUP_ROOT)}
    for model, key in ((Property, "properties_due"), (Lead, "leads_due")):
        result[key] = sum(add_calendar_months(date, months) <= now for date in model.objects.filter(status="archived", archived_at__isnull=False).values_list("archived_at", flat=True).iterator())
    if settings.CRM_BACKUP_ROOT:
        root = backup_root()
        cutoff = now - timedelta(days=settings.CRM_BACKUP_REVIEW_DAYS)
        for path in root.glob("snapshot-*"):
            if path.is_symlink() or not path.is_dir() or (path / "manifest.json").is_symlink():
                continue
            try:
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("Invalid backup manifest.")
                created_at = parse_datetime(manifest["created_at"])
                if created_at is None or timezone.is_naive(created_at):
                    raise ValueError("Invalid backup creation timestamp.")
                result["backups_due"] += created_at <= cutoff
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise BackupError("Cannot read a backup retention date; verify the backup directory.") from error
    return result
