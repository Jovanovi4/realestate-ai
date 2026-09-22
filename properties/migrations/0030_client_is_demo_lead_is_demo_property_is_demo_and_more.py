from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0029_lead_personal_data_consent_at_and_more")]

    operations = [
        migrations.AddField(
            model_name="client",
            name="is_demo",
            field=models.BooleanField(default=False, verbose_name="Демонстрационный клиент"),
        ),
        migrations.AddField(
            model_name="lead",
            name="is_demo",
            field=models.BooleanField(default=False, verbose_name="Демонстрационная заявка"),
        ),
        migrations.AddField(
            model_name="property",
            name="is_demo",
            field=models.BooleanField(default=False, verbose_name="Демонстрационный объект"),
        ),
        migrations.AddField(
            model_name="realtorprofile",
            name="demo_data_created",
            field=models.BooleanField(default=False, verbose_name="Демо-данные созданы"),
        ),
        migrations.AddField(
            model_name="realtorprofile",
            name="onboarding_dismissed",
            field=models.BooleanField(default=False, verbose_name="Онбординг скрыт"),
        ),
    ]
