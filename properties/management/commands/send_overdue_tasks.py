from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from properties.models import ClientReminder
from properties.services.overdue_task_notification_service import OverdueTaskNotificationService


class Command(BaseCommand):
    help = "Отправить владельцам кабинетов уведомления о просроченных задачах. Запускайте по расписанию."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Показать число задач без отправки сообщений")

    def handle(self, *args, **options):
        overdue = ClientReminder.objects.filter(
            is_done=False,
            due_at__lt=timezone.now(),
            overdue_notified_at__isnull=True,
        )
        if options["dry_run"]:
            self.stdout.write(f"Ожидают уведомления: {overdue.count()} задач")
            return

        owner_ids = overdue.order_by("owner_id").values_list("owner_id", flat=True).distinct()
        sent = 0
        for owner_id in owner_ids:
            user = get_user_model().objects.get(pk=owner_id)
            tasks = list(overdue.filter(owner_id=owner_id).select_related("client").order_by("due_at")[:20])
            if OverdueTaskNotificationService.send_for_user(user, tasks):
                sent += 1
        self.stdout.write(f"Отправлено уведомлений: {sent}")
