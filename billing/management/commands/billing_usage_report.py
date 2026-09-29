import json

from django.core.management.base import BaseCommand, CommandError

from billing.inventory import unassigned_media_report
from billing.models import BillingAccount
from billing.usage import account_report


class Command(BaseCommand):
    help = "Read-only shadow usage report: live records, attributed bytes, AI and prospective denials."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int)

    def handle(self, *args, **options):
        accounts = BillingAccount.objects.all().order_by("pk")
        if options["account_id"] is not None:
            accounts = accounts.filter(pk=options["account_id"])
            if not accounts.exists():
                raise CommandError("Billing account not found.")
        self.stdout.write(json.dumps({"mode": "shadow", "accounts": [account_report(account) for account in accounts], "unassigned_media": unassigned_media_report()}, ensure_ascii=False, indent=2))
