import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from properties.agency_access import membership_for
from properties.services.crm_analytics_audit import audit_report
from properties.services.crm_analytics_service import resolve_scope


class Command(BaseCommand):
    help = "Read-only comparison of analytics with CRM lists and query timing; prints counts only."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, required=True)
        parser.add_argument("--scope", choices=("personal", "team"), default="personal")
        parser.add_argument("--member-id", type=int)
        parser.add_argument("--days", type=int, choices=(7, 30, 90), default=30)
        parser.add_argument("--allow-sqlite", action="store_true", help="Local smoke check only; not PostgreSQL validation.")

    def handle(self, *args, **options):
        if connection.vendor != "postgresql" and not options["allow_sqlite"]:
            raise CommandError("Для проверки пилота нужна PostgreSQL; --allow-sqlite только для локальной проверки команды.")
        user = get_user_model().objects.filter(pk=options["user_id"]).first()
        if not user:
            raise CommandError("Пользователь не найден.")
        membership = membership_for(user)
        if options["scope"] == "team" and membership is None:
            raise CommandError("У пользователя нет командного пространства.")
        if options["member_id"] and options["scope"] != "team":
            raise CommandError("Выбор сотрудника доступен только для команды.")
        scope = resolve_scope(
            user, requested_scope=options["scope"], requested_member=options["member_id"],
            membership=membership,
        )
        result = audit_report(user, scope, days=options["days"])
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        if result["mismatches"]:
            raise CommandError("Агрегаты и списки CRM расходятся; проверьте mismatches выше.")
