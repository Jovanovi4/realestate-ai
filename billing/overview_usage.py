"""Presentation of live resources and observed credit buckets; no writes."""

from decimal import Decimal

from django.core.exceptions import ValidationError

from properties.models import RealtorProfile

from .catalog import validate_snapshot
from .models import AccountInventory, DemoCreditGrant, UsageOperation
from .rules import AccessState, collect_usage
from .usage import observed_credits


def format_storage_bytes(value):
    """Use the binary MB/GB units agreed in BP-0.1, without hiding small files."""
    if value is None:
        return "Объём уточняется"
    if value == 0:
        return "0 МБ"
    for divisor, unit in ((1024**3, "ГБ"), (1024**2, "МБ"), (1024, "КБ")):
        if value >= divisor:
            number = (Decimal(value) / divisor).quantize(Decimal("0.1"))
            return f"{format(number, 'f').removesuffix('.0').replace('.', ',')} {unit}"
    return f"{value} Б"


def _meter(used, limit):
    known = used is not None and limit is not None
    excess = max(0, used - limit) if known else None
    return {
        "used": used, "limit": limit, "known": known, "excess": excess,
        "over_limit": bool(excess),
        "at_limit": bool(known and limit > 0 and used == limit),
        "percent": min(100, used * 100 // limit) if known and limit else 100 if excess else 0,
    }


def resource_summary(account, terms, *, now):
    usage = collect_usage(account, now=now)
    limits = terms.get("limits", {})
    archived = usage.total_properties - usage.active_properties
    seats = usage.occupied_seats + usage.invited_seats
    rows = [
        {"key": "active_properties", "label": "Объекты",
         "hint": f"Вне архива, включая черновики. В архиве: {archived}.",
         **_meter(usage.active_properties, limits.get("active_properties"))},
        {"key": "landings", "label": "Лендинги",
         "hint": "Публичные страницы объектов вне архива.",
         **_meter(usage.landings, limits.get("landings"))},
        {"key": "seats", "label": "Места",
         "hint": f"Участники: {usage.occupied_seats} · Действующие приглашения: {usage.invited_seats}." if account.agency_id else "Место владельца личного пространства.",
         **_meter(seats, limits.get("seats"))},
        {"key": "storage_bytes", "label": "Хранилище",
         "hint": "Фото и логотипы, включая архив.",
         **_meter(usage.storage_bytes, limits.get("storage_bytes"))},
    ]
    for row in rows:
        row["used_label"] = format_storage_bytes(row["used"]) if row["key"] == "storage_bytes" else str(row["used"])
        row["limit_label"] = format_storage_bytes(row["limit"]) if row["key"] == "storage_bytes" and row["limit"] is not None else str(row["limit"]) if row["limit"] is not None else "не определён"
        row["excess_label"] = format_storage_bytes(row["excess"]) if row["key"] == "storage_bytes" and row["excess"] else str(row["excess"])
    inventory = AccountInventory.objects.filter(account=account).first() if account.pk else None
    return {"resources": rows, "inventory_checked_at": inventory.checked_at if inventory else None,
            "storage_issues": bool(inventory and inventory.last_issue_count), "storage_known": usage.storage_bytes is not None}


def _bucket_totals(account, bucket, *, period=False, context=None):
    if period:
        operations = UsageOperation.objects.filter(account=account, period_id=bucket.pk)
        allocated, used, reserved = bucket.ai_credits_allocated, bucket.ai_credits_used, bucket.ai_credits_reserved
        scope = {"period_id": bucket.pk}
    else:
        # A Demo grant keeps its lifetime spend after the founder's account moves.
        operations = UsageOperation.objects.filter(demo_grant_id=bucket.pk)
        allocated, used, reserved = bucket.credits_allocated, bucket.credits_used, bucket.credits_reserved
        scope = {"demo_grant_id": bucket.pk}
    if context is not None:
        used, pending = context.ai_used, max(0, context.ai_reserved - reserved)
    else:
        observed, pending = observed_credits(account=account, **scope)
        used += observed
    committed = used + reserved + pending
    return {**_meter(committed, allocated), "allocated": allocated, "used": used, "reserved": reserved, "pending": pending,
            "remaining": max(0, allocated - committed),
            "spent": used, "unknown_operations": operations.filter(status=UsageOperation.Status.UNKNOWN).exists()}


def credit_summary(account, context, *, period, now, historical=False):
    summary = {"known": True, "allocated": 0, "spent": 0, "reserved": 0, "pending": 0,
               "remaining": 0, "percent": 0, "over_limit": False, "excess": 0,
               "unknown_operations": False, "title": "ИИ-кредиты", "remaining_label": "Остаток",
               "hint": "", "profile_link": False}
    if context.state == AccessState.UNKNOWN and not historical:
        return {**summary, "known": False, "hint": "Не удалось проверить ИИ-бюджет. Обратитесь в поддержку."}
    if context.period_id and period is None and not historical:
        return {**summary, "known": False, "hint": "Не удалось проверить ИИ-бюджет периода. Обновите страницу."}
    period_bucket = period if period and (historical or context.period_id or context.state == AccessState.SUSPENDED and period.ends_at > now) else None
    if period_bucket:
        try:
            validate_snapshot(period_bucket.terms_snapshot)
        except ValidationError:
            return {**summary, "known": False, "hint": "Не удалось проверить ИИ-бюджет периода."}
        current = context if context.state == AccessState.ACTIVE and not historical else None
        summary.update(_bucket_totals(account, period_bucket, period=True, context=current))
        summary["hint"] = "Общий бюджет команды за расчётный период." if account.agency_id else "Бюджет за текущий расчётный период."
        if historical or context.state == AccessState.GRACE:
            summary.update(remaining_label="Остаток периода",
                           hint="Расчётный период завершён. Остаток не переносится в следующий период.")
        elif context.state == AccessState.SUSPENDED:
            summary.update(remaining_label="Остаток периода", hint="Бюджет назначенного периода. Подписка приостановлена.")
        return summary

    if account.personal_user_id:
        grant = DemoCreditGrant.objects.filter(user_id=account.personal_user_id, account_id=account.pk).first() if account.pk else None
        if grant:
            try:
                validate_snapshot(grant.terms_snapshot)
            except ValidationError:
                return {**summary, "known": False, "hint": "Не удалось проверить бонус Демо. Обратитесь в поддержку."}
            current = context if context.state in {AccessState.DEMO, AccessState.RESTRICTED} else None
            summary.update(_bucket_totals(account, grant, context=current))
            summary.update(hint="Разовый бонус Демо. Он не обновляется каждый месяц.")
        else:
            verified = RealtorProfile.objects.filter(user_id=account.personal_user_id, email_verified_at__isnull=False).exists()
            summary.update(profile_link=not verified,
                           hint="Email подтверждён. Разовый бонус Демо ещё не начислен." if verified else "Подтвердите email в личном кабинете, чтобы получить разовый бонус Демо.")
    else:
        summary["hint"] = "ИИ-бюджет команды не назначен. Личные кредиты участников не добавляются к бюджету команды."
    if context.state == AccessState.SUSPENDED:
        summary["hint"] += " Подписка приостановлена."
    return summary
