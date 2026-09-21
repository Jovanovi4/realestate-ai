import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0028_realtorprofile_telegram_chat_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="personal_data_consent_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Согласие на обработку данных получено"),
        ),
        migrations.AddField(
            model_name="lead",
            name="personal_data_consent_version",
            field=models.CharField(blank=True, max_length=32, verbose_name="Версия согласия"),
        ),
        migrations.CreateModel(
            name="UserLegalAcceptance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("terms_accepted_at", models.DateTimeField(verbose_name="Условия сервиса приняты")),
                ("terms_version", models.CharField(max_length=32, verbose_name="Версия условий")),
                ("personal_data_consent_at", models.DateTimeField(verbose_name="Согласие на обработку данных получено")),
                ("personal_data_consent_version", models.CharField(max_length=32, verbose_name="Версия согласия")),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="legal_acceptance", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Принятие юридических документов",
                "verbose_name_plural": "Принятие юридических документов",
            },
        ),
    ]
