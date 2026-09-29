"""Run these TransactionTestCase checks with a disposable PostgreSQL DATABASE_URL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
import uuid

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from properties.models import Property, RealtorProfile

from .enforcement import TariffDenied
from .models import BillingAccount, DemoCreditGrant
from .reservations import reserved_resources
from .rules import Operation
from .services import AccountService, SubscriptionService
from .tests import seed_test_catalog
from .usage import begin_operation


@skipUnless(connection.vendor == "postgresql", "Requires a disposable PostgreSQL test database")
@override_settings(BILLING_ENFORCEMENT_ENABLED=True)
class PostgreSQLContentionTests(TransactionTestCase):
    def setUp(self):
        seed_test_catalog()
        self.user = get_user_model().objects.create_user("postgres-race-owner")
        self.account = AccountService.ensure_personal(self.user)
        self.property = Property.objects.create(owner=self.user, title="Первый объект")

    def race(self, work):
        barrier = Barrier(2)

        def run(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return work(index)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, index) for index in range(2)]
            return [future.result(timeout=20) for future in futures]

    def test_only_one_creator_takes_last_demo_property_slot(self):
        def work(index):
            account = BillingAccount.objects.get(pk=self.account.pk)
            actor = get_user_model().objects.get(pk=self.user.pk)
            try:
                with reserved_resources(account=account, actor=actor, action=Operation.CREATE_PROPERTY,
                        amounts={"active_properties": 1, "total_properties": 1}, request_key=uuid.uuid4()):
                    Property.objects.create(owner=actor, title=f"Параллельный {index}")
                return "created"
            except TariffDenied:
                return "denied"

        self.assertCountEqual(self.race(work), ["created", "denied"])
        self.assertEqual(Property.objects.filter(owner=self.user).count(), 2)

    def test_only_one_ai_package_reserves_last_five_credits(self):
        RealtorProfile.objects.create(user=self.user, email="owner@example.test", email_verified_at=timezone.now())
        grant = SubscriptionService.ensure_demo_grant(self.user)

        def work(index):
            account = BillingAccount.objects.get(pk=self.account.pk)
            actor = get_user_model().objects.get(pk=self.user.pk)
            resource = Property.objects.get(pk=self.property.pk)
            try:
                begin_operation(account=account, actor=actor, action=Operation.AI_GENERATE,
                    resource=resource, ai_action="package", request_key=uuid.uuid4())
                return "reserved"
            except TariffDenied:
                return "denied"

        self.assertCountEqual(self.race(work), ["reserved", "denied"])
        grant.refresh_from_db()
        self.assertEqual(grant.credits_reserved, 3)
