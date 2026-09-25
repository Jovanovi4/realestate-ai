from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def move_schedule_data(apps, schema_editor):
    Task = apps.get_model("properties", "ClientReminder")
    Deal = apps.get_model("properties", "Deal")
    Showing = apps.get_model("properties", "Showing")

    for task in Task.objects.select_related("client").iterator():
        task.owner_id = task.client.owner_id
        task.save(update_fields=["owner"])

    for deal in Deal.objects.all().iterator():
        if deal.next_action:
            Task.objects.create(
                owner_id=deal.owner_id,
                client_id=deal.client_id,
                deal_id=deal.pk,
                text=deal.next_action,
                due_at=deal.next_action_at,
            )
        if deal.viewing_at:
            Showing.objects.create(
                owner_id=deal.owner_id,
                deal_id=deal.pk,
                starts_at=deal.viewing_at,
            )


class Migration(migrations.Migration):
    dependencies = [("properties", "0034_deal_is_demo")]

    operations = [
        migrations.AddField(
            model_name="clientreminder",
            name="owner",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="crm_tasks", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name="clientreminder",
            name="client",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="reminders", to="properties.client", verbose_name="Клиент"),
        ),
        migrations.AlterField(
            model_name="clientreminder",
            name="due_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Срок выполнения"),
        ),
        migrations.AlterField(
            model_name="clientreminder",
            name="text",
            field=models.CharField(max_length=255, verbose_name="Задача"),
        ),
        migrations.AlterModelOptions(
            name="clientreminder",
            options={"ordering": ["is_done", "due_at"], "verbose_name": "Задача", "verbose_name_plural": "Задачи"},
        ),
        migrations.AddField(
            model_name="clientreminder",
            name="deal",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="tasks", to="properties.deal", verbose_name="Сделка"),
        ),
        migrations.AddField(
            model_name="clientreminder",
            name="overdue_notified_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Уведомление о просрочке отправлено"),
        ),
        migrations.CreateModel(
            name="Showing",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("starts_at", models.DateTimeField(verbose_name="Начало показа")),
                ("ends_at", models.DateTimeField(blank=True, null=True, verbose_name="Окончание показа")),
                ("status", models.CharField(choices=[("planned", "Запланирован"), ("completed", "Проведён"), ("cancelled", "Отменён")], default="planned", max_length=20, verbose_name="Статус")),
                ("location", models.CharField(blank=True, max_length=255, verbose_name="Место встречи")),
                ("outcome_note", models.TextField(blank=True, verbose_name="Итог показа")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deal", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="showings", to="properties.deal", verbose_name="Сделка")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="showings", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Показ",
                "verbose_name_plural": "Показы",
                "ordering": ["starts_at"],
                "indexes": [models.Index(fields=["owner", "starts_at"], name="showing_owner_start_idx")],
            },
        ),
        migrations.RunPython(move_schedule_data, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="clientreminder",
            name="owner",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="crm_tasks", to=settings.AUTH_USER_MODEL),
        ),
        migrations.RemoveField(model_name="deal", name="viewing_at"),
        migrations.RemoveField(model_name="deal", name="next_action"),
        migrations.RemoveField(model_name="deal", name="next_action_at"),
    ]
