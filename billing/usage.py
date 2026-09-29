"""Usage ledger and AI reservations; optional enforcement stays off by default."""

from contextvars import ContextVar
from copy import deepcopy
from datetime import timedelta
import logging
import time
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from .models import (
    AccountInventory, BillingAccount, BillingPeriod, DemoCreditGrant, UsageEntry, UsageOperation, UsageResult,
)
from .enforcement import enabled as enforcement_enabled, require_allowed, TariffDenied
from .rules import Operation, PolicyDecision, PolicyService, collect_usage
from .services import AccountService, utc_datetime


logger = logging.getLogger("billing.observation")
_provider_trace = ContextVar("billing_provider_trace", default=None)


class DuplicateAIRequest(Exception):
    """A repeated key cannot start another provider call."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def enabled():
    return getattr(settings, "BILLING_SHADOW_ENABLED", False)


def attempt(callback):
    """An isolated savepoint prevents a metering error from poisoning CRM writes."""
    if not enabled():
        return None
    try:
        with transaction.atomic():
            return callback()
    except Exception as error:
        # Do not log exception text, request data, prompts, credentials or SQL.
        logger.error("Shadow accounting gap (%s); CRM operation is not blocked.", type(error).__name__)
        return None


def scope_snapshot(account):
    return {"personal_user_id": account.personal_user_id, "agency_id": account.agency_id}


def ensure_scope(scope):
    if scope.get("agency_id"):
        from properties.models import Agency

        return AccountService.ensure_agency(Agency.objects.get(pk=scope["agency_id"]))
    return AccountService.ensure_personal(get_user_model().objects.get(pk=scope["personal_user_id"]))


def _decision(account, actor, operation, resource, now, **kwargs):
    result = PolicyService.evaluate(account, actor, operation, resource=resource, now=now, **kwargs)
    return {"mode": "shadow", "allowed": result.allowed, "code": result.code,
            "plan": result.plan_code, "state": str(result.state),
            "limit": result.limit, "used": result.used, "required": result.required}


@transaction.atomic
def begin_operation(*, account, actor, action, resource=None, resource_ref=None, ai_action="", request_key=None,
                    request_fingerprint="", now=None, decision=None, meter=True):
    now = utc_datetime(now or timezone.now())
    request_key = uuid.UUID(str(request_key)) if request_key is not None else uuid.uuid4()
    account = BillingAccount.objects.select_for_update().get(pk=account.pk)
    resource_type = resource._meta.label_lower if resource is not None else resource_ref[0] if resource_ref else ""
    resource_id = str(resource.pk) if resource is not None else str(resource_ref[1]) if resource_ref else ""
    existing = UsageOperation.objects.filter(request_key=request_key).first()
    actor_id = getattr(actor, "pk", None)
    if existing:
        if (existing.account_id, existing.actor_id, existing.action, existing.resource_type, existing.resource_id, existing.ai_action, existing.request_fingerprint) != (account.pk, actor_id, str(action), resource_type, resource_id, ai_action, request_fingerprint):
            raise ValidationError("Ключ учёта уже относится к другой операции.")
        existing._billing_reused = True
        return existing
    if resource is not None and not AccountService.matches(account, resource):
        raise ValidationError("Нельзя учитывать чужой ресурс на этом счёте.")
    if enforcement_enabled() and str(action) == Operation.AI_GENERATE:
        require_allowed(PolicyService.evaluate(account, actor, action, resource=resource, ai_action=ai_action, now=now))
    if meter:
        try:
            context = PolicyService.context(account, now=now)
            terms = context.terms
            period_id = context.period_id
            planned = terms["ai_costs"].get(ai_action, 0) if ai_action else 0
        except ValidationError:
            terms, period_id, planned = {}, None, 0
        grant = DemoCreditGrant.objects.filter(account=account, user_id=account.personal_user_id).first() if account.personal_user_id and not period_id else None
    else:
        terms, period_id, planned, grant = {}, None, 0, None
    if decision is None:
        decision = _decision(account, actor, action, resource, now, ai_action=ai_action) if meter else {"mode": "idempotency", "allowed": True, "code": "recording_disabled"}
    budget_reserved = False
    if meter and str(action) == Operation.AI_GENERATE and planned and decision.get("allowed") and (period_id or grant):
        if period_id:
            budget_reserved = bool(BillingPeriod.objects.filter(pk=period_id,
                ai_credits_allocated__gte=F("ai_credits_used") + F("ai_credits_reserved") + planned,
            ).update(ai_credits_reserved=F("ai_credits_reserved") + planned))
        else:
            budget_reserved = bool(DemoCreditGrant.objects.filter(pk=grant.pk,
                credits_allocated__gte=F("credits_used") + F("credits_reserved") + planned,
            ).update(credits_reserved=F("credits_reserved") + planned))
        if not budget_reserved:
            decision = {**decision, "allowed": False, "code": "reservation_unavailable"}
            if enforcement_enabled():
                raise TariffDenied(PolicyDecision(False, "reservation_unavailable", "ИИ-кредитов для этого действия недостаточно."))
    operation = UsageOperation.objects.create(
        account=account, actor_id=actor_id, request_key=request_key, request_fingerprint=request_fingerprint, action=str(action),
        resource_type=resource_type, resource_id=resource_id, scope_snapshot=scope_snapshot(account),
        terms_snapshot=deepcopy(terms), decision=decision, period_id=period_id,
        demo_grant=grant, ai_action=ai_action, planned_credits=planned, budget_reserved=budget_reserved, started_at=now,
    )
    operation._billing_reused = False
    return operation


@transaction.atomic
def finish_operation(operation, *, status=UsageOperation.Status.SUCCEEDED, deltas=None, result_ids=None, error_code="", provider=None, now=None):
    BillingAccount.objects.select_for_update().get(pk=operation.account_id)
    operation = UsageOperation.objects.select_for_update().get(pk=operation.pk)
    if operation.status in {UsageOperation.Status.SUCCEEDED, UsageOperation.Status.FAILED}:
        if operation.status != status:
            raise ValidationError("Нельзя повторно завершить операцию с другим исходом.")
        previous_deltas = {entry.metric: entry.delta for entry in operation.entries.all()}
        if (result_ids is not None and operation.result_ids != result_ids) or (deltas is not None and previous_deltas != {key: value for key, value in deltas.items() if value}):
            raise ValidationError("Повтор завершения должен содержать исходные результаты и расход.")
        return operation
    if status not in {UsageOperation.Status.SUCCEEDED, UsageOperation.Status.FAILED, UsageOperation.Status.UNKNOWN}:
        raise ValidationError("Нужно конечное состояние операции.")
    if operation.budget_reserved and status != UsageOperation.Status.UNKNOWN:
        amount = operation.planned_credits
        if operation.period_id:
            updates = {"ai_credits_reserved": F("ai_credits_reserved") - amount}
            if status == UsageOperation.Status.SUCCEEDED:
                updates["ai_credits_used"] = F("ai_credits_used") + amount
            released = BillingPeriod.objects.filter(pk=operation.period_id, ai_credits_reserved__gte=amount).update(**updates)
        else:
            updates = {"credits_reserved": F("credits_reserved") - amount}
            if status == UsageOperation.Status.SUCCEEDED:
                updates["credits_used"] = F("credits_used") + amount
            released = DemoCreditGrant.objects.filter(pk=operation.demo_grant_id, credits_reserved__gte=amount).update(**updates)
        if not released:
            raise ValidationError("Резерв ИИ-кредитов требует сверки.")
    operation.status, operation.finished_at = status, utc_datetime(now or timezone.now())
    operation.error_code = error_code
    operation.result_ids = result_ids or []
    for name, value in (provider or {}).items():
        if name in {"provider", "provider_model", "provider_response_id", "provider_status", "input_tokens", "output_tokens", "cached_input_tokens", "total_tokens", "duration_ms"}:
            setattr(operation, name, value)
    operation.save()
    if status == UsageOperation.Status.SUCCEEDED:
        for metric, delta in (deltas or {}).items():
            if delta:
                UsageEntry.objects.create(operation=operation, account_id=operation.account_id, metric=metric, delta=delta)
    return operation


def record_provider(**values):
    """Only whitelisted numeric/technical fields; no provider response body."""
    trace = _provider_trace.get()
    if trace is None:
        return
    for name in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens"):
        if name in values:
            value = values[name]
            trace[name] = value if type(value) is int and value >= 0 else None
    for name, maximum in (("provider", 30), ("provider_model", 100), ("provider_response_id", 128), ("provider_status", 30)):
        if name in values:
            trace[name] = str(values[name] or "")[:maximum]


def _replay_ai_results(operation):
    """Only durable results linked to this operation may be replayed."""
    from properties.models import AIContent

    if operation.status not in {UsageOperation.Status.RUNNING, UsageOperation.Status.UNKNOWN, UsageOperation.Status.SUCCEEDED}:
        raise DuplicateAIRequest("already_failed")
    result_ids = operation.result_ids or list(operation.saved_results.order_by("pk").values_list("content_id", flat=True))
    linked_ids = set(operation.saved_results.values_list("content_id", flat=True))
    if not result_ids or linked_ids != set(result_ids):
        raise DuplicateAIRequest("outcome_pending" if operation.status == UsageOperation.Status.UNKNOWN else "already_running")
    contents = {content.pk: content for content in AIContent.objects.filter(pk__in=result_ids)}
    if len(contents) != len(result_ids):
        raise DuplicateAIRequest("results_unavailable")
    if operation.status != UsageOperation.Status.SUCCEEDED:
        recover_saved_operation(operation)
    return [contents[pk] for pk in result_ids]


def observe_ai_generation(*, resource, actor, ai_action, generate, save, request_key=None, request_fingerprint=""):
    """Meter one provider operation, then persist the complete usable result atomically."""
    def start():
        scope = {"agency_id": resource.agency_id, "personal_user_id": None if resource.agency_id else resource.owner_id}
        account = ensure_scope(scope)
        if (enabled() or enforcement_enabled()) and account.personal_user_id:
            from properties.models import RealtorProfile
            from .services import SubscriptionService

            if RealtorProfile.objects.filter(user_id=account.personal_user_id, email_verified_at__isnull=False).exists():
                SubscriptionService.ensure_demo_grant(account.personal_user)
        return begin_operation(account=account, actor=actor, action=Operation.AI_GENERATE, resource=resource,
            ai_action=ai_action, request_key=request_key, request_fingerprint=request_fingerprint,
            meter=enabled() or enforcement_enabled())

    # A caller-supplied key is a safety boundary: fail before contacting the
    # provider if its durable record cannot be created or read.
    operation = start() if request_key is not None or enforcement_enabled() else attempt(start)
    if operation and getattr(operation, "_billing_reused", False):
        return _replay_ai_results(operation)
    record = (lambda callback: callback()) if request_key is not None or enforcement_enabled() else attempt
    trace = {}
    token = _provider_trace.set(trace)
    started = time.monotonic()
    try:
        generated = generate()
        with transaction.atomic():
            contents = save(generated)
            if not contents:
                raise ValidationError("Готовые результаты ИИ не сохранены.")
            if operation:
                def link_results():
                    for content in contents:
                        UsageResult.objects.get_or_create(operation=operation, content=content)

                record(link_results)
                if contents and not trace.get("provider"):
                    trace.update(provider=contents[0].provider, provider_model=contents[0].model)
                trace["duration_ms"] = int((time.monotonic() - started) * 1000)
                record(lambda: finish_operation(operation, result_ids=[content.pk for content in contents],
                    deltas={UsageEntry.Metric.AI_CREDITS: operation.planned_credits}, provider=trace))
        return contents
    except Exception as error:
        if operation:
            trace["duration_ms"] = int((time.monotonic() - started) * 1000)
            unknown = getattr(error, "outcome_unknown", False)
            status = UsageOperation.Status.UNKNOWN if unknown else UsageOperation.Status.FAILED
            code = "provider_outcome_unknown" if unknown else "generation_failed"
            try:
                record(lambda: finish_operation(operation, status=status, error_code=code, provider=trace))
            except Exception as accounting_error:
                logger.error("AI completion requires review (%s).", type(accounting_error).__name__)
        raise
    finally:
        _provider_trace.reset(token)


def observed_credits(*, account, period_id=None, demo_grant_id=None):
    """Demo lifetime bucket survives reassignment of its account during agency conversion."""
    if period_id:
        operations = UsageOperation.objects.filter(account=account, period_id=period_id)
    elif demo_grant_id:
        operations = UsageOperation.objects.filter(demo_grant_id=demo_grant_id)
    else:
        return 0, 0
    used = UsageEntry.objects.filter(operation__in=operations.filter(budget_reserved=False), metric=UsageEntry.Metric.AI_CREDITS).aggregate(value=Sum("delta"))["value"] or 0
    pending = operations.filter(status__in=[UsageOperation.Status.RUNNING, UsageOperation.Status.UNKNOWN], budget_reserved=False).aggregate(value=Sum("planned_credits"))["value"] or 0
    return used, pending


def account_report(account, *, now=None):
    now = utc_datetime(now or timezone.now())
    usage = collect_usage(account, now=now)
    try:
        context = PolicyService.context(account, now=now)
        plan, state, limits = context.terms["plan_code"], str(context.state), context.terms["limits"]
        ai = {"allocated": context.ai_allocated, "used": context.ai_used, "pending": context.ai_reserved}
    except ValidationError:
        plan, state, limits, ai = None, "unknown", {}, None
    totals = account.usage_entries.values("metric").annotate(value=Sum("delta"))
    operations = account.usage_operations
    inventory = AccountInventory.objects.filter(account=account).first()
    inventory_report = None if inventory is None else {
        "references_complete": inventory.references_complete, "file_sizes_known": inventory.files_complete,
        "last_updated_at": inventory.checked_at.isoformat() if inventory.checked_at else None,
        "issue_count": inventory.last_issue_count,
    }
    return {"account_id": account.pk, "scope": scope_snapshot(account), "mode": "shadow", "recording_enabled": enabled(), "plan": plan, "state": state,
        "usage": vars(usage), "limits": limits, "ai": ai, "ledger": {row["metric"]: row["value"] for row in totals},
        "inventory": inventory_report,
        "unknown_operations": operations.filter(status=UsageOperation.Status.UNKNOWN).count(),
        "running_operations": operations.filter(status=UsageOperation.Status.RUNNING).count(),
        "prospective_denials": operations.filter(decision__allowed=False).count(),
        "credit_cost_unknown": operations.exclude(ai_action="").filter(terms_snapshot={}).count(),
        "provider_usage_unknown": operations.exclude(ai_action="").exclude(ai_action="audit").exclude(provider_status="not_sent").filter(total_tokens__isnull=True).count()}


def observe_xml_export(properties, actor, channel):
    """A successful combined export is observed separately for each workspace."""
    grouped = {}
    for prop in properties:
        key = (prop.agency_id, None if prop.agency_id else prop.owner_id)
        grouped[key] = grouped.get(key, 0) + 1
    for (agency_id, user_id), count in grouped.items():
        def record(agency_id=agency_id, user_id=user_id, count=count):
            account = ensure_scope({"agency_id": agency_id, "personal_user_id": user_id})
            decision = _decision(account, actor, Operation.XML_EXPORT, None, timezone.now())
            decision.update(channel=channel, exported_properties=count)
            operation = begin_operation(account=account, actor=actor, action=Operation.XML_EXPORT, decision=decision)
            return finish_operation(operation)

        attempt(record)


def mark_stale_operations(*, age_minutes=15, now=None):
    """No retry, credit refund or implicit success: stale work needs operator review."""
    if type(age_minutes) is not int or age_minutes < 1:
        raise ValueError("A positive stale-operation age is required.")
    now = utc_datetime(now or timezone.now())
    count = 0
    for operation in UsageOperation.objects.filter(status=UsageOperation.Status.RUNNING, started_at__lt=now - timedelta(minutes=age_minutes)).iterator():
        with transaction.atomic():
            BillingAccount.objects.select_for_update().get(pk=operation.account_id)
            operation.refresh_from_db()
            if operation.status == UsageOperation.Status.RUNNING:
                finish_operation(operation, status=UsageOperation.Status.UNKNOWN, error_code="worker_outcome_unknown", now=now)
                count += 1
    return count


@transaction.atomic
def recover_saved_operation(operation):
    """Explicit reconciliation only; never request another provider generation."""
    BillingAccount.objects.select_for_update().get(pk=operation.account_id)
    operation.refresh_from_db()
    if operation.status not in {UsageOperation.Status.RUNNING, UsageOperation.Status.UNKNOWN}:
        return False
    contents = [result.content for result in operation.saved_results.select_related("content").order_by("pk")]
    if not operation.ai_action or not contents:
        return False
    provider = {"provider": operation.provider or contents[0].provider,
        "provider_model": operation.provider_model or contents[0].model}
    finish_operation(operation, result_ids=[content.pk for content in contents],
        deltas={"ai_credits": operation.planned_credits}, provider=provider)
    return True
