"""Atomic workspace claims with optional server-side tariff enforcement."""

from contextlib import contextmanager
import hashlib
import json
import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import BillingAccount, ResourceReservation
from .enforcement import enabled as enforcement_enabled, require_allowed
from .rules import PolicyDecision, PolicyService, collect_usage
from .services import AccountService


class DuplicateResourceRequest(Exception):
    def __init__(self, reservation):
        self.reservation = reservation
        super().__init__("Resource request already exists")


def request_identity(request):
    raw = request.POST.get("request_key") or request.headers.get("Idempotency-Key")
    try:
        key = uuid.UUID(raw) if raw else uuid.uuid4()
    except (TypeError, ValueError):
        raise ValidationError("Некорректный ключ запроса.")
    values = [(name, request.POST.getlist(name)) for name in sorted(request.POST) if name not in {"csrfmiddlewaretoken", "request_key"}]
    def file_identity(item):
        position = item.tell()
        item.seek(0)
        digest = hashlib.sha256()
        for chunk in item.chunks():
            digest.update(chunk)
        item.seek(position)
        return (item.name, item.size, digest.hexdigest())

    files = [(name, [file_identity(item) for item in request.FILES.getlist(name)]) for name in sorted(request.FILES)]
    fingerprint = hashlib.sha256(json.dumps([request.path, values, files], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"request_key": key, "request_fingerprint": fingerprint}


@transaction.atomic
def reserve_resources(*, account, actor, action, amounts, resource=None, request_key=None, request_fingerprint="", invited_role="agent"):
    """Serialize claims per account and evaluate against all earlier active claims."""
    account = BillingAccount.objects.select_for_update().get(pk=account.pk)
    key = uuid.UUID(str(request_key)) if request_key else uuid.uuid4()
    existing = ResourceReservation.objects.filter(request_key=key).first()
    actor_id = getattr(actor, "pk", None)
    if existing:
        if (existing.account_id, existing.actor_id, existing.action, existing.amounts, existing.request_fingerprint) != (account.pk, actor_id, str(action), amounts, request_fingerprint):
            raise ValidationError("Ключ резерва уже относится к другому действию.")
        existing._billing_reused = True
        return existing
    if resource is not None and not AccountService.matches(account, resource):
        raise ValidationError("Нельзя резервировать чужой ресурс на этом счёте.")
    if enforcement_enabled() and "storage_bytes" in amounts:
        from .inventory import reconcile_account
        reconcile_account(account)
    quantity = (amounts.get("storage_bytes") if str(action) == "upload_files" else
                amounts.get("invited_seats") if str(action) == "invite_member" else
                amounts.get("active_properties", 1))
    primary = PolicyService.evaluate(account, actor, action, resource=resource, quantity=quantity, invited_role=invited_role)
    checks = [(str(action), primary)]
    if primary.allowed and "landings" in amounts and str(action) == "create_property":
        context = PolicyService.context(account)
        usage = collect_usage(account)
        used = usage.landings + usage.landings_reserved
        limit = context.terms["limits"]["landings"]
        allowed = used + amounts["landings"] <= limit
        checks.append(("landings", PolicyDecision(allowed, "allowed" if allowed else "quota_exceeded",
            "Действие доступно." if allowed else "Лимит публичных лендингов исчерпан.",
            context.terms["plan_code"], context.state, limit=limit, used=used, required=amounts["landings"])))
    if primary.allowed and "storage_bytes" in amounts and str(action) != "upload_files":
        checks.append(("storage_bytes", PolicyService.evaluate(account, actor, "upload_files",
            quantity=amounts["storage_bytes"])))
    selected = next((result for _, result in checks if not result.allowed), primary)
    require_allowed(selected)
    reservation = ResourceReservation.objects.create(account=account, actor_id=actor_id, request_key=key,
        request_fingerprint=request_fingerprint,
        action=str(action), amounts=amounts, decision={"mode": "enforce" if enforcement_enabled() else "shadow",
            "allowed": selected.allowed, "code": selected.code, "limit": selected.limit, "used": selected.used,
            "required": selected.required, "checks": [{"resource": name, "code": result.code, "allowed": result.allowed}
                for name, result in checks]})
    reservation._billing_reused = False
    return reservation


@transaction.atomic
def finish_reservation(reservation, *, consumed, result_id=""):
    BillingAccount.objects.select_for_update().get(pk=reservation.account_id)
    reservation = ResourceReservation.objects.select_for_update().get(pk=reservation.pk)
    target = ResourceReservation.Status.CONSUMED if consumed else ResourceReservation.Status.RELEASED
    if reservation.status != ResourceReservation.Status.ACTIVE:
        if reservation.status != target or reservation.result_id != str(result_id):
            raise ValidationError("Резерв уже завершён с другим исходом.")
        return reservation
    reservation.status = target
    reservation.result_id = str(result_id)
    reservation.finished_at = timezone.now()
    reservation.save(update_fields=["status", "result_id", "finished_at"])
    return reservation


@contextmanager
def reserved_resources(*, account, actor, action, amounts, resource=None, request_key=None, request_fingerprint="", invited_role="agent"):
    """Keep the account lock until the CRM write and claim transition commit."""
    with transaction.atomic():
        reservation = reserve_resources(account=account, actor=actor, action=action, amounts=amounts, resource=resource,
            request_key=request_key, request_fingerprint=request_fingerprint, invited_role=invited_role)
        if reservation._billing_reused:
            raise DuplicateResourceRequest(reservation)
        try:
            yield reservation
        except BaseException:
            # The surrounding transaction rolls back the claim and CRM write.
            raise
        else:
            finish_reservation(reservation, consumed=True, result_id=reservation.result_id)
