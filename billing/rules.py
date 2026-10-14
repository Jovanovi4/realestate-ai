"""Read-only tariff decisions shared by resource reservations and views."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.utils import timezone

from properties.models import AgencyInvitation, AgencyMembership, Property

from .catalog import validate_snapshot
from .models import AccountInventory, DemoCreditGrant, Plan, ResourceReservation, Subscription
from .services import AccountService, current_plan_version, utc_datetime


class AccessState(StrEnum):
    DEMO = "demo"
    ACTIVE = "active"
    GRACE = "grace"
    RESTRICTED = "restricted"
    SUSPENDED = "suspended"
    UNKNOWN = "unknown"


class Operation(StrEnum):
    VIEW = "view"
    CSV_EXPORT = "csv_export"
    MANAGE_BILLING = "manage_billing"
    EDIT_CRM = "edit_crm"
    CREATE_PROPERTY = "create_property"
    RESTORE_PROPERTY = "restore_property"
    ARCHIVE_PROPERTY = "archive_property"
    PUBLISH_LANDING = "publish_landing"
    UNPUBLISH_LANDING = "unpublish_landing"
    PUBLIC_LANDING = "public_landing"
    PUBLISH_TELEGRAM = "publish_telegram"
    UPLOAD_FILES = "upload_files"
    REMOVE_FILES = "remove_files"
    AI_GENERATE = "ai_generate"
    XML_EXPORT = "xml_export"
    CREATE_AGENCY = "create_agency"
    INVITE_MEMBER = "invite_member"
    ACCEPT_INVITE = "accept_invite"
    REMOVE_MEMBER = "remove_member"
    CHANGE_MEMBER_ROLE = "change_member_role"


@dataclass(frozen=True)
class UsageSnapshot:
    active_properties: int | None = None
    total_properties: int | None = None
    landings: int | None = None
    occupied_seats: int | None = None
    invited_seats: int | None = None
    storage_bytes: int | None = None
    storage_reserved_bytes: int = 0
    active_properties_reserved: int = 0
    total_properties_reserved: int = 0
    landings_reserved: int = 0
    invited_seats_reserved: int = 0

    def __post_init__(self):
        for value in vars(self).values():
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Usage must be non-negative integers or unknown (None).")


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    code: str
    message: str
    plan_code: str | None = None
    state: AccessState = AccessState.UNKNOWN
    limit: int | None = None
    used: int | None = None
    required: int = 0
    ai_cost: int = 0


@dataclass(frozen=True)
class AccessContext:
    state: AccessState
    terms: dict
    period_id: int | None = None
    ai_allocated: int = 0
    ai_used: int = 0
    ai_reserved: int = 0


def collect_usage(account, *, now=None):
    """Live counts, including for an unsaved scope; never provision an account."""
    now = utc_datetime(now or timezone.now())
    if bool(account.personal_user_id) == bool(account.agency_id):
        raise ValueError("Usage requires exactly one workspace scope.")
    scope = {"agency_id": account.agency_id} if account.agency_id else {"agency__isnull": True, "owner_id": account.personal_user_id}
    properties = Property.objects.filter(**scope)
    counts = properties.aggregate(
        total=Count("pk"),
        active=Count("pk", filter=~Q(status="archived")),
        landings=Count("pk", filter=Q(landing_published=True) & ~Q(status="archived")),
    )
    occupied, invited = 1, 0
    if account.agency_id:
        occupied = AgencyMembership.objects.filter(agency_id=account.agency_id).count()
        invited = AgencyInvitation.objects.filter(agency_id=account.agency_id, accepted_by__isnull=True, revoked_at__isnull=True, expires_at__gt=now).count()
    inventory = AccountInventory.objects.filter(account=account, references_complete=True, files_complete=True).first() if account.pk else None
    storage = None
    if inventory:
        files = account.stored_files.aggregate(value=Sum("size_bytes"), unknown=Count("pk", filter=Q(size_bytes__isnull=True)))
        if not files["unknown"]:
            storage = files["value"] or 0
    reserved = {key: 0 for key in ("active_properties", "total_properties", "landings", "invited_seats", "storage_bytes")}
    if account.pk:
        for amounts in ResourceReservation.objects.filter(account=account, status=ResourceReservation.Status.ACTIVE).values_list("amounts", flat=True):
            for key, value in amounts.items():
                reserved[key] += value
    return UsageSnapshot(active_properties=counts["active"], total_properties=counts["total"], landings=counts["landings"],
        occupied_seats=occupied, invited_seats=invited, storage_bytes=storage,
        active_properties_reserved=reserved["active_properties"], total_properties_reserved=reserved["total_properties"],
        landings_reserved=reserved["landings"], invited_seats_reserved=reserved["invited_seats"],
        storage_reserved_bytes=reserved["storage_bytes"])


class PolicyService:
    @staticmethod
    def context(account, *, now=None):
        from .usage import observed_credits

        now = utc_datetime(now or timezone.now())
        subscription = Subscription.objects.filter(account=account).first()
        if subscription and not subscription.is_suspended:
            active = list(subscription.periods.filter(voided_at__isnull=True, starts_at__lte=now, ends_at__gt=now)[:2])
            if len(active) > 1:
                raise ValidationError("Пересекающиеся периоды: требуется проверка оператора.")
            if active:
                period = active[0]
                validate_snapshot(period.terms_snapshot)
                observed, pending = observed_credits(account=account, period_id=period.pk)
                return AccessContext(AccessState.ACTIVE, deepcopy(period.terms_snapshot), period.pk, period.ai_credits_allocated, period.ai_credits_used + observed, period.ai_credits_reserved + pending)
            previous = subscription.periods.filter(voided_at__isnull=True, ends_at__lte=now).order_by("-ends_at").first()
            if previous:
                validate_snapshot(previous.terms_snapshot)
                if now < previous.ends_at + timedelta(days=previous.terms_snapshot["grace_days"]):
                    return AccessContext(AccessState.GRACE, deepcopy(previous.terms_snapshot), previous.pk)
        version = current_plan_version(Plan.Code.DEMO)
        if version is None:
            raise ValidationError("Каталог тарифов не настроен.")
        terms = version.snapshot()
        validate_snapshot(terms)
        state = AccessState.SUSPENDED if subscription and subscription.is_suspended else AccessState.RESTRICTED if subscription and subscription.periods.filter(starts_at__lte=now).exists() else AccessState.DEMO
        grant = DemoCreditGrant.objects.filter(user_id=account.personal_user_id, account=account).first() if account.personal_user_id and state != AccessState.SUSPENDED else None
        if grant:
            validate_snapshot(grant.terms_snapshot)
            terms["ai_costs"] = deepcopy(grant.terms_snapshot["ai_costs"])
            observed, pending = observed_credits(account=account, demo_grant_id=grant.pk)
            return AccessContext(state, terms, ai_allocated=grant.credits_allocated, ai_used=grant.credits_used + observed, ai_reserved=grant.credits_reserved + pending)
        return AccessContext(state, terms)

    @staticmethod
    def _role(account, actor):
        if not actor or not actor.is_authenticated or not actor.is_active:
            return None
        if account.personal_user_id:
            return "owner" if account.personal_user_id == actor.pk else None
        return AgencyMembership.objects.filter(agency_id=account.agency_id, user=actor).values_list("role", flat=True).first()

    @classmethod
    def evaluate(cls, account, actor, operation, *, resource=None, usage=None, quantity=1, ai_action=None, invited_role="agent", now=None):
        """Return a decision, never raise quota errors in UI or mutate usage/subscription."""
        try:
            operation = Operation(operation)
        except (TypeError, ValueError):
            return PolicyDecision(False, "invalid_operation", "Неизвестное действие.")
        if type(quantity) is not int or quantity < 1:
            return PolicyDecision(False, "invalid_quantity", "Нужно положительное целое количество.")
        now = utc_datetime(now or timezone.now())
        if resource is not None:
            try:
                matches = AccountService.matches(account, resource)
            except ValueError:
                return PolicyDecision(False, "invalid_resource", "Неизвестный ресурс рабочего пространства.")
            if not matches:
                return PolicyDecision(False, "wrong_workspace", "Ресурс принадлежит другому рабочему пространству.")
        if account.agency_id and account.agency.closed_at:
            return PolicyDecision(False, "team_closed", "Команда закрыта. Данные доступны владельцу в архиве.")
        role = cls._role(account, actor)
        if operation == Operation.PUBLIC_LANDING:
            if not isinstance(resource, Property) or not resource.landing_published or resource.status == "archived":
                return PolicyDecision(False, "landing_unavailable", "Страница недоступна.")
        elif operation == Operation.ACCEPT_INVITE:
            if (not actor or not actor.is_authenticated or not actor.is_active
                    or not isinstance(resource, AgencyInvitation) or not account.agency_id):
                return PolicyDecision(False, "forbidden", "Нужно действующее приглашение и вход в аккаунт.")
            if resource.accepted_by_id == actor.pk:
                if role:
                    return PolicyDecision(True, "already_accepted", "Приглашение уже принято.")
                return PolicyDecision(False, "forbidden", "Доступ к команде уже отозван.")
            if resource.accepted_by_id or resource.revoked_at or resource.expires_at <= now or AgencyMembership.objects.filter(user=actor).exists():
                return PolicyDecision(False, "invalid_invitation", "Приглашение недействительно или вы уже состоите в команде.")
        elif not role:
            return PolicyDecision(False, "forbidden", "Нет доступа к этому рабочему пространству.")

        if operation in {Operation.REMOVE_MEMBER, Operation.CHANGE_MEMBER_ROLE} and (not account.agency_id or not isinstance(resource, AgencyMembership)):
            return PolicyDecision(False, "invalid_resource", "Укажите сотрудника этой команды.")
        if operation in {Operation.ARCHIVE_PROPERTY, Operation.RESTORE_PROPERTY, Operation.PUBLISH_LANDING, Operation.UNPUBLISH_LANDING, Operation.PUBLISH_TELEGRAM} and not isinstance(resource, Property):
            return PolicyDecision(False, "invalid_resource", "Укажите объект этого пространства.")

        if operation in {Operation.VIEW, Operation.CSV_EXPORT}:
            return PolicyDecision(True, "allowed", "Доступ к собственным данным сохранён.")
        if operation == Operation.MANAGE_BILLING:
            return PolicyDecision(role == "owner", "allowed" if role == "owner" else "forbidden", "Тариф меняет владелец пространства.")
        if operation == Operation.AI_GENERATE and ai_action == "audit":
            return PolicyDecision(True, "allowed", "Локальная проверка не расходует кредиты.", ai_cost=0)
        try:
            context = cls.context(account, now=now)
        except ValidationError:
            return PolicyDecision(False, "configuration_error", "Не удалось проверить тариф. Обратитесь в поддержку.")
        terms, limits = context.terms, context.terms["limits"]

        def decision(allowed, code="allowed", message="Действие доступно.", **values):
            return PolicyDecision(allowed, code, message, terms["plan_code"], context.state, **values)

        def quota(used, limit, required):
            if used is None:
                return decision(False, "usage_unknown", "Расход ещё не измерен; требуется сверка.", limit=limit, required=required)
            allowed = required == 0 or used + required <= limit
            return decision(allowed, "allowed" if allowed else "quota_exceeded", "Действие доступно." if allowed else "Лимит исчерпан. Освободите ресурс или увеличьте тариф.", limit=limit, used=used, required=required)

        if context.state == AccessState.SUSPENDED:
            return decision(False, "subscription_suspended", "Подписка приостановлена. Данные доступны для просмотра и выгрузки.")
        if operation != Operation.PUBLIC_LANDING and account.agency_id and operation != Operation.ACCEPT_INVITE and not terms["features"]["team"] and role != "owner":
            return decision(False, "team_read_only", "Совместное редактирование требует Бизнес-тарифа.")
        if operation in {Operation.ARCHIVE_PROPERTY, Operation.RESTORE_PROPERTY, Operation.INVITE_MEMBER} and account.agency_id and role not in {"owner", "manager"}:
            return decision(False, "forbidden", "Действие доступно владельцу или руководителю.")
        if operation in {Operation.REMOVE_MEMBER, Operation.CHANGE_MEMBER_ROLE} and role != "owner":
            return decision(False, "forbidden", "Сотрудниками управляет владелец.")
        if operation in {Operation.REMOVE_MEMBER, Operation.CHANGE_MEMBER_ROLE} and isinstance(resource, AgencyMembership) and resource.role == "owner":
            return decision(False, "forbidden", "Нельзя исключить владельца или изменить его роль.")
        if operation in {Operation.EDIT_CRM, Operation.ARCHIVE_PROPERTY, Operation.UNPUBLISH_LANDING, Operation.REMOVE_FILES, Operation.REMOVE_MEMBER}:
            return decision(True)
        if context.state == AccessState.GRACE and operation != Operation.PUBLIC_LANDING:
            return decision(False, "grace_no_new_resources", "Оплаченный срок закончился. Продлите тариф для новых ресурсов.")
        if operation == Operation.PUBLISH_TELEGRAM:
            if context.state == AccessState.RESTRICTED:
                return decision(False, "subscription_expired", "Оплаченный срок закончился. Продлите тариф для публикации в Telegram.")
            allowed = terms["features"].get("telegram_publications", True) if terms["schema"] == 1 else terms["features"]["telegram_publications"]
            return decision(allowed, "allowed" if allowed else "feature_not_available",
                "Публикация в Telegram доступна." if allowed else "Публикация в Telegram недоступна на вашем тарифе.")
        if operation == Operation.XML_EXPORT:
            return decision(terms["features"]["xml_export"], "allowed" if terms["features"]["xml_export"] else "feature_not_available", "XML-экспорт доступен в Стандарте и Бизнесе.")
        if operation == Operation.CREATE_AGENCY:
            allowed = bool(account.personal_user_id and role == "owner" and terms["features"]["team"] and not AgencyMembership.objects.filter(user=actor).exists())
            return decision(allowed, "allowed" if allowed else "feature_not_available", "Создание команды требует Бизнеса и отсутствия другого членства.")
        if operation in {Operation.INVITE_MEMBER, Operation.ACCEPT_INVITE, Operation.CHANGE_MEMBER_ROLE}:
            if not account.agency_id or not terms["features"]["team"]:
                return decision(False, "feature_not_available", "Командная работа требует Бизнес-тарифа.")
            if operation == Operation.INVITE_MEMBER and (invited_role not in {"agent", "manager"} or (role == "manager" and invited_role != "agent")):
                return decision(False, "forbidden", "Руководитель приглашает только риелторов.")
            if operation == Operation.CHANGE_MEMBER_ROLE:
                if invited_role not in {"agent", "manager"}:
                    return decision(False, "forbidden", "Назначить можно роль риелтора или руководителя.")
                return decision(True)
        if operation == Operation.AI_GENERATE:
            cost = terms["ai_costs"].get(ai_action)
            if cost is None:
                return decision(False, "invalid_ai_action", "Неизвестный сценарий ИИ.")
            result = quota(context.ai_used + context.ai_reserved, context.ai_allocated, cost * quantity)
            return PolicyDecision(**{**vars(result), "ai_cost": cost * quantity})

        usage = usage or collect_usage(account, now=now)
        if not isinstance(usage, UsageSnapshot):
            return decision(False, "invalid_usage", "Нужен серверный снимок расхода.")
        if operation in {Operation.CREATE_PROPERTY, Operation.RESTORE_PROPERTY}:
            required = quantity
            if operation == Operation.RESTORE_PROPERTY:
                if not isinstance(resource, Property):
                    return decision(False, "invalid_resource", "Укажите восстанавливаемый объект.")
                required = quantity if resource.status == "archived" else 0
            if operation == Operation.CREATE_PROPERTY:
                total_used = None if usage.total_properties is None else usage.total_properties + usage.total_properties_reserved
                total = quota(total_used, limits["total_properties"], quantity)
                if not total.allowed:
                    return total
            used = None if usage.active_properties is None else usage.active_properties + usage.active_properties_reserved
            return quota(used, limits["active_properties"], required)
        if operation in {Operation.PUBLISH_LANDING, Operation.PUBLIC_LANDING}:
            if not isinstance(resource, Property) or resource.status == "archived":
                return decision(False, "invalid_resource", "Архивный или неизвестный объект нельзя публиковать.")
            required = 0 if resource.landing_published else quantity
            used = None if usage.landings is None else usage.landings + usage.landings_reserved
            if operation == Operation.PUBLIC_LANDING and used is not None and used > limits["landings"]:
                return decision(False, "landing_selection_required", "Выберите страницы в пределах новой квоты.")
            return quota(used, limits["landings"], required)
        if operation in {Operation.INVITE_MEMBER, Operation.ACCEPT_INVITE}:
            used = None if usage.occupied_seats is None or usage.invited_seats is None else usage.occupied_seats + usage.invited_seats + usage.invited_seats_reserved
            # A valid acceptance replaces an already counted invitation reservation.
            if operation == Operation.ACCEPT_INVITE and used is not None and used > limits["seats"]:
                return decision(False, "quota_exceeded", "Команда превышает число доступных мест.", limit=limits["seats"], used=used)
            return quota(used, limits["seats"], 0 if operation == Operation.ACCEPT_INVITE else quantity)
        if operation == Operation.UPLOAD_FILES:
            used = None if usage.storage_bytes is None else usage.storage_bytes + usage.storage_reserved_bytes
            return quota(used, limits["storage_bytes"], quantity)
        return decision(False, "invalid_operation", "Для действия ещё не определено правило.")
