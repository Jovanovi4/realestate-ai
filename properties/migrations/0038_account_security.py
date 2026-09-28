import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0037_backfill_lead_assignees"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="realtorprofile", name="email_verified_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="realtorprofile", name="email_verification_nonce",
            field=models.UUIDField(default=uuid.uuid4, editable=False),
        ),
        migrations.CreateModel(
            name="AccountEmailRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("verify", "Подтверждение email"), ("reset", "Восстановление доступа")], max_length=10)),
                ("nonce", models.UUIDField(blank=True, null=True)),
                ("auth_hash", models.CharField(blank=True, max_length=64)),
                ("status", models.CharField(choices=[("pending", "Ожидает"), ("sent", "Отправлено"), ("discarded", "Отменено"), ("failed", "Ошибка")], default="pending", max_length=10)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="account_email_requests", to=settings.AUTH_USER_MODEL)),
            ],
            options={"indexes": [models.Index(fields=["status", "created_at"], name="account_mail_status_created")]},
        ),
    ]
