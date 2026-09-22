from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0030_client_is_demo_lead_is_demo_property_is_demo_and_more")]

    operations = [
        migrations.AddField(
            model_name="realtorprofile",
            name="onboarding_started",
            field=models.BooleanField(default=False, verbose_name="Онбординг начат"),
        ),
    ]
