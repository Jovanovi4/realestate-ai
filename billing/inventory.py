"""Read storage metadata only; never delete or rewrite user files during metering."""

from pathlib import Path, PurePosixPath

from django.core.files.storage import FileSystemStorage, default_storage
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from properties.models import Property, PropertyImage

from .models import AccountInventory, BillingAccount, FileReference, StoredFile, UsageEntry
from .rules import collect_usage


def property_scope(account, prefix=""):
    return {f"{prefix}agency_id": account.agency_id} if account.agency_id else {f"{prefix}agency__isnull": True, f"{prefix}owner_id": account.personal_user_id}


def measure_file(name, storage=None):
    storage = storage or default_storage
    try:
        path = PurePosixPath(name)
        if not name or path.is_absolute() or "\\" in name or any(part in {"", ".", ".."} for part in name.split("/")):
            return None, StoredFile.State.UNKNOWN, "unsafe_storage_name"
        if isinstance(storage, FileSystemStorage):
            current = Path(storage.location)
            if current.is_symlink():
                return None, StoredFile.State.UNKNOWN, "storage_symlink"
            for part in path.parts:
                current = current / part
                if current.is_symlink():
                    return None, StoredFile.State.UNKNOWN, "storage_symlink"
            if not current.exists():
                return 0, StoredFile.State.MISSING, "missing_file"
            if not current.is_file():
                return None, StoredFile.State.UNKNOWN, "not_regular_file"
            size = current.stat().st_size
        else:
            if not storage.exists(name):
                return 0, StoredFile.State.MISSING, "missing_file"
            size = storage.size(name)
        if type(size) is not int or size < 0:
            return None, StoredFile.State.UNKNOWN, "invalid_storage_size"
        return size, StoredFile.State.PRESENT, ""
    except Exception:
        return None, StoredFile.State.UNKNOWN, "storage_metadata_error"


def _measure(asset, now):
    if asset.state == StoredFile.State.TRANSFERRED and not asset.references.exists():
        return asset
    asset.size_bytes, asset.state, asset.issue_code = measure_file(asset.name)
    asset.checked_at = now
    asset.save()
    return asset


def _reference(account, resource_type, resource_id, field_name, name, now):
    key = {"resource_type": resource_type, "resource_id": str(resource_id), "field_name": field_name}
    previous = FileReference.objects.select_related("file").filter(**key).first()
    asset = None
    if name:
        asset, _ = StoredFile.objects.get_or_create(account=account, name=name)
        FileReference.objects.update_or_create(**key, defaults={"file": asset})
        # A previously transferred file can return to this workspace. Attach its
        # reference before measuring, so the retired-file shortcut cannot hide it.
        _measure(asset, now)
        for retired in StoredFile.objects.filter(name=name).exclude(account=account).filter(references__isnull=True):
            retired.state, retired.size_bytes, retired.issue_code, retired.checked_at = StoredFile.State.TRANSFERRED, 0, "", now
            retired.save()
            AccountInventory.objects.filter(account_id=retired.account_id).update(files_complete=False)
    elif previous:
        previous.delete()
    if previous and (not asset or previous.file_id != asset.pk):
        old = previous.file
        if asset and old.name == asset.name and old.account_id != account.pk and not old.references.exists():
            # Explicit resource reassignment, not a claimed successful file deletion.
            old.state, old.size_bytes, old.issue_code, old.checked_at = StoredFile.State.TRANSFERRED, 0, "", now
            old.save()
            AccountInventory.objects.filter(account_id=old.account_id).update(files_complete=False)
        else:
            _measure(old, now)  # Unreferenced but still saved files continue occupying bytes.
    return asset


def sync_resource_files(account, resource_type, resource_id, *, old_name="", now=None):
    now = now or timezone.now()
    if old_name:
        old, _ = StoredFile.objects.get_or_create(account=account, name=old_name)
        _measure(old, now)
    if resource_type == "properties.property":
        prop = Property.objects.filter(pk=resource_id).first()
        _reference(account, resource_type, resource_id, "landing_logo", prop.landing_logo.name if prop else "", now)
        if prop:
            for image in prop.images.all():
                _reference(account, "properties.propertyimage", image.pk, "image", image.image.name, now)
    elif resource_type == "properties.propertyimage":
        image = PropertyImage.objects.filter(pk=resource_id).first()
        _reference(account, resource_type, resource_id, "image", image.image.name if image else "", now)


def _scan_references(account, now):
    seen = set()
    for prop in Property.objects.filter(**property_scope(account)).only("pk", "landing_logo").iterator():
        key = ("properties.property", str(prop.pk), "landing_logo")
        if prop.landing_logo:
            seen.add(key)
        _reference(account, *key, prop.landing_logo.name, now)
    for image in PropertyImage.objects.filter(**property_scope(account, "property__")).only("pk", "image").iterator():
        key = ("properties.propertyimage", str(image.pk), "image")
        if image.image:
            seen.add(key)
        _reference(account, *key, image.image.name, now)
    for reference in FileReference.objects.filter(file__account=account).iterator():
        if (reference.resource_type, reference.resource_id, reference.field_name) not in seen:
            reference.delete()
    for asset in account.stored_files.all().iterator():
        _measure(asset, now)


@transaction.atomic
def reconcile_account(account, *, scan_files=True, now=None):
    """A baseline/correction ledger entry, never a retroactive AI charge."""
    from .usage import begin_operation, finish_operation

    now = now or timezone.now()
    account = BillingAccount.objects.select_for_update().get(pk=account.pk)
    inventory, _ = AccountInventory.objects.get_or_create(account=account)
    if scan_files:
        _scan_references(account, now)
        inventory.references_complete = True
    inventory.files_complete = inventory.references_complete and not account.stored_files.filter(size_bytes__isnull=True).exists()
    inventory.last_issue_count = account.stored_files.exclude(issue_code="").count()
    inventory.checked_at = now
    inventory.save()
    usage = collect_usage(account, now=now)
    metrics = {key: value for key, value in vars(usage).items() if key in UsageEntry.Metric.values and value is not None}
    ledger = {row["metric"]: row["value"] for row in account.usage_entries.values("metric").annotate(value=Sum("delta"))}
    deltas = {key: value - ledger.get(key, 0) for key, value in metrics.items() if value != ledger.get(key, 0)}
    if deltas:
        operation = begin_operation(account=account, actor=None, action="inventory.reconcile", now=now,
            decision={"mode": "shadow", "allowed": True, "code": "inventory_correction", "files_complete": inventory.files_complete})
        finish_operation(operation, deltas=deltas, now=now)
    return {"account_id": account.pk, "files_complete": inventory.files_complete,
            "issue_count": inventory.last_issue_count, "usage": vars(usage), "corrections": deltas}


def refresh_saved_file(account, name):
    """Call after an actual storage delete; a failed delete must retain occupied bytes."""
    from .usage import attempt

    def refresh():
        BillingAccount.objects.select_for_update().get(pk=account.pk)
        asset = StoredFile.objects.filter(account=account, name=name).first()
        if asset:
            _measure(asset, timezone.now())
        return reconcile_account(account, scan_files=False)

    return attempt(refresh)


def unassigned_media_report():
    """Local-storage pilot diagnostic; report counts only, do not expose names or clean up."""
    if not isinstance(default_storage, FileSystemStorage):
        return {"supported": False, "unassigned_files": None, "unassigned_bytes": None}
    known = set(StoredFile.objects.values_list("name", flat=True))
    # Current avatar is a separate technical resource, not this account quota.
    root = Path(default_storage.location)
    count, size, unknown = 0, 0, 0
    if root.is_symlink():
        return {"supported": False, "unassigned_files": None, "unassigned_bytes": None}
    stack = [root / prefix for prefix in ("properties", "landing_logos")]
    while stack:
        directory = stack.pop()
        if directory.is_symlink():
            unknown += 1
            continue
        if not directory.exists():
            continue
        try:
            for path in directory.iterdir():
                if path.is_symlink():
                    unknown += 1
                elif path.is_dir():
                    stack.append(path)
                elif path.is_file() and path.relative_to(root).as_posix() not in known:
                    count += 1
                    size += path.stat().st_size
        except OSError:
            unknown += 1
    return {"supported": True, "unassigned_files": count, "unassigned_bytes": size if not unknown else None, "unreadable_entries": unknown}
