from django.core.management.base import BaseCommand, CommandError

from billing.models import UsageOperation
from billing.usage import mark_stale_operations, recover_saved_operation


class Command(BaseCommand):
    help = "Mark stale running observations unknown. Never retry an API call or silently release its reserve."

    def add_arguments(self, parser):
        parser.add_argument("--age-minutes", type=int, default=15)
        parser.add_argument("--recover-saved", action="store_true", help="Explicitly settle complete, atomically saved results without API retries.")

    def handle(self, *args, **options):
        try:
            count = mark_stale_operations(age_minutes=options["age_minutes"])
        except ValueError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Marked unknown: {count}. Operator review required; no automatic retries or refunds.")
        if options["recover_saved"]:
            recovered = sum(recover_saved_operation(operation) for operation in UsageOperation.objects.filter(
                status__in=["running", "unknown"], saved_results__isnull=False).distinct().iterator())
            self.stdout.write(f"Recovered saved operations: {recovered}. No provider requests were sent.")
