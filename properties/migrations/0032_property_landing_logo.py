from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0031_realtorprofile_onboarding_started")]

    operations = [
        migrations.AddField(
            model_name="property",
            name="landing_logo",
            field=models.ImageField(blank=True, upload_to="landing_logos/", verbose_name="Логотип в шапке лендинга"),
        ),
    ]
