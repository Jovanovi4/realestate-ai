from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def link_existing_leads(apps, schema_editor):
    Client = apps.get_model("properties", "Client")
    Lead = apps.get_model("properties", "Lead")
    status_map = {"new": "new", "in_progress": "in_progress", "viewing": "viewing", "won": "won", "lost": "lost"}
    for lead in Lead.objects.select_related("property").order_by("created_at", "id"):
        if not lead.property.owner_id:
            continue
        client, created = Client.objects.get_or_create(
            owner_id=lead.property.owner_id,
            phone=lead.phone,
            defaults={"name": lead.name, "source": "landing", "status": status_map.get(lead.status, "new")},
        )
        if not client.name and lead.name:
            client.name = lead.name
            client.save(update_fields=["name"])
        lead.client_id = client.pk
        lead.save(update_fields=["client"])


class Migration(migrations.Migration):
    dependencies = [("properties", "0016_add_trust_landing_block")]

    operations = [
        migrations.CreateModel(
            name="Client",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=150, verbose_name="Имя")),
                ("phone", models.CharField(max_length=30, verbose_name="Телефон")),
                ("preferred_contact_time", models.CharField(blank=True, max_length=120, verbose_name="Удобное время связи")),
                ("budget", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name="Бюджет")),
                ("preferred_area", models.CharField(blank=True, max_length=255, verbose_name="Интересующий район")),
                ("source", models.CharField(choices=[("landing", "Лендинг"), ("avito", "Avito"), ("recommendation", "Рекомендация"), ("manual", "Добавлен вручную"), ("other", "Другое")], default="landing", max_length=30, verbose_name="Источник")),
                ("notes", models.TextField(blank=True, verbose_name="Комментарий риелтора")),
                ("status", models.CharField(choices=[("new", "Новый"), ("in_progress", "В работе"), ("viewing", "Показ"), ("negotiation", "Переговоры"), ("won", "Сделка"), ("lost", "Отказ")], default="new", max_length=20, verbose_name="Этап воронки")),
                ("outcome_reason", models.CharField(blank=True, max_length=255, verbose_name="Причина результата")),
                ("first_contacted_at", models.DateTimeField(blank=True, null=True, verbose_name="Первый контакт")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="clients", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "Клиент", "verbose_name_plural": "Клиенты", "ordering": ["-updated_at"]},
        ),
        migrations.CreateModel(
            name="ClientReminder",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("text", models.CharField(max_length=255, verbose_name="Напоминание")),
                ("due_at", models.DateTimeField(verbose_name="Когда напомнить")),
                ("is_done", models.BooleanField(default=False, verbose_name="Выполнено")),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reminders", to="properties.client")),
            ],
            options={"verbose_name": "Напоминание", "verbose_name_plural": "Напоминания", "ordering": ["is_done", "due_at"]},
        ),
        migrations.AddField(
            model_name="lead", name="client", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="leads", to="properties.client"),
        ),
        migrations.CreateModel(
            name="ClientInteraction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("interaction_type", models.CharField(choices=[("call", "Звонок"), ("message", "Сообщение"), ("viewing", "Показ"), ("note", "Заметка"), ("status", "Изменение статуса")], default="note", max_length=20, verbose_name="Тип")),
                ("text", models.TextField(blank=True, verbose_name="Комментарий")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("client", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="interactions", to="properties.client")),
                ("lead", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="interactions", to="properties.lead")),
            ],
            options={"verbose_name": "Взаимодействие с клиентом", "verbose_name_plural": "История взаимодействий", "ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(model_name="client", constraint=models.UniqueConstraint(fields=("owner", "phone"), name="unique_client_phone_per_owner")),
        migrations.RunPython(link_existing_leads, migrations.RunPython.noop),
    ]
