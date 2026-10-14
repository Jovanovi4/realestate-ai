from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0057_property_first_published_at_showing_completed_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="agencyinvitation",
            name="revoked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
