import uuid

from django.conf import settings
from django.db import models


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

    landing_title = models.CharField(max_length=255, blank=True, verbose_name="Заголовок лендинга")
    landing_subtitle = models.TextField(blank=True, verbose_name="Подзаголовок лендинга")
    landing_about_title = models.CharField(max_length=255, blank=True, verbose_name="Заголовок блока «Об объекте»")
    landing_contact_title = models.CharField(max_length=255, blank=True, verbose_name="Заголовок блока заявки")
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
        if not self.landing_slug:
            self.landing_slug = uuid.uuid4().hex[:12]
        super().save(*args, **kwargs)

    @property
    def primary_image(self):
        return self.images.filter(is_primary=True).first() or self.images.first()

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
        ("profile_about", "Текст «О риелторе»"),
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
    benefits = models.JSONField(default=default_realtor_benefits, verbose_name="Преимущества риелтора")
    about = models.TextField(
        blank=True,
        verbose_name="О риелторе",
        help_text="Коротко расскажите об опыте, подходе к работе или преимуществах. Этот текст появится на лендингах.",
    )

    def __str__(self):
        return self.display_name or self.user.get_full_name() or self.user.username


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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [models.UniqueConstraint(fields=["owner", "phone"], name="unique_client_phone_per_owner")]
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"

    def __str__(self):
        return f"{self.name} · {self.phone}"


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
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="reminders")
    text = models.CharField(max_length=255, verbose_name="Напоминание")
    due_at = models.DateTimeField(verbose_name="Когда напомнить")
    is_done = models.BooleanField(default=False, verbose_name="Выполнено")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["is_done", "due_at"]
        verbose_name = "Напоминание"
        verbose_name_plural = "Напоминания"


class Lead(models.Model):
    STATUS_CHOICES = [
        ("new", "Новая"),
        ("read", "Прочитана"),
        ("archived", "Архив"),
    ]

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="leads")
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="leads")
    name = models.CharField(max_length=150, verbose_name="Имя")
    phone = models.CharField(max_length=30, verbose_name="Телефон")
    message = models.TextField(blank=True, verbose_name="Комментарий")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new", verbose_name="Статус обработки")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка"
        verbose_name_plural = "Заявки"
