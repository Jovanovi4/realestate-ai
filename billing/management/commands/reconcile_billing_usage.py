import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from properties.models import Agency, RealtorProfile

from billing.inventory import reconcile_account, unassigned_media_report
from billing.models import BillingAccount
from billing.services import AccountService, SubscriptionService


class Command(BaseCommand):
    help = "Provision shadow accounts and reconcile records/files. Never charge historical AI or delete files."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int)
        parser.add_argument("--counts-only", action="store_true")

    def handle(self, *args, **options):
        try:
            if options["account_id"] is not None:
                accounts = [BillingAccount.objects.get(pk=options["account_id"])]
            else:
                for user in get_user_model().objects.all().iterator():
                    AccountService.ensure_personal(user)
                for agency in Agency.objects.all().iterator():
                    AccountService.ensure_agency(agency)
                for profile in RealtorProfile.objects.filter(email_verified_at__isnull=False).select_related("user").iterator():
                    SubscriptionService.ensure_demo_grant(profile.user)
                accounts = BillingAccount.objects.all().order_by("pk")
            accounts = list(accounts)
            initial = [reconcile_account(account, scan_files=not options["counts_only"]) for account in accounts]
            # A later workspace scan may correctly reattribute a former workspace's
            # retired reference; close those corrections without another storage scan.
            results = []
            for account, first in zip(accounts, initial):
                final = reconcile_account(account, scan_files=False)
                final["corrections"] = {key: first["corrections"].get(key, 0) + final["corrections"].get(key, 0)
                    for key in first["corrections"].keys() | final["corrections"].keys()}
                results.append(final)
        except (BillingAccount.DoesNotExist, ValidationError, DatabaseError) as error:
            raise CommandError(f"Billing reconciliation failed ({type(error).__name__}); no quota blocking or file cleanup is performed.") from error
        self.stdout.write(json.dumps({"mode": "shadow", "accounts": results, "unassigned_media": unassigned_media_report()}, ensure_ascii=False, indent=2))
