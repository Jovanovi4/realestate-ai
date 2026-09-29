"""Read-only subscription information for the authenticated workspace."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from .catalog import validate_snapshot
from .models import BillingPeriod, Plan
from .overview_usage import credit_summary, resource_summary
from .rules import AccessContext, AccessState, PolicyService
from .services import current_plan_version, utc_datetime


ACCESS_LABELS = {
    AccessState.DEMO: ("Бесплатный доступ", "Демо действует без ограничения по сроку.", "secondary"),
    AccessState.ACTIVE: ("Действует", "Доступ назначен до конца расчётного периода.", "success"),
    AccessState.GRACE: ("Льготный срок", "Расчётный период завершился. Ниже указана дата окончания льготного срока.", "warning"),
    AccessState.RESTRICTED: ("Возврат на Демо", "Назначенный доступ завершён. Ваши данные сохранены.", "secondary"),
    AccessState.SUSPENDED: ("Приостановлен", "Подписка приостановлена. Ваши данные сохранены.", "warning"),
    AccessState.UNKNOWN: ("Требует проверки", "Не удалось определить тариф. Обратитесь в поддержку.", "warning"),
}


def _period_summary(period):
    if period is None:
        return None
    return {
        "plan_name": Plan.Code(period.plan_version.plan.code).label,
        "source_label": period.get_source_display(),
        "starts_at": period.starts_at,
        "ends_at": period.ends_at,
    }


def _access_context(account, now):
    try:
        if account is not None:
            context = PolicyService.context(account, now=now)
        else:
            version = current_plan_version(Plan.Code.DEMO)
            if version is None:
                raise ValidationError("Каталог тарифов не настроен.")
            terms = version.snapshot()
            validate_snapshot(terms)
            context = AccessContext(AccessState.DEMO, terms)
    except ValidationError:
        context = AccessContext(AccessState.UNKNOWN, {})
    return context


def _subscription_summary(account, context, now):
    label, description, badge = ACCESS_LABELS[context.state]
    summary = {
        "plan_name": Plan.Code(context.terms["plan_code"]).label if context.terms else "Не определён",
        "state": context.state,
        "state_label": label,
        "state_description": description,
        "badge": badge,
        "period": None,
        "period_label": "Расчётный период",
        "upcoming_period": None,
        "grace_ends_at": None,
    }
    if account is None or context.state == AccessState.UNKNOWN:
        return summary, None

    periods = BillingPeriod.objects.filter(subscription__account=account, voided_at__isnull=True).select_related("plan_version__plan")
    if context.period_id:
        period = periods.filter(pk=context.period_id).first()
        if period and context.state == AccessState.GRACE:
            summary["period_label"] = "Завершённый период"
            summary["grace_ends_at"] = period.ends_at + timedelta(days=context.terms["grace_days"])
    else:
        period = periods.filter(starts_at__lte=now, ends_at__gt=now).first() if context.state == AccessState.SUSPENDED else None
        if period:
            summary["period_label"] = "Назначенный период"
        else:
            period = periods.filter(ends_at__lte=now).order_by("-ends_at", "-pk").first()
            summary["period_label"] = "Последний период"
    summary["period"] = _period_summary(period)
    if period and context.state == AccessState.SUSPENDED and period.ends_at > now:
        # Display the assigned plan with a suspension badge; policy access still
        # comes solely from PolicyService and is never changed by this page.
        summary["plan_name"] = summary["period"]["plan_name"]
    upcoming = periods.filter(starts_at__gt=now).order_by("starts_at", "pk").first()
    summary["upcoming_period"] = _period_summary(upcoming)
    return summary, period


def subscription_summary(account, *, now=None):
    """An absent account gets the default Demo display without provisioning it."""
    now = utc_datetime(now or timezone.now())
    context = _access_context(account, now)
    return _subscription_summary(account, context, now)[0]


def workspace_summary(account, *, now=None):
    """Read tariff and usage once for a persisted account or an unsaved scope."""
    now = utc_datetime(now or timezone.now())
    saved_account = account if account.pk else None
    context = _access_context(saved_account, now)
    summary, period = _subscription_summary(saved_account, context, now)
    terms = context.terms
    if context.state == AccessState.SUSPENDED and period and period.ends_at > now:
        # Assigned plan limits match its displayed name, but remain informational.
        try:
            validate_snapshot(period.terms_snapshot)
            terms = period.terms_snapshot
        except ValidationError:
            terms = {}
    summary.update(resource_summary(account, terms, now=now))
    summary["ai"] = credit_summary(account, context, period=period, now=now)
    if summary["period"] and context.state in {AccessState.RESTRICTED, AccessState.SUSPENDED} and period.ends_at <= now:
        summary["period"]["ai"] = credit_summary(account, context, period=period, now=now, historical=True)
    return summary
