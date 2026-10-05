from copy import deepcopy
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from .catalog import POLICY_VERSION, default_ai_costs, validate_ai_costs, validate_snapshot


class ValidatedModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        # Uniqueness is enforced by the DB, including racing get_or_create calls.
        self.full_clean(validate_unique=False)
        return super().save(*args, **kwargs)

    def protect_terms(self, fields):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(*fields).first()
            if previous and any(previous[name] != getattr(self, name) for name in fields):
                raise ValidationError("Условия неизменяемы. Создайте новую версию/период.")


class BillingAccount(ValidatedModel):
    personal_user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="personal_billing_account", verbose_name="Личный владелец")
    agency = models.OneToOneField("properties.Agency", null=True, blank=True, on_delete=models.PROTECT, related_name="billing_account", verbose_name="Агентство")
    payer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="payable_billing_accounts", verbose_name="Плательщик")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Тарифный счёт"
        verbose_name_plural = "Тарифные счета"
        constraints = [models.CheckConstraint(
            condition=(models.Q(personal_user__isnull=False, agency__isnull=True) | models.Q(personal_user__isnull=True, agency__isnull=False)),
            name="billing_account_exactly_one_scope",
        )]

    @property
    def owner_id(self):
        return self.personal_user_id or self.agency.owner_id

    def __str__(self):
        return f"Агентство #{self.agency_id}" if self.agency_id else f"Личный счёт #{self.personal_user_id}"


class Plan(ValidatedModel):
    class Code(models.TextChoices):
        DEMO = "demo", "Демо"
        STANDARD = "standard", "Стандарт"
        BUSINESS = "business", "Бизнес"

    code = models.CharField(max_length=20, unique=True, choices=Code.choices)
    name = models.CharField(max_length=80, verbose_name="Название")

    class Meta:
        verbose_name = "Тариф"
        verbose_name_plural = "Тарифы"
        constraints = [models.CheckConstraint(condition=models.Q(code__in=["demo", "standard", "business"]), name="billing_known_plan_code")]

    def clean(self):
        self.protect_terms(("code",))

    def __str__(self):
        return self.name


class PlanVersion(ValidatedModel):
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveIntegerField(default=1)
    policy_version = models.CharField(max_length=30, default=POLICY_VERSION)
    active_properties_limit = models.PositiveIntegerField()
    total_properties_limit = models.PositiveIntegerField()
    landings_limit = models.PositiveIntegerField()
    seats_limit = models.PositiveIntegerField()
    storage_bytes_limit = models.PositiveBigIntegerField()
    ai_credits_limit = models.PositiveIntegerField()
    demo_ai_credits_limit = models.PositiveIntegerField(default=0)
    xml_export_enabled = models.BooleanField(default=False)
    team_enabled = models.BooleanField(default=False)
    telegram_publications_enabled = models.BooleanField(default=True)
    grace_days = models.PositiveSmallIntegerField(default=7)
    ai_costs = models.JSONField(default=default_ai_costs, validators=[validate_ai_costs])
    monthly_price_minor = models.PositiveBigIntegerField(null=True, blank=True, verbose_name="Цена в минимальных денежных единицах")
    currency = models.CharField(max_length=3, blank=True, verbose_name="Валюта продаж")
    available = models.BooleanField(default=True, verbose_name="Доступен для новых назначений")
    created_at = models.DateTimeField(auto_now_add=True)

    TERMS = (
        "plan_id", "version", "policy_version", "active_properties_limit", "total_properties_limit",
        "landings_limit", "seats_limit", "storage_bytes_limit", "ai_credits_limit", "demo_ai_credits_limit",
        "xml_export_enabled", "team_enabled", "telegram_publications_enabled", "grace_days", "ai_costs", "monthly_price_minor", "currency",
    )

    class Meta:
        verbose_name = "Версия тарифа"
        verbose_name_plural = "Версии тарифов"
        ordering = ["plan_id", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "version"], name="billing_unique_plan_version"),
            models.CheckConstraint(condition=models.Q(version__gte=1, seats_limit__gte=1), name="billing_version_and_seats_positive"),
            models.CheckConstraint(condition=models.Q(total_properties_limit__gte=models.F("active_properties_limit"), landings_limit__lte=models.F("active_properties_limit")), name="billing_consistent_property_limits"),
        ]

    @property
    def sale_ready(self):
        return self.plan.code != Plan.Code.DEMO and self.monthly_price_minor is not None and self.monthly_price_minor > 0 and bool(re.fullmatch(r"[A-Z]{3}", self.currency)) and self.available

    def snapshot(self):
        legacy = self.policy_version == "BP-0.1"
        features = {"xml_export": self.xml_export_enabled, "team": self.team_enabled}
        if not legacy:
            features["telegram_publications"] = self.telegram_publications_enabled
        return {
            "schema": 1 if legacy else 2, "policy_version": self.policy_version, "plan_code": self.plan.code, "version": self.version,
            "limits": {key: getattr(self, f"{key}_limit") for key in (
                "active_properties", "total_properties", "landings", "seats", "storage_bytes", "ai_credits", "demo_ai_credits",
            )},
            "features": features,
            "ai_costs": deepcopy(self.ai_costs), "grace_days": self.grace_days,
        }

    def clean(self):
        self.protect_terms(self.TERMS)
        if not self.plan_id or not Plan.objects.filter(pk=self.plan_id).exists():
            return  # Field validation reports the missing/invalid foreign key.
        if self.plan.code != Plan.Code.DEMO and self.monthly_price_minor == 0:
            raise ValidationError({"monthly_price_minor": "Платная цена должна быть положительной; до её утверждения оставьте NULL."})
        if self.monthly_price_minor is None:
            if self.currency:
                raise ValidationError({"currency": "Для неустановленной цены валюта должна быть пустой."})
        elif (self.monthly_price_minor != 0 or self.currency) and not re.fullmatch(r"[A-Z]{3}", self.currency or ""):
            raise ValidationError({"currency": "Укажите трёхбуквенный код валюты верхним регистром."})
        if self.plan_id and self.plan.code == Plan.Code.DEMO and self.monthly_price_minor not in (0, None):
            raise ValidationError("Демо — бесплатный тариф.")
        if self.policy_version == "BP-0.1" and not self.telegram_publications_enabled:
            raise ValidationError("Право Telegram требует новой версии политики.")
        validate_snapshot(self.snapshot())

    def __str__(self):
        return f"{self.plan.name} v{self.version}"


class Subscription(ValidatedModel):
    account = models.OneToOneField(BillingAccount, on_delete=models.PROTECT, related_name="subscription")
    anchor_at = models.DateTimeField(verbose_name="Исходная дата привязки, UTC")
    cancel_at_period_end = models.BooleanField(default=False)
    scheduled_plan_version = models.ForeignKey(PlanVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="scheduled_subscriptions")
    is_suspended = models.BooleanField(default=False)
    suspension_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Подписка"
        verbose_name_plural = "Подписки"

    def clean(self):
        self.protect_terms(("account_id",))
        if self.anchor_at and timezone.is_naive(self.anchor_at):
            raise ValidationError({"anchor_at": "Дата должна содержать часовой пояс."})
        if self.is_suspended and not (self.suspension_reason or "").strip():
            raise ValidationError({"suspension_reason": "Укажите причину приостановки."})

    def __str__(self):
        return f"Подписка счёта #{self.account_id}"


class BillingPeriod(ValidatedModel):
    class Source(models.TextChoices):
        PILOT = "pilot", "Пилотный доступ"
        MANUAL = "manual", "Назначение оператором"

    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name="periods")
    plan_version = models.ForeignKey(PlanVersion, on_delete=models.PROTECT, related_name="periods")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    terms_snapshot = models.JSONField(default=dict, editable=False, validators=[validate_snapshot])
    ai_credits_allocated = models.PositiveIntegerField(default=0, editable=False)
    ai_credits_used = models.PositiveIntegerField(default=0, editable=False)
    ai_credits_reserved = models.PositiveIntegerField(default=0, editable=False)
    request_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.PILOT)
    reason = models.CharField(max_length=255)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="granted_billing_periods")
    voided_at = models.DateTimeField(null=True, blank=True, editable=False)
    void_reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    TERMS = ("subscription_id", "plan_version_id", "starts_at", "ends_at", "terms_snapshot", "ai_credits_allocated", "request_key", "source", "reason", "granted_by_id")

    class Meta:
        verbose_name = "Расчётный период"
        verbose_name_plural = "Расчётные периоды"
        ordering = ["-starts_at", "-pk"]
        indexes = [models.Index(fields=["subscription", "starts_at", "ends_at"], name="billing_period_dates")]
        constraints = [
            models.CheckConstraint(condition=models.Q(ends_at__gt=models.F("starts_at")), name="billing_period_positive_duration"),
            models.CheckConstraint(condition=models.Q(ai_credits_allocated__gte=models.F("ai_credits_used") + models.F("ai_credits_reserved")), name="billing_period_credits_within_budget"),
            models.UniqueConstraint(fields=["subscription", "starts_at"], condition=models.Q(voided_at__isnull=True), name="billing_unique_live_period_start"),
        ]

    def clean(self):
        self.protect_terms(self.TERMS)
        if not (self.reason or "").strip():
            raise ValidationError({"reason": "Укажите основание доступа; это не фиктивный платёж."})
        if self.plan_version_id and PlanVersion.objects.filter(pk=self.plan_version_id, plan__code=Plan.Code.DEMO).exists():
            raise ValidationError("Демо не требует платного расчётного периода.")
        for name in ("starts_at", "ends_at"):
            value = getattr(self, name)
            if value and timezone.is_naive(value):
                raise ValidationError({name: "Дата должна содержать часовой пояс."})
        if self.starts_at and self.ends_at:
            if self.ends_at <= self.starts_at:
                raise ValidationError("Конец периода должен быть позже начала.")
            if not self.voided_at and self.subscription_id and type(self).objects.filter(
                subscription_id=self.subscription_id, voided_at__isnull=True,
                starts_at__lt=self.ends_at, ends_at__gt=self.starts_at,
            ).exclude(pk=self.pk).exists():
                raise ValidationError("Действующие периоды одной подписки не могут пересекаться.")
        if self.voided_at and not (self.void_reason or "").strip():
            raise ValidationError({"void_reason": "Укажите причину аннулирования."})
        previous = type(self).objects.filter(pk=self.pk).values("voided_at", "void_reason").first() if self.pk else None
        if previous and previous["voided_at"] and (previous["voided_at"] != self.voided_at or previous["void_reason"] != self.void_reason):
            raise ValidationError("Аннулирование периода необратимо; выдайте новый период.")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            for name, model in (("subscription", Subscription), ("plan_version", PlanVersion)):
                if not model.objects.filter(pk=getattr(self, f"{name}_id")).exists():
                    raise ValidationError({name: "Укажите существующую запись."})
            # All cooperating grant writers serialize on the same account in PostgreSQL.
            account_id = self.subscription.account_id
            BillingAccount.objects.select_for_update().get(pk=account_id)
            if not type(self).objects.filter(pk=self.pk).exists():
                expected = self.plan_version.snapshot()
                if self.terms_snapshot and self.terms_snapshot != expected:
                    raise ValidationError("Новый период должен сохранять условия выбранной версии.")
                self.terms_snapshot = expected
                self.ai_credits_allocated = expected["limits"]["ai_credits"]
            return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.plan_version} · {self.starts_at:%d.%m.%Y} — {self.ends_at:%d.%m.%Y}"


class DemoCreditGrant(ValidatedModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="demo_credit_grant")
    account = models.ForeignKey(BillingAccount, on_delete=models.PROTECT, related_name="demo_credit_grants")
    terms_snapshot = models.JSONField(default=dict, editable=False, validators=[validate_snapshot])
    credits_allocated = models.PositiveIntegerField(editable=False)
    credits_used = models.PositiveIntegerField(default=0, editable=False)
    credits_reserved = models.PositiveIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Разовый грант Демо"
        verbose_name_plural = "Разовые гранты Демо"
        constraints = [models.CheckConstraint(condition=models.Q(credits_allocated__gte=models.F("credits_used") + models.F("credits_reserved")), name="billing_demo_credits_within_budget")]

    def clean(self):
        self.protect_terms(("user_id", "terms_snapshot", "credits_allocated"))
        if self.account_id and BillingAccount.objects.filter(pk=self.account_id).exists() and self.account.personal_user_id != self.user_id:
            raise ValidationError("Грант принадлежит личному счёту этого пользователя, не агентству.")
        validate_snapshot(self.terms_snapshot)
        if self.terms_snapshot["plan_code"] != Plan.Code.DEMO or self.credits_allocated != self.terms_snapshot["limits"]["demo_ai_credits"]:
            raise ValidationError("Грант должен соответствовать сохранённым условиям Демо.")

    def __str__(self):
        return f"Грант пользователя #{self.user_id}"


class UsageOperation(ValidatedModel):
    """Observed work with an optional atomic credit reservation."""

    class Status(models.TextChoices):
        RUNNING = "running", "Выполняется"
        SUCCEEDED = "succeeded", "Завершена"
        FAILED = "failed", "Ошибка"
        UNKNOWN = "unknown", "Исход требует сверки"

    account = models.ForeignKey(BillingAccount, on_delete=models.PROTECT, related_name="usage_operations")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="usage_operations")
    request_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request_fingerprint = models.CharField(max_length=64, blank=True, editable=False)
    action = models.CharField(max_length=60)
    resource_type = models.CharField(max_length=80, blank=True)
    resource_id = models.CharField(max_length=40, blank=True)
    scope_snapshot = models.JSONField(default=dict, blank=True)
    terms_snapshot = models.JSONField(default=dict, blank=True)
    decision = models.JSONField(default=dict, blank=True)
    period = models.ForeignKey(BillingPeriod, null=True, blank=True, on_delete=models.PROTECT, related_name="usage_operations")
    demo_grant = models.ForeignKey(DemoCreditGrant, null=True, blank=True, on_delete=models.PROTECT, related_name="usage_operations")
    ai_action = models.CharField(max_length=30, blank=True)
    planned_credits = models.PositiveIntegerField(default=0)
    budget_reserved = models.BooleanField(default=False, editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RUNNING)
    result_ids = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    provider = models.CharField(max_length=30, blank=True)
    provider_model = models.CharField(max_length=100, blank=True)
    provider_response_id = models.CharField(max_length=128, blank=True)
    provider_status = models.CharField(max_length=30, blank=True)
    input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    output_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    cached_input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    total_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now, editable=False)
    finished_at = models.DateTimeField(null=True, blank=True, editable=False)

    IDENTITY = (
        "account_id", "actor_id", "request_key", "request_fingerprint", "action", "resource_type", "resource_id", "scope_snapshot",
        "terms_snapshot", "decision", "period_id", "demo_grant_id", "ai_action", "planned_credits", "budget_reserved", "started_at",
    )

    class Meta:
        verbose_name = "Операция наблюдательного учёта"
        verbose_name_plural = "Операции наблюдательного учёта"
        ordering = ["-started_at", "-pk"]
        indexes = [models.Index(fields=["account", "status", "started_at"], name="billing_usage_account_status")]
        constraints = [
            models.CheckConstraint(condition=(models.Q(status="running", finished_at__isnull=True) | models.Q(status__in=["succeeded", "failed", "unknown"], finished_at__isnull=False)), name="billing_usage_terminal_time"),
            models.CheckConstraint(condition=~(models.Q(period__isnull=False) & models.Q(demo_grant__isnull=False)), name="billing_usage_one_credit_bucket"),
        ]

    def clean(self):
        self.protect_terms(self.IDENTITY)
        if self.terms_snapshot:
            validate_snapshot(self.terms_snapshot)
        if self.period_id and self.period.subscription.account_id != self.account_id:
            raise ValidationError("Период относится к другому счёту.")
        if self.demo_grant_id and self.demo_grant.account_id != self.account_id and not self.pk:
            raise ValidationError("Грант относится к другому счёту.")
        previous = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if previous and previous.status in {self.Status.SUCCEEDED, self.Status.FAILED}:
            fields = [field.attname for field in self._meta.concrete_fields]
            if any(getattr(previous, name) != getattr(self, name) for name in fields):
                raise ValidationError("Завершённая операция неизменяема; используйте запись сверки.")


class UsageEntry(ValidatedModel):
    class Metric(models.TextChoices):
        ACTIVE_PROPERTIES = "active_properties", "Активные объекты"
        TOTAL_PROPERTIES = "total_properties", "Все карточки"
        LANDINGS = "landings", "Намерения публикации"
        OCCUPIED_SEATS = "occupied_seats", "Рабочие места"
        INVITED_SEATS = "invited_seats", "Резервы приглашений"
        STORAGE_BYTES = "storage_bytes", "Сохранённые байты"
        AI_CREDITS = "ai_credits", "Наблюдаемые ИИ-кредиты"

    operation = models.ForeignKey(UsageOperation, on_delete=models.PROTECT, related_name="entries")
    account = models.ForeignKey(BillingAccount, on_delete=models.PROTECT, related_name="usage_entries")
    metric = models.CharField(max_length=24, choices=Metric.choices)
    delta = models.BigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Запись расхода"
        verbose_name_plural = "Журнал расхода"
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["operation", "metric"], name="billing_usage_entry_once"),
            models.CheckConstraint(condition=~models.Q(metric="ai_credits") | models.Q(delta__gte=0), name="billing_observed_ai_nonnegative"),
        ]

    def clean(self):
        self.protect_terms(("operation_id", "account_id", "metric", "delta"))
        if self.operation_id and self.operation.account_id != self.account_id:
            raise ValidationError("Запись расхода относится к счёту операции.")
        if self.operation_id and self.operation.status != UsageOperation.Status.SUCCEEDED:
            raise ValidationError("Расход фиксируется только для успешно завершённой операции.")
        if self.metric == self.Metric.AI_CREDITS and self.operation_id and (
            not self.operation.ai_action or not self.operation.result_ids or self.delta != self.operation.planned_credits
        ):
            raise ValidationError("ИИ-кредиты должны соответствовать одной операции с сохранённым результатом.")


class ResourceReservation(ValidatedModel):
    """A short-lived claim counted alongside live workspace resources."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Зарезервировано"
        CONSUMED = "consumed", "Ресурс создан"
        RELEASED = "released", "Освобождено"

    account = models.ForeignKey(BillingAccount, on_delete=models.PROTECT, related_name="resource_reservations")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    request_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request_fingerprint = models.CharField(max_length=64, blank=True, editable=False)
    action = models.CharField(max_length=60)
    amounts = models.JSONField(default=dict)
    decision = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    result_id = models.CharField(max_length=40, blank=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    finished_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Резерв ресурса"
        verbose_name_plural = "Резервы ресурсов"
        indexes = [models.Index(fields=["account", "status", "created_at"], name="billing_reserve_account_status")]
        constraints = [models.CheckConstraint(
            condition=(models.Q(status="active", finished_at__isnull=True) |
                       models.Q(status__in=["consumed", "released"], finished_at__isnull=False)),
            name="billing_reserve_terminal_time",
        )]

    def clean(self):
        allowed = {"active_properties", "total_properties", "landings", "invited_seats", "storage_bytes"}
        if not isinstance(self.amounts, dict) or not self.amounts or any(
            key not in allowed or type(value) is not int or value <= 0 for key, value in self.amounts.items()
        ):
            raise ValidationError("Резерв должен содержать положительный объём известного ресурса.")
        self.protect_terms(("account_id", "actor_id", "request_key", "request_fingerprint", "action", "amounts", "decision", "created_at"))
        previous = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if previous and previous.status != self.Status.ACTIVE and (
            previous.status != self.status or previous.result_id != self.result_id or previous.finished_at != self.finished_at
        ):
            raise ValidationError("Завершённый резерв неизменяем.")


class StoredFile(ValidatedModel):
    class State(models.TextChoices):
        PRESENT = "present", "Файл сохранён"
        MISSING = "missing", "Подтверждено отсутствие"
        UNKNOWN = "unknown", "Размер неизвестен"
        TRANSFERRED = "transferred", "Передан другому пространству"

    account = models.ForeignKey(BillingAccount, on_delete=models.PROTECT, related_name="stored_files")
    name = models.CharField(max_length=1024)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.UNKNOWN)
    issue_code = models.CharField(max_length=40, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Учтённый файл"
        verbose_name_plural = "Учтённые файлы"
        constraints = [models.UniqueConstraint(fields=["account", "name"], name="billing_storage_name_per_account")]


class FileReference(ValidatedModel):
    file = models.ForeignKey(StoredFile, on_delete=models.PROTECT, related_name="references")
    resource_type = models.CharField(max_length=80)
    resource_id = models.CharField(max_length=40)
    field_name = models.CharField(max_length=40)

    class Meta:
        verbose_name = "Ссылка на учтённый файл"
        verbose_name_plural = "Ссылки на учтённые файлы"
        constraints = [models.UniqueConstraint(fields=["resource_type", "resource_id", "field_name"], name="billing_storage_reference_once")]


class AccountInventory(ValidatedModel):
    account = models.OneToOneField(BillingAccount, on_delete=models.PROTECT, related_name="inventory")
    references_complete = models.BooleanField(default=False)
    files_complete = models.BooleanField(default=False)
    checked_at = models.DateTimeField(null=True, blank=True)
    last_issue_count = models.PositiveIntegerField(default=0)
    accounting_started_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        verbose_name = "Сверка ресурсов счёта"
        verbose_name_plural = "Сверки ресурсов счетов"


class UsageResult(ValidatedModel):
    operation = models.ForeignKey(UsageOperation, on_delete=models.PROTECT, related_name="saved_results")
    content = models.OneToOneField("properties.AIContent", on_delete=models.PROTECT, related_name="usage_result")

    class Meta:
        verbose_name = "Сохранённый результат операции"
        verbose_name_plural = "Сохранённые результаты операций"

    def clean(self):
        self.protect_terms(("operation_id", "content_id"))
        if self.operation_id and self.content_id and (
            self.operation.resource_type != "properties.property" or self.operation.resource_id != str(self.content.property_id)
        ):
            raise ValidationError("Результат должен относиться к объекту исходной операции.")
