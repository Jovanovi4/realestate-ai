"""Observe committed resource changes, including admin and normal model saves."""

from dataclasses import replace
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.utils import timezone

from properties.audit import current_actor
from properties.models import AgencyInvitation, AgencyMembership, Property, PropertyImage, RealtorProfile

from .inventory import reconcile_account, sync_resource_files
from .models import AccountInventory
from .rules import Operation, PolicyService, collect_usage
from .usage import attempt, begin_operation, enabled, ensure_scope, finish_operation


FIELDS = {
    Property: ("owner_id", "agency_id", "status", "landing_published", "landing_logo"),
    PropertyImage: ("property__owner_id", "property__agency_id", "image"),
    AgencyInvitation: ("agency_id", "accepted_by_id", "expires_at"),
    AgencyMembership: ("agency_id", "user_id", "role"),
}


def _snapshot(sender, pk):
    value = sender.objects.filter(pk=pk).values(*FIELDS[sender]).first()
    if value is not None and sender == PropertyImage:
        value = {"owner_id": value["property__owner_id"], "agency_id": value["property__agency_id"], "image": value["image"]}
    return value


def _before(sender, instance, raw=False, **kwargs):
    if raw or not enabled():
        return
    instance._billing_before = _snapshot(sender, instance.pk) if instance.pk else None


def _scope(value):
    return {"agency_id": value["agency_id"], "personal_user_id": None if value["agency_id"] else value["owner_id"]}


def _metrics(sender, value, now):
    if not value:
        return {}
    if sender == Property:
        active = int(value["status"] != "archived")
        return {"total_properties": 1, "active_properties": active, "landings": int(active and value["landing_published"])}
    if sender == AgencyMembership:
        return {"occupied_seats": 1}
    if sender == AgencyInvitation:
        return {"invited_seats": int(value["accepted_by_id"] is None and value["expires_at"] > now)}
    return {}


def _observe(sender, pk, before, after, actor_id, key, occurred_at):
    value = after or before
    if value is None:
        return
    account = ensure_scope(_scope(value))
    old_metrics, new_metrics = _metrics(sender, before, occurred_at), _metrics(sender, after, occurred_at)
    deltas = {metric: new_metrics.get(metric, 0) - old_metrics.get(metric, 0) for metric in set(old_metrics) | set(new_metrics)}
    old_name = (before or {}).get("image" if sender == PropertyImage else "landing_logo", "")
    file_changed = sender == PropertyImage and (
        before is None or after is None or old_name != (after or {}).get("image", "") or _scope(before) != _scope(value)
    ) or sender == Property and (
        old_name != (after or {}).get("landing_logo", "") or before and _scope(before) != _scope(value)
    )
    scope_changed = before and after and _scope(before) != _scope(after)
    if not any(deltas.values()) and not file_changed and not scope_changed:
        return
    actor = get_user_model().objects.filter(pk=actor_id).first() if actor_id else None
    old_account = ensure_scope(_scope(before)) if scope_changed else None
    if file_changed:
        sync_resource_files(account, sender._meta.label_lower, pk, old_name=old_name)
        # Completeness is established from all references, including archive/demos.
        initialized = AccountInventory.objects.filter(account=account, references_complete=True).exists()
        reconcile_account(account, scan_files=not initialized)
    if old_account and old_account.pk != account.pk:
        # Move file references first, so an explicit reassignment is not mistaken
        # for a retained orphan in the old workspace.
        reconcile_account(old_account, scan_files=True)
    usage = collect_usage(account)
    evaluation_actor = actor or get_user_model().objects.get(pk=account.owner_id)
    decision = {"mode": "shadow", "allowed": True, "code": "resource_observed", "evaluated_as_owner": actor is None}
    action = "resource.changed"
    if sender == Property:
        resource = Property.objects.filter(pk=pk).first()
        if not before and after:
            action = Operation.CREATE_PROPERTY
        elif before and after and before["status"] == "archived" and after["status"] != "archived":
            action = Operation.RESTORE_PROPERTY
        elif deltas.get("landings", 0) > 0:
            action = Operation.PUBLISH_LANDING
        elif deltas.get("active_properties", 0) < 0:
            action = Operation.ARCHIVE_PROPERTY if resource else "resource.deleted"
        before_usage = replace(usage, **{metric: max(0, getattr(usage, metric) - delta) for metric, delta in deltas.items()})
        if resource and action in Operation._value2member_map_:
            if before:
                resource.status, resource.landing_published = before["status"], before["landing_published"]
            result = PolicyService.evaluate(account, evaluation_actor, action, resource=resource, usage=before_usage)
            decision.update(allowed=result.allowed, code=result.code, limit=result.limit, used=result.used)
            if deltas.get("landings", 0) > 0 and action != Operation.PUBLISH_LANDING:
                resource.status = after["status"]
                resource.landing_published = False
                publication = PolicyService.evaluate(account, evaluation_actor, Operation.PUBLISH_LANDING, resource=resource, usage=before_usage)
                if not publication.allowed:
                    decision.update(allowed=False, code=publication.code, limit=publication.limit, used=publication.used)
    elif sender in {AgencyInvitation, AgencyMembership}:
        action = "team.seats"
        context = PolicyService.context(account)
        seats = usage.occupied_seats + usage.invited_seats
        allowed = seats <= context.terms["limits"]["seats"] and (context.terms["features"]["team"] or not any(delta > 0 for delta in deltas.values()))
        decision.update(allowed=allowed, code="resource_observed" if allowed else "team_capacity_or_feature", used=seats, limit=context.terms["limits"]["seats"])
    if file_changed:
        field = "image" if sender == PropertyImage else "landing_logo"
        new_name = (after or {}).get(field, "")
        if new_name and new_name != old_name:
            if action == "resource.changed":
                action = Operation.UPLOAD_FILES
            size = account.stored_files.filter(name=new_name).values_list("size_bytes", flat=True).first()
            if size is None:
                decision.update(allowed=False, code="usage_unknown")
            elif size > 0:
                before_storage = None if usage.storage_bytes is None else max(0, usage.storage_bytes - size)
                result = PolicyService.evaluate(account, evaluation_actor, Operation.UPLOAD_FILES, quantity=size, usage=replace(usage, storage_bytes=before_storage))
                decision["storage_check"] = {"allowed": result.allowed, "code": result.code, "limit": result.limit, "used": result.used}
                if decision["allowed"] or not result.allowed:
                    decision.update(allowed=result.allowed, code=result.code, limit=result.limit, used=result.used)
        else:
            action = Operation.REMOVE_FILES
    if not scope_changed:
        # Reconciliation has already established the real file totals; its ledger
        # writes must not be duplicated by a per-file event.
        operation = begin_operation(account=account, actor=actor, action=action, request_key=key,
            resource_ref=(sender._meta.label_lower, pk), now=occurred_at, decision=decision)
        finish_operation(operation, deltas=deltas if not file_changed else {}, now=timezone.now())
    reconcile_account(account, scan_files=False)


def _schedule(sender, instance, before, after):
    if not enabled():
        return
    actor = current_actor()
    actor_id = getattr(actor, "pk", None)
    key, now, pk = uuid.uuid4(), timezone.now(), instance.pk
    # A rolled-back CRM transaction must not leave an observed successful change.
    transaction.on_commit(lambda: attempt(lambda: _observe(sender, pk, before, after, actor_id, key, now)))


def _after_save(sender, instance, created, raw=False, **kwargs):
    if raw or not enabled():
        return
    _schedule(sender, instance, getattr(instance, "_billing_before", None), _snapshot(sender, instance.pk))


def _before_delete(sender, instance, **kwargs):
    if enabled():
        instance._billing_before = _snapshot(sender, instance.pk)


def _after_delete(sender, instance, **kwargs):
    _schedule(sender, instance, getattr(instance, "_billing_before", None), None)


def _verified_email(sender, instance, raw=False, **kwargs):
    if not raw and enabled() and instance.email_verified_at:
        from .services import SubscriptionService

        user = instance.user
        transaction.on_commit(lambda: attempt(lambda: SubscriptionService.ensure_demo_grant(user)))


def connect_usage_signals():
    for model in FIELDS:
        label = model._meta.label_lower
        pre_save.connect(_before, sender=model, dispatch_uid=f"billing-before-{label}")
        post_save.connect(_after_save, sender=model, dispatch_uid=f"billing-after-{label}")
        pre_delete.connect(_before_delete, sender=model, dispatch_uid=f"billing-before-delete-{label}")
        post_delete.connect(_after_delete, sender=model, dispatch_uid=f"billing-after-delete-{label}")
    post_save.connect(_verified_email, sender=RealtorProfile, dispatch_uid="billing-confirmed-email")
