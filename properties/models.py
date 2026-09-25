import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def default_landing_block_order():
    return ["hero", "facts", "description", "gallery", "mortgage", "trust", "contact"]


def default_landing_enabled_blocks():
    return {key: True for key in ["hero", "facts", "description", "gallery", "mortgage", "trust", "contact"]}


def default_realtor_benefits():
    return [
        {"icon": "◆", "title": "Знание рынка", "description": "Помогаем разобраться в деталях объекта и условиях сделки."},
        {"icon": "◌", "title": "На связи", "description": "Отвечаем на вопросы и организуем просмотр в удобное время."},
        {"icon": "✓", "title": "Внимание к сделке", "description": "Сопровождаем процесс бережно и прозрачно."},
    ]


def same_workspace(left, right):
    """Private records belong to one user; team records belong to one agency."""
    if left.agency_id or right.agency_id:
        return bool(left.agency_id and left.agency_id == right.agency_id)
    return left.owner_id == right.owner_id


class Property(models.Model):
    PROPERTY_TYPES = [
        ("apartment", "Квартира"),
        ("house", "Дом"),
        ("cottage", "Дача"),
        ("land", "Участок"),
        ("villa", "Вилла"),
        ("office", "Офис"),
        ("commercial", "Коммерческая"),
    ]

    STATUS_CHOICES = [
        ("draft", "Черновик"),
        ("published", "Опубликован"),
        ("showing", "На показах"),
        ("reserved", "Забронирован"),
        ("sold", "Продан"),
        ("archived", "В архиве"),
        ("available", "Свободен"),
        ("rented", "Сдан"),
    ]

    DEAL_TYPE_CHOICES = [
        ("sale", "Продажа"),
        ("rent", "Долгосрочная аренда"),
    ]

    UTILITIES_CHOICES = [
        ("included", "Включены в стоимость"),
        ("separate", "Оплачиваются отдельно"),
        ("meters", "Отдельно по счётчикам"),
    ]

    AVITO_OPERATION_CHOICES = [
        ("sell", "Продам"),
        ("rent", "Сдам"),
    ]

    CURRENCY_CHOICES = [
        ("EUR", "€ Евро"),
        ("USD", "$ Доллар США"),
        ("RUB", "₽ Рубль"),
    ]

    CONDITION_CHOICES = [
        ("new", "Новостройка"),
        ("excellent", "Отличное"),
        ("good", "Хорошее"),
        ("needs_repair", "Требует ремонта"),
    ]

    LANDING_TEMPLATES = [
        ("classic", "Классический"),
        ("modern", "Современный"),
        ("premium", "Премиальный"),
    ]

    LANDING_ACCENT_CHOICES = [
        ("template", "По шаблону"),
        ("graphite", "Графитовый"),
        ("blue", "Синий"),
        ("emerald", "Изумрудный"),
        ("terracotta", "Терракотовый"),
    ]

    LANDING_BUTTON_STYLE_CHOICES = [
        ("template", "По шаблону"),
        ("rounded", "Скруглённые"),
        ("strict", "Строгие"),
    ]

    LANDING_HERO_LAYOUT_CHOICES = [
        ("split", "Фото рядом с текстом"),
        ("background", "Фото на фоне"),
    ]

    LANDING_GALLERY_STYLE_CHOICES = [
        ("large", "Крупное фото"),
        ("grid", "Сетка фото"),
    ]

    LANDING_BLOCKS = [
        ("hero", "Первый экран"),
        ("facts", "Основные характеристики"),
        ("description", "Описание объекта"),
        ("gallery", "Фотогалерея"),
        ("mortgage", "Калькулятор ипотеки"),
        ("trust", "Преимущества риелтора"),
        ("contact", "Заявка и контакты"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="properties",
        null=True,
        blank=True,
        verbose_name="Риелтор",
    )
    agency = models.ForeignKey("Agency", on_delete=models.PROTECT, null=True, blank=True, related_name="properties")

    title = models.CharField(
        max_length=255,
        verbose_name="Название"
    )

    marketing_headline = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Заголовок объявления",
    )

    property_type = models.CharField(
        max_length=20,
        choices=PROPERTY_TYPES,
        default="apartment",
        verbose_name="Тип недвижимости"
    )

    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Цена"
    )

    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default="EUR",
        verbose_name="Валюта",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft",
        verbose_name="Статус",
    )

    avito_export = models.BooleanField(default=False, verbose_name="Включить в экспорт Avito")
    cian_export = models.BooleanField(default=False, verbose_name="Включить в экспорт ЦИАН")
    deal_type = models.CharField(max_length=10, choices=DEAL_TYPE_CHOICES, default="sale", verbose_name="Тип сделки")
    avito_operation = models.CharField(
        max_length=10,
        choices=AVITO_OPERATION_CHOICES,
        default="sell",
        verbose_name="Тип объявления Avito",
    )

    address = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Адрес"
    )

    area = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Площадь"
    )

    rooms = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Комнаты"
    )

    bathrooms = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Санузлы",
    )

    floor = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Этаж",
    )

    floors_total = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Этажей в здании",
    )

    land_area = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Площадь участка, м²",
    )

    year_built = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="Год постройки",
    )

    condition = models.CharField(
        max_length=20,
        choices=CONDITION_CHOICES,
        blank=True,
        verbose_name="Состояние",
    )

    amenities = models.TextField(
        blank=True,
        verbose_name="Особенности и удобства",
        help_text="Например: парковка, бассейн, терраса, охрана, вид на море.",
    )

    description = models.TextField(
        blank=True,
        verbose_name="Описание"
    )

    short_description = models.TextField(
        blank=True,
        verbose_name="Короткое описание",
    )

    rent_deposit = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Залог")
    rent_commission = models.PositiveIntegerField(null=True, blank=True, verbose_name="Комиссия, %")
    utilities_terms = models.CharField(max_length=20, choices=UTILITIES_CHOICES, blank=True, verbose_name="Коммунальные платежи")
    min_lease_months = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name="Минимальный срок аренды, мес.")
    available_from = models.DateField(null=True, blank=True, verbose_name="Свободен с")
    furnished = models.BooleanField(default=False, verbose_name="Есть мебель")
    pets_allowed = models.BooleanField(default=False, verbose_name="Можно с животными")
    children_allowed = models.BooleanField(default=False, verbose_name="Можно с детьми")

    landing_title = models.CharField(max_length=255, blank=True, verbose_name="Заголовок лендинга")
    landing_subtitle = models.TextField(blank=True, verbose_name="Подзаголовок лендинга")
    landing_about_title = models.CharField(max_length=255, blank=True, verbose_name="Заголовок блока «Об объекте»")
    landing_contact_title = models.CharField(max_length=255, blank=True, verbose_name="Заголовок блока заявки")
    landing_trust_about = models.TextField(blank=True, verbose_name="Текст блока преимуществ")
    landing_benefits = models.JSONField(default=default_realtor_benefits, verbose_name="Карточки преимуществ лендинга")
    landing_logo = models.ImageField(upload_to="landing_logos/", blank=True, verbose_name="Логотип в шапке лендинга")
    seo_title = models.CharField(max_length=255, blank=True, verbose_name="SEO-заголовок")
    seo_description = models.CharField(max_length=300, blank=True, verbose_name="SEO-описание")
    landing_block_order = models.JSONField(
        default=default_landing_block_order,
        verbose_name="Порядок блоков лендинга",
    )
    landing_enabled_blocks = models.JSONField(default=default_landing_enabled_blocks, verbose_name="Включённые блоки лендинга")

    landing_slug = models.SlugField(max_length=64, unique=True, null=True, blank=True)
    landing_published = models.BooleanField(default=False, verbose_name="Лендинг опубликован")
    landing_template = models.CharField(
        max_length=30,
        choices=LANDING_TEMPLATES,
        default="classic",
        verbose_name="Шаблон лендинга",
    )
    landing_accent = models.CharField(
        max_length=20,
        choices=LANDING_ACCENT_CHOICES,
        default="template",
        verbose_name="Акцентный цвет",
    )
    landing_button_style = models.CharField(
        max_length=20,
        choices=LANDING_BUTTON_STYLE_CHOICES,
        default="template",
        verbose_name="Форма кнопок",
    )
    landing_hero_layout = models.CharField(
        max_length=20,
        choices=LANDING_HERO_LAYOUT_CHOICES,
        default="split",
        verbose_name="Вид первого экрана",
    )
    landing_gallery_style = models.CharField(
        max_length=20,
        choices=LANDING_GALLERY_STYLE_CHOICES,
        default="large",
        verbose_name="Вид галереи",
    )
    is_demo = models.BooleanField(default=False, verbose_name="Демонстрационный объект")

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Объект"
        verbose_name_plural = "Объекты"

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.agency_id and (not self.owner_id or not AgencyMembership.objects.filter(agency_id=self.agency_id, user_id=self.owner_id).exists()):
            raise ValidationError({"owner": "Владелец записи не состоит в агентстве."})
        if not self.landing_slug:
            self.landing_slug = uuid.uuid4().hex[:12]
        super().save(*args, **kwargs)

    @property
    def primary_image(self):
        return self.images.filter(is_primary=True).first() or self.images.first()

    @property
    def price_period_label(self):
        return "в месяц" if self.deal_type == "rent" else ""

    def ensure_primary_image(self):
        if not self.images.filter(is_primary=True).exists():
            image = self.images.order_by("order", "id").first()
            if image:
                image.is_primary = True
                image.save(update_fields=["is_primary"])
    
class PropertyImage(models.Model):

    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="images"
    )

    image = models.ImageField(
        upload_to="properties/"
    )

    order = models.PositiveIntegerField(
        default=0
    )

    is_primary = models.BooleanField(default=False, verbose_name="Главная фотография")

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.property.title} ({self.order})"
    
class AIContent(models.Model):

    CONTENT_TYPES = [
        ("headline", "Заголовок объявления"),
        ("short_description", "Короткое описание"),
        ("full_description", "Полное описание"),
        ("description", "Описание (архив)"),
        ("landing_headline", "Первый экран лендинга"),
        ("landing_subtitle", "Подзаголовок лендинга"),
        ("landing_about", "Текст «Об объекте»"),
        ("seo_title", "SEO-заголовок"),
        ("seo_description", "SEO-описание"),
        ("benefits", "Преимущества для лендинга"),
        ("cta", "Призыв к действию"),
        ("audit", "Проверка готовности"),
        ("lead_reply", "Ответ клиенту"),
    ]

    APPLY_TARGET_CHOICES = [
        ("", "Не применять автоматически"),
        ("marketing_headline", "Заголовок объявления"),
        ("short_description", "Короткое описание"),
        ("description", "Полное описание"),
        ("landing_title", "Заголовок лендинга"),
        ("landing_subtitle", "Подзаголовок лендинга"),
        ("landing_about_title", "Заголовок блока «Об объекте»"),
        ("landing_contact_title", "Заголовок блока заявки"),
        ("seo_title", "SEO-заголовок"),
        ("seo_description", "SEO-описание"),
        ("landing_trust_about", "Текст блока преимуществ"),
    ]

    TONE_CHOICES = [
        ("business", "Деловой"),
        ("premium", "Премиальный"),
        ("concise", "Лаконичный"),
        ("emotional", "Эмоциональный"),
    ]

    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="contents"
    )
    lead = models.ForeignKey("Lead", on_delete=models.SET_NULL, null=True, blank=True, related_name="ai_contents")

    content_type = models.CharField(
        max_length=30,
        choices=CONTENT_TYPES,
    )

    tone = models.CharField(
        max_length=20,
        choices=TONE_CHOICES,
        default="business",
        verbose_name="Тон",
    )

    provider = models.CharField(max_length=30, blank=True, verbose_name="Провайдер")
    model = models.CharField(max_length=100, blank=True, verbose_name="Модель")
    duration_ms = models.PositiveIntegerField(default=0, verbose_name="Время генерации, мс")
    prompt = models.TextField(blank=True, verbose_name="Запрос к ИИ")

    language = models.CharField(
        max_length=10,
        default="ru"
    )

    title = models.CharField(
        max_length=255,
        blank=True
    )
    apply_target = models.CharField(max_length=40, choices=APPLY_TARGET_CHOICES, blank=True, verbose_name="Куда применить")

    content = models.TextField()
    edited_content = models.TextField(blank=True, verbose_name="Отредактированный текст")
    is_applied = models.BooleanField(default=False, verbose_name="Применён к объекту")
    applied_at = models.DateTimeField(null=True, blank=True, verbose_name="Применён")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "ИИ-контент"
        verbose_name_plural = "ИИ-контент"


class RealtorProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="realtor_profile")
    display_name = models.CharField(max_length=150, blank=True, verbose_name="Имя")
    photo = models.ImageField(upload_to="realtors/", blank=True, verbose_name="Фото")
    phone = models.CharField(max_length=30, blank=True, verbose_name="Телефон")
    telegram_username = models.CharField(max_length=100, blank=True, verbose_name="Telegram без @")
    email = models.EmailField(blank=True, verbose_name="Email")
    telegram_chat_id = models.CharField(
        max_length=64,
        blank=True,
        verbose_name="Telegram chat ID для уведомлений",
        help_text="Приватный ID чата с ботом. Он не показывается посетителям лендингов.",
    )
    benefits = models.JSONField(default=default_realtor_benefits, verbose_name="Преимущества риелтора")
    about = models.TextField(
        blank=True,
        verbose_name="О риелторе",
        help_text="Коротко расскажите об опыте, подходе к работе или преимуществах. Этот текст появится на лендингах.",
    )
    demo_data_created = models.BooleanField(default=False, verbose_name="Демо-данные созданы")
    onboarding_started = models.BooleanField(default=False, verbose_name="Онбординг начат")
    onboarding_dismissed = models.BooleanField(default=False, verbose_name="Онбординг скрыт")

    def __str__(self):
        return self.display_name or self.user.get_full_name() or self.user.username


class Agency(models.Model):
    name = models.CharField(max_length=150, verbose_name="Название агентства")
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_agency")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class AgencyMembership(models.Model):
    ROLE_CHOICES = [("owner", "Владелец"), ("manager", "Руководитель"), ("agent", "Риелтор")]
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="memberships")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agency_membership")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="agent")
    joined_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.role == "owner" and self.agency_id and self.user_id != self.agency.owner_id:
            raise ValidationError({"role": "Владельцем может быть только создатель агентства."})
        if self.agency_id and self.user_id == self.agency.owner_id and self.role != "owner":
            raise ValidationError({"role": "Нельзя изменить роль владельца агентства."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user} · {self.agency}"


class AgencyInvitation(models.Model):
    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="invitations")
    code = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    role = models.CharField(max_length=20, choices=AgencyMembership.ROLE_CHOICES[1:], default="agent")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="accepted_agency_invitations")

    def __str__(self):
        return f"{self.agency} · {self.role}"


class Client(models.Model):
    STATUS_CHOICES = [
        ("new", "Новый"),
        ("in_progress", "В работе"),
        ("viewing", "Показ"),
        ("negotiation", "Переговоры"),
        ("won", "Сделка"),
        ("lost", "Отказ"),
    ]
    SOURCE_CHOICES = [
        ("landing", "Лендинг"),
        ("avito", "Avito"),
        ("recommendation", "Рекомендация"),
        ("manual", "Добавлен вручную"),
        ("other", "Другое"),
    ]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="clients")
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, null=True, blank=True, related_name="clients")
    name = models.CharField(max_length=150, verbose_name="Имя")
    phone = models.CharField(max_length=30, verbose_name="Телефон")
    preferred_contact_time = models.CharField(max_length=120, blank=True, verbose_name="Удобное время связи")
    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Бюджет")
    preferred_area = models.CharField(max_length=255, blank=True, verbose_name="Интересующий район")
    source = models.CharField(max_length=30, choices=SOURCE_CHOICES, default="landing", verbose_name="Источник")
    notes = models.TextField(blank=True, verbose_name="Комментарий риелтора")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new", verbose_name="Этап воронки")
    outcome_reason = models.CharField(max_length=255, blank=True, verbose_name="Причина результата")
    first_contacted_at = models.DateTimeField(null=True, blank=True, verbose_name="Первый контакт")
    is_demo = models.BooleanField(default=False, verbose_name="Демонстрационный клиент")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "phone"], condition=models.Q(agency__isnull=True), name="unique_client_phone_per_owner"),
            models.UniqueConstraint(fields=["agency", "phone"], condition=models.Q(agency__isnull=False), name="unique_client_phone_per_agency"),
        ]
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"

    def __str__(self):
        return f"{self.name} · {self.phone}"

    def save(self, *args, **kwargs):
        if self.agency_id and not AgencyMembership.objects.filter(agency_id=self.agency_id, user_id=self.owner_id).exists():
            raise ValidationError({"owner": "Владелец записи не состоит в агентстве."})
        super().save(*args, **kwargs)


class ClientInteraction(models.Model):
    TYPE_CHOICES = [
        ("call", "Звонок"),
        ("message", "Сообщение"),
        ("viewing", "Показ"),
        ("note", "Заметка"),
        ("status", "Изменение статуса"),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="interactions")
    lead = models.ForeignKey("Lead", on_delete=models.SET_NULL, null=True, blank=True, related_name="interactions")
    interaction_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="note", verbose_name="Тип")
    text = models.TextField(blank=True, verbose_name="Комментарий")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Взаимодействие с клиентом"
        verbose_name_plural = "История взаимодействий"


class ClientReminder(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="crm_tasks")
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, null=True, blank=True, related_name="tasks")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, null=True, blank=True, related_name="reminders", verbose_name="Клиент")
    deal = models.ForeignKey("Deal", on_delete=models.CASCADE, null=True, blank=True, related_name="tasks", verbose_name="Сделка")
    text = models.CharField(max_length=255, verbose_name="Задача")
    due_at = models.DateTimeField(null=True, blank=True, verbose_name="Срок выполнения")
    is_done = models.BooleanField(default=False, verbose_name="Выполнено")
    completed_at = models.DateTimeField(null=True, blank=True)
    overdue_notified_at = models.DateTimeField(null=True, blank=True, verbose_name="Уведомление о просрочке отправлено")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["is_done", "due_at"]
        verbose_name = "Задача"
        verbose_name_plural = "Задачи"

    def clean(self):
        errors = {}
        if self.agency_id and self.owner_id and not AgencyMembership.objects.filter(agency_id=self.agency_id, user_id=self.owner_id).exists():
            errors["owner"] = "Ответственный не состоит в агентстве."
        if self.client_id and self.owner_id and not same_workspace(self, self.client):
            errors["client"] = "Клиент находится в другом рабочем пространстве."
        if self.deal_id:
            if self.owner_id and not same_workspace(self, self.deal):
                errors["deal"] = "Сделка находится в другом рабочем пространстве."
            if self.client_id and self.client_id != self.deal.client_id:
                errors["client"] = "Клиент должен совпадать с клиентом сделки."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.deal_id and not self.client_id:
            self.client = self.deal.client
        if not self.agency_id and (self.deal_id or self.client_id):
            self.agency_id = self.deal.agency_id if self.deal_id else self.client.agency_id
        if not self.owner_id:
            if self.deal_id:
                self.owner = self.deal.owner
            elif self.client_id:
                self.owner = self.client.owner
        update_fields = kwargs.get("update_fields")
        if self.pk and (update_fields is None or "due_at" in update_fields):
            previous_due_at = type(self).objects.filter(pk=self.pk).values_list("due_at", flat=True).first()
            if previous_due_at != self.due_at:
                self.overdue_notified_at = None
                if update_fields is not None:
                    kwargs["update_fields"] = set(update_fields) | {"overdue_notified_at"}
        self.full_clean()
        super().save(*args, **kwargs)


class Lead(models.Model):
    CONTACT_PURPOSE_CHOICES = [
        ("viewing", "Записаться на просмотр"),
        ("presentation", "Получить презентацию"),
        ("rent_terms", "Уточнить условия аренды"),
    ]
    INTEREST_CHOICES = [
        ("buy", "Покупка"),
        ("long_rent", "Долгосрочная аренда"),
    ]
    STATUS_CHOICES = [
        ("new", "Новая"),
        ("read", "Прочитана"),
        ("archived", "Архив"),
    ]

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="leads")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_leads", verbose_name="Ответственный")
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    name = models.CharField(max_length=150, verbose_name="Имя")
    phone = models.CharField(max_length=30, verbose_name="Телефон")
    contact_purpose = models.CharField(
        max_length=20,
        choices=CONTACT_PURPOSE_CHOICES,
        default="viewing",
        verbose_name="Повод обращения",
    )
    message = models.TextField(blank=True, verbose_name="Комментарий")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new", verbose_name="Статус обработки")
    interest_type = models.CharField(max_length=20, choices=INTEREST_CHOICES, default="buy", verbose_name="Интерес клиента")
    personal_data_consent_at = models.DateTimeField(null=True, blank=True, verbose_name="Согласие на обработку данных получено")
    personal_data_consent_version = models.CharField(max_length=32, blank=True, verbose_name="Версия согласия")
    is_demo = models.BooleanField(default=False, verbose_name="Демонстрационная заявка")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка"
        verbose_name_plural = "Заявки"

    def clean(self):
        errors = {}
        if self.client_id and self.property_id and not same_workspace(self.client, self.property):
            errors["client"] = "Клиент находится в другом рабочем пространстве."
        if self.assigned_to_id and self.property_id:
            if self.property.agency_id:
                allowed = AgencyMembership.objects.filter(agency_id=self.property.agency_id, user_id=self.assigned_to_id).exists()
            else:
                allowed = self.assigned_to_id == self.property.owner_id
            if not allowed:
                errors["assigned_to"] = "Ответственный не имеет доступа к заявке."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.assigned_to_id and self.property_id:
            self.assigned_to_id = self.property.owner_id
        self.full_clean()
        super().save(*args, **kwargs)


class Deal(models.Model):
    STAGE_CHOICES = [
        ("new", "Новая"),
        ("in_progress", "В работе"),
        ("viewing", "Показ"),
        ("negotiation", "Переговоры"),
        ("reserved", "Бронь"),
        ("won", "Завершена"),
        ("lost", "Отказ"),
    ]
    ACTIVE_STAGES = ("new", "in_progress", "viewing", "negotiation", "reserved")
    CLOSED_STAGES = ("won", "lost")

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="deals")
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, null=True, blank=True, related_name="deals")
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_deals",
        verbose_name="Ответственный",
    )
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="deals", verbose_name="Клиент")
    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="deals", verbose_name="Объект")
    lead = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name="deals", verbose_name="Исходная заявка")
    stage = models.CharField(max_length=20, choices=STAGE_CHOICES, default="new", verbose_name="Этап сделки")
    outcome_note = models.TextField(blank=True, verbose_name="Итог или причина отказа")
    expected_commission = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Ожидаемая комиссия",
    )
    is_demo = models.BooleanField(default=False, verbose_name="Демонстрационная сделка")
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата завершения")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "property"],
                condition=models.Q(stage__in=["new", "in_progress", "viewing", "negotiation", "reserved"]),
                name="unique_active_deal_per_client_property",
            ),
        ]
        indexes = [models.Index(fields=["owner", "stage"], name="deal_owner_stage_idx")]
        verbose_name = "Сделка"
        verbose_name_plural = "Сделки"

    def clean(self):
        errors = {}
        if self.agency_id and self.owner_id and not AgencyMembership.objects.filter(agency_id=self.agency_id, user_id=self.owner_id).exists():
            errors["owner"] = "Создатель не состоит в агентстве."
        if self.client_id and self.owner_id and not same_workspace(self, self.client):
            errors["client"] = "Клиент находится в другом рабочем пространстве."
        if self.property_id and self.owner_id and not same_workspace(self, self.property):
            errors["property"] = "Объект находится в другом рабочем пространстве."
        if self.responsible_id:
            if self.agency_id:
                allowed = AgencyMembership.objects.filter(agency_id=self.agency_id, user_id=self.responsible_id).exists()
            else:
                allowed = self.responsible_id == self.owner_id
            if not allowed:
                errors["responsible"] = "Ответственный не имеет доступа к сделке."
        if self.lead_id and self.client_id and self.property_id:
            if self.lead.client_id != self.client_id or self.lead.property_id != self.property_id:
                errors["lead"] = "Заявка должна относиться к выбранным клиенту и объекту."
        if self.expected_commission is not None and self.expected_commission < 0:
            errors["expected_commission"] = "Комиссия не может быть отрицательной."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.agency_id and self.property_id:
            self.agency_id = self.property.agency_id
        if self.stage in self.CLOSED_STAGES:
            self.closed_at = self.closed_at or timezone.now()
        else:
            self.closed_at = None
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"closed_at"}
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.client} · {self.property}"


class Showing(models.Model):
    STATUS_CHOICES = [
        ("planned", "Запланирован"),
        ("completed", "Проведён"),
        ("cancelled", "Отменён"),
    ]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="showings")
    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, null=True, blank=True, related_name="showings")
    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, related_name="showings", verbose_name="Сделка")
    starts_at = models.DateTimeField(verbose_name="Начало показа")
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name="Окончание показа")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="planned", verbose_name="Статус")
    location = models.CharField(max_length=255, blank=True, verbose_name="Место встречи")
    outcome_note = models.TextField(blank=True, verbose_name="Итог показа")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["owner", "starts_at"], name="showing_owner_start_idx")]
        verbose_name = "Показ"
        verbose_name_plural = "Показы"

    def clean(self):
        errors = {}
        if self.agency_id and self.owner_id and not AgencyMembership.objects.filter(agency_id=self.agency_id, user_id=self.owner_id).exists():
            errors["owner"] = "Ответственный не состоит в агентстве."
        if self.owner_id and self.deal_id and not same_workspace(self, self.deal):
            errors["deal"] = "Сделка находится в другом рабочем пространстве."
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            errors["ends_at"] = "Окончание должно быть позже начала."
        if self.status == "completed" and not self.outcome_note.strip():
            errors["outcome_note"] = "Запишите результат показа."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self.agency_id and self.deal_id:
            self.agency_id = self.deal.agency_id
        if not self.owner_id and self.deal_id:
            self.owner = self.deal.owner
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.deal} · {self.starts_at:%d.%m.%Y %H:%M}"


class UserLegalAcceptance(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="legal_acceptance")
    terms_accepted_at = models.DateTimeField(verbose_name="Условия сервиса приняты")
    terms_version = models.CharField(max_length=32, verbose_name="Версия условий")
    personal_data_consent_at = models.DateTimeField(verbose_name="Согласие на обработку данных получено")
    personal_data_consent_version = models.CharField(max_length=32, verbose_name="Версия согласия")

    class Meta:
        verbose_name = "Принятие юридических документов"
        verbose_name_plural = "Принятие юридических документов"


class AuditEvent(models.Model):
    ACTION_CHOICES = [("created", "Создано"), ("updated", "Изменено"), ("deleted", "Удалено")]
    agency = models.ForeignKey(Agency, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_events")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="personal_audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_actions")
    model_name = models.CharField(max_length=60)
    object_pk = models.CharField(max_length=40)
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    changed_fields = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["agency", "created_at"], name="audit_agency_time_idx")]

    def __str__(self):
        return f"{self.get_action_display()}: {self.model_name} #{self.object_pk}"
