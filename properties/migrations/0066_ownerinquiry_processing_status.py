from django.db import migrations, models
from django.db.models import Q


def mark_contacted_inquiries_in_progress(apps, schema_editor):
    OwnerInquiry = apps.get_model("properties", "OwnerInquiry")
    OwnerInquiry.objects.filter(
        Q(first_contacted_at__isnull=False) | ~Q(result="pending") | Q(property__isnull=False),
    ).update(status="read")


class Migration(migrations.Migration):
    dependencies = [("properties", "0065_landing_alternative_requests")]

    operations = [
        migrations.AlterField(
            model_name="lead",
            name="status",
            field=models.CharField(choices=[("new", "Новая"), ("read", "В работе"), ("archived", "Архив")], default="new", max_length=20, verbose_name="Статус обработки"),
        ),
        migrations.AddField(
            model_name="ownerinquiry",
            name="status",
            field=models.CharField(choices=[("new", "Новая"), ("read", "В работе"), ("archived", "Архив")], db_index=True, default="new", max_length=20, verbose_name="Статус обработки"),
        ),
        migrations.AddField(
            model_name="ownerinquiry",
            name="archived_at",
            field=models.DateTimeField(blank=True, db_index=True, editable=False, null=True, verbose_name="Дата архивирования"),
        ),
        migrations.RunPython(mark_contacted_inquiries_in_progress, migrations.RunPython.noop),
    ]
