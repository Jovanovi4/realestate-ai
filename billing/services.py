"""Explicit account provisioning and bounded operator grants; no payment calls."""

import calendar
from datetime import datetime, timedelta, timezone as datetime_timezone
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from properties.models import (
    AIContent, AgencyInvitation, AgencyMembership, Client, ClientReminder,
    Deal, Lead, Property, PropertyImage, RealtorProfile, Showing,
)

from .models import BillingAccount, BillingPeriod, DemoCreditGrant, Plan, PlanVersion, Subscription


def utc_datetime(value):
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError("Billing dates must be timezone-aware.")
    return value.astimezone(datetime_timezone.utc)


def month_boundary(anchor, offset):
    """Always derive from the original day: Jan 31 -> Feb 28 -> Mar 31."""
    anchor = utc_datetime(anchor)
    if type(offset) is not int or offset < 0:
        raise ValueError("Month offset must be a non-negative integer.")
    year, month_index = divmod(anchor.year * 12 + anchor.month - 1 + offset, 12)
    month = month_index + 1
    return anchor.replace(year=year, month=month, day=min(anchor.day, calendar.monthrange(year, month)[1]))


def next_month_boundary(start, anchor):
    start, anchor = utc_datetime(start), utc_datetime(anchor)
    if start < anchor:
        raise ValueError("Period cannot precede its billing anchor.")
    offset = (start.year - anchor.year) * 12 + start.month - anchor.month
    boundary = month_boundary(anchor, offset)
    return boundary if boundary > start else month_boundary(anchor, offset + 1)


def resource_scope(resource):
    """Resolve the resource itself; never the requesting user's merged workspace."""
    if not getattr(resource, "pk", None):
        raise ValueError("A saved workspace resource is required.")
    if isinstance(resource, (Lead, PropertyImage, AIContent)):
        resource = resource.property
    if isinstance(resource, (AgencyInvitation, AgencyMembership)):
        return {"agency_id": resource.agency_id, "personal_user_id": None}
    if not isinstance(resource, (Property, Client, Deal, ClientReminder, Showing)):
        raise ValueError("Unsupported billing resource.")
    return {"agency_id": resource.agency_id, "personal_user_id": None if resource.agency_id else resource.owner_id}


class AccountService:
    @staticmethod
    def ensure_personal(user):
        if not user.pk:
            raise ValueError("A saved user is required.")
        return BillingAccount.objects.get_or_create(personal_user=user, defaults={"payer": user})[0]

    @staticmethod
    def ensure_agency(agency):
        if not agency.pk:
            raise ValueError("A saved agency is required.")
        return BillingAccount.objects.get_or_create(agency=agency, defaults={"payer_id": agency.owner_id})[0]

    @staticmethod
    def for_resource(resource):
        # Read-only lookup: evaluating policy does not create billing data.
        return BillingAccount.objects.get(**resource_scope(resource))

    @staticmethod
    def ensure_for_resource(resource):
        scope = resource_scope(resource)
        if scope["agency_id"]:
            from properties.models import Agency
            return AccountService.ensure_agency(Agency.objects.get(pk=scope["agency_id"]))
        from django.contrib.auth import get_user_model
        return AccountService.ensure_personal(get_user_model().objects.get(pk=scope["personal_user_id"]))

    @staticmethod
    def matches(account, resource):
        scope = resource_scope(resource)
        return account.agency_id == scope["agency_id"] and account.personal_user_id == scope["personal_user_id"]

    @staticmethod
    @transaction.atomic
    def convert_founder_account(agency, actor):
        """Explicit agency-creation workflow; membership alone never moves a budget."""
        from .models import AccountInventory
        from .usage import begin_operation, finish_operation

        if actor.pk != agency.owner_id:
            raise PermissionDenied("Перевести личный счёт может только владелец агентства.")
        existing_team = BillingAccount.objects.select_for_update().filter(agency=agency).first()
        if existing_team:
            return existing_team
        account = AccountService.ensure_personal(actor)
        account = BillingAccount.objects.select_for_update().get(pk=account.pk)
        key = uuid.uuid5(uuid.NAMESPACE_URL, f"realtorai:account-conversion:agency:{agency.pk}")
        operation = begin_operation(account=account, actor=actor, action="account.convert_to_agency", request_key=key,
            decision={"mode": "shadow", "allowed": True, "code": "explicit_scope_conversion", "agency_id": agency.pk})
        account.personal_user = None
        account.agency = agency
        account.save(update_fields=["personal_user", "agency"])
        # The paid subscription/period/ledger keeps its original account and IDs.
        personal = AccountService.ensure_personal(actor)
        grant = DemoCreditGrant.objects.filter(user=actor).first()
        if grant:
            grant.account = personal
            grant.save(update_fields=["account"])
        AccountInventory.objects.filter(account=account).update(files_complete=False)
        finish_operation(operation)
        return account


def current_plan_version(code):
    return PlanVersion.objects.select_related("plan").filter(plan__code=code, available=True).order_by("-version").first()


class SubscriptionService:
    @staticmethod
    @transaction.atomic
    def grant_period(*, account, plan_version, actor, reason, request_key, starts_at, ends_at=None, source=BillingPeriod.Source.PILOT):
        """Internal operator provisioning, deliberately not a successful-payment API."""
        if not actor or not actor.is_active or not actor.has_perm("billing.add_billingperiod"):
            raise PermissionDenied("Только уполномоченный оператор может выдавать доступ.")
        if not reason or not reason.strip():
            raise ValidationError("Укажите основание временного доступа.")
        if source not in BillingPeriod.Source.values:
            raise ValidationError("Платёжное предоставление доступа пока не подключено.")
        try:
            request_key = uuid.UUID(str(request_key))
            starts_at = utc_datetime(starts_at)
            ends_at = utc_datetime(ends_at) if ends_at is not None else None
        except (ValueError, TypeError, AttributeError) as error:
            raise ValidationError("Нужны UUID операции и даты с часовым поясом.") from error
        account = BillingAccount.objects.select_for_update().get(pk=account.pk)
        previous = BillingPeriod.objects.select_related("subscription").filter(request_key=request_key).first()
        if previous:
            if (previous.subscription.account_id != account.pk or previous.plan_version_id != plan_version.pk
                    or previous.starts_at != starts_at or (ends_at is not None and previous.ends_at != ends_at)
                    or previous.reason != reason.strip() or previous.source != source):
                raise ValidationError("Ключ уже использован для другой операции.")
            return previous
        plan_version = PlanVersion.objects.select_related("plan").get(pk=plan_version.pk)
        if not plan_version.available or plan_version.plan.code == Plan.Code.DEMO:
            raise ValidationError("Выберите доступную платную версию; Демо не требует периода.")
        subscription, _ = Subscription.objects.get_or_create(account=account, defaults={"anchor_at": starts_at})
        if subscription.is_suspended:
            raise ValidationError("Сначала разберите причину приостановки подписки.")
        latest = subscription.periods.filter(voided_at__isnull=True).order_by("-ends_at").first()
        if latest and starts_at >= latest.ends_at + timedelta(days=latest.terms_snapshot["grace_days"]):
            # A new run after the grace interval has a new anchor, not retroactive debt.
            subscription.anchor_at = starts_at
            subscription.save(update_fields=["anchor_at"])
        try:
            ends_at = ends_at or next_month_boundary(starts_at, subscription.anchor_at)
        except ValueError as error:
            raise ValidationError("Начало периода не может предшествовать привязке подписки.") from error
        return BillingPeriod.objects.create(
            subscription=subscription, plan_version=plan_version, starts_at=starts_at, ends_at=ends_at,
            source=source, reason=reason.strip(), granted_by=actor, request_key=request_key,
        )

    @staticmethod
    @transaction.atomic
    def ensure_demo_grant(user):
        # Caller must opt in; registration/email handlers are not connected yet.
        account = AccountService.ensure_personal(user)
        account = BillingAccount.objects.select_for_update().get(pk=account.pk)
        existing = DemoCreditGrant.objects.filter(user=user).first()
        if existing:
            return existing
        if not RealtorProfile.objects.filter(user=user, email_verified_at__isnull=False).exists():
            raise ValidationError("Подтвердите email перед выдачей разового гранта.")
        version = current_plan_version(Plan.Code.DEMO)
        if version is None:
            raise ValidationError("Каталог тарифов не настроен.")
        snapshot = version.snapshot()
        return DemoCreditGrant.objects.create(
            user=user, account=account, terms_snapshot=snapshot,
            credits_allocated=snapshot["limits"]["demo_ai_credits"],
        )
