import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from properties.models import Property

from .models import ResourceReservation
from .reservations import finish_reservation, reserve_resources, reserved_resources
from .rules import Operation, collect_usage
from .services import AccountService
from .tests import seed_test_catalog


class ResourceReservationTests(TestCase):
    def setUp(self):
        seed_test_catalog()
        self.user = get_user_model().objects.create_user("reserve-owner")
        self.account = AccountService.ensure_personal(self.user)

    def test_active_claims_are_counted_once_and_consumption_replaces_them_with_live_resource(self):
        first = reserve_resources(account=self.account, actor=self.user, action=Operation.CREATE_PROPERTY,
            amounts={"active_properties": 1, "total_properties": 1})
        self.assertEqual(collect_usage(self.account).active_properties_reserved, 1)
        second = reserve_resources(account=self.account, actor=self.user, action=Operation.CREATE_PROPERTY,
            amounts={"active_properties": 1, "total_properties": 1})
        self.assertEqual(second.decision["code"], "allowed")
        third = reserve_resources(account=self.account, actor=self.user, action=Operation.CREATE_PROPERTY,
            amounts={"active_properties": 1, "total_properties": 1})
        self.assertFalse(third.decision["allowed"])
        with self.assertRaises(ValidationError):
            reserve_resources(account=self.account, actor=self.user, action=Operation.CREATE_PROPERTY,
                amounts={"active_properties": 2}, request_key=first.request_key)
        finish_reservation(first, consumed=False)
        self.assertEqual(collect_usage(self.account).active_properties_reserved, 2)
        with reserved_resources(account=self.account, actor=self.user, action=Operation.CREATE_PROPERTY,
                amounts={"active_properties": 1, "total_properties": 1}):
            Property.objects.create(owner=self.user, title="Созданный объект")
        usage = collect_usage(self.account)
        self.assertEqual((usage.active_properties, usage.active_properties_reserved), (1, 2))
        self.assertEqual(ResourceReservation.objects.filter(status="active").count(), 2)

    def test_failed_write_rolls_back_claim_and_resource(self):
        before = ResourceReservation.objects.count()
        with self.assertRaises(RuntimeError):
            with reserved_resources(account=self.account, actor=self.user, action=Operation.CREATE_PROPERTY,
                    amounts={"active_properties": 1, "total_properties": 1}):
                Property.objects.create(owner=self.user, title="Откат")
                raise RuntimeError("failed")
        self.assertEqual(ResourceReservation.objects.count(), before)
        self.assertFalse(Property.objects.filter(title="Откат").exists())

    def test_same_request_key_does_not_create_second_claim(self):
        key = uuid.uuid4()
        first = reserve_resources(account=self.account, actor=self.user, action=Operation.UPLOAD_FILES,
            amounts={"storage_bytes": 42}, request_key=key)
        second = reserve_resources(account=self.account, actor=self.user, action=Operation.UPLOAD_FILES,
            amounts={"storage_bytes": 42}, request_key=key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(collect_usage(self.account).storage_reserved_bytes, 42)
