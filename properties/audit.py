from contextvars import ContextVar

from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

from .models import (
    AIContent, AgencyInvitation, AgencyMembership, AuditEvent, Client, ClientInteraction,
    ClientReminder, Deal, Lead, Property, PropertyImage, RealtorProfile, Showing,
)


_actor = ContextVar("audit_actor", default=None)
TRACKED_MODELS = (
    Property, PropertyImage, Client, ClientInteraction, ClientReminder, Lead,
    Deal, Showing, AIContent, RealtorProfile, AgencyMembership, AgencyInvitation,
)


class AuditActorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _actor.set(request.user if request.user.is_authenticated else None)
        try:
            return self.get_response(request)
        finally:
            _actor.reset(token)


def _scope(instance):
    if isinstance(instance, (PropertyImage, AIContent, Lead)):
        record = instance.property
    elif isinstance(instance, ClientInteraction):
        record = instance.client
    elif isinstance(instance, (AgencyMembership, AgencyInvitation)):
        return instance.agency_id, None
    elif isinstance(instance, RealtorProfile):
        return None, instance.user_id
    else:
        record = instance
    return record.agency_id, record.owner_id


def _before_save(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or not instance.pk:
        instance._audit_changed_fields = []
        return
    fields = [
        field for field in sender._meta.concrete_fields
        if not field.primary_key and not getattr(field, "auto_now", False)
        and (update_fields is None or field.name in update_fields or field.attname in update_fields)
    ]
    old = sender.objects.filter(pk=instance.pk).values(*[field.attname for field in fields]).first()
    instance._audit_changed_fields = [field.verbose_name or field.name for field in fields if old and old[field.attname] != getattr(instance, field.attname)]


def _after_save(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    changed = getattr(instance, "_audit_changed_fields", [])
    if not created and not changed:
        return
    agency_id, owner_id = _scope(instance)
    actor = _actor.get()
    AuditEvent.objects.create(
        agency_id=agency_id, owner_id=owner_id, actor=actor if getattr(actor, "is_authenticated", False) else None,
        model_name=str(sender._meta.verbose_name).capitalize(), object_pk=str(instance.pk),
        action="created" if created else "updated", changed_fields=changed if not created else [],
    )


def _before_delete(sender, instance, **kwargs):
    instance._audit_scope = _scope(instance)


def _after_delete(sender, instance, **kwargs):
    agency_id, owner_id = instance._audit_scope
    actor = _actor.get()
    AuditEvent.objects.create(
        agency_id=agency_id, owner_id=owner_id, actor=actor if getattr(actor, "is_authenticated", False) else None,
        model_name=str(sender._meta.verbose_name).capitalize(), object_pk=str(instance.pk), action="deleted",
    )


def connect_audit_signals():
    for model in TRACKED_MODELS:
        pre_save.connect(_before_save, sender=model, dispatch_uid=f"audit-before-save-{model._meta.label_lower}")
        post_save.connect(_after_save, sender=model, dispatch_uid=f"audit-after-save-{model._meta.label_lower}")
        pre_delete.connect(_before_delete, sender=model, dispatch_uid=f"audit-before-delete-{model._meta.label_lower}")
        post_delete.connect(_after_delete, sender=model, dispatch_uid=f"audit-after-delete-{model._meta.label_lower}")
