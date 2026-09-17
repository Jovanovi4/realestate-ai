import uuid

from django.conf import settings
from django.db import models


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
        ("reserved", "Забронирован"),
        ("sold", "Продан"),
        ("archived", "В архиве"),
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

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return f"{self.property.title} ({self.order})"
    
class AIContent(models.Model):

    property = models.ForeignKey(
        Property,
        on_delete=models.CASCADE,
        related_name="contents"
    )

    content_type = models.CharField(
        max_length=30
    )

    language = models.CharField(
        max_length=10,
        default="ru"
    )

    title = models.CharField(
        max_length=255,
        blank=True
    )

    content = models.TextField()

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

    def __str__(self):
        return self.display_name or self.user.get_full_name() or self.user.username


class Lead(models.Model):
    STATUS_CHOICES = [
        ("new", "Новая"),
        ("in_progress", "В работе"),
        ("viewing", "Показ назначен"),
        ("won", "Успешно"),
        ("lost", "Отказ"),
    ]

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="leads")
    name = models.CharField(max_length=150, verbose_name="Имя")
    phone = models.CharField(max_length=30, verbose_name="Телефон")
    message = models.TextField(blank=True, verbose_name="Комментарий")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="new", verbose_name="Статус")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка"
        verbose_name_plural = "Заявки"
