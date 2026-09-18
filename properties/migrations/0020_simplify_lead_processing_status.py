from django.db import migrations, models


def normalize_existing_statuses(apps, schema_editor):
    Lead = apps.get_model("properties", "Lead")
    Lead.objects.exclude(status="new").update(status="read")


class Migration(migrations.Migration):
    dependencies = [("properties", "0019_landing_block_controls_and_benefits")]

    operations = [
        migrations.RunPython(normalize_existing_statuses, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="lead",
            name="status",
            field=models.CharField(
                choices=[("new", "Новая"), ("read", "Прочитана"), ("archived", "Архив")],
                default="new",
                max_length=20,
                verbose_name="Статус обработки",
            ),
        ),
    ]
