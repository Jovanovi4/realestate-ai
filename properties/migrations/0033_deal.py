from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("properties", "0032_property_landing_logo"),
    ]

    operations = [
        migrations.CreateModel(
            name="Deal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stage", models.CharField(choices=[("new", "Новая"), ("in_progress", "В работе"), ("viewing", "Показ"), ("negotiation", "Переговоры"), ("reserved", "Бронь"), ("won", "Завершена"), ("lost", "Отказ")], default="new", max_length=20, verbose_name="Этап сделки")),
                ("viewing_at", models.DateTimeField(blank=True, null=True, verbose_name="Дата и время показа")),
                ("next_action", models.CharField(blank=True, max_length=255, verbose_name="Следующее действие")),
                ("next_action_at", models.DateTimeField(blank=True, null=True, verbose_name="Когда выполнить")),
                ("outcome_note", models.TextField(blank=True, verbose_name="Итог или причина отказа")),
                ("expected_commission", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name="Ожидаемая комиссия")),
                ("closed_at", models.DateTimeField(blank=True, null=True, verbose_name="Дата завершения")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deals", to="properties.client", verbose_name="Клиент")),
                ("lead", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deals", to="properties.lead", verbose_name="Исходная заявка")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deals", to=settings.AUTH_USER_MODEL)),
                ("property", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deals", to="properties.property", verbose_name="Объект")),
                ("responsible", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_deals", to=settings.AUTH_USER_MODEL, verbose_name="Ответственный")),
            ],
            options={
                "verbose_name": "Сделка",
                "verbose_name_plural": "Сделки",
                "ordering": ["-updated_at"],
                "indexes": [models.Index(fields=["owner", "stage"], name="deal_owner_stage_idx")],
                "constraints": [models.UniqueConstraint(condition=models.Q(stage__in=["new", "in_progress", "viewing", "negotiation", "reserved"]), fields=("client", "property"), name="unique_active_deal_per_client_property")],
            },
        ),
    ]
