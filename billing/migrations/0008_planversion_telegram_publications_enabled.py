from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("billing", "0007_resourcereservation_request_fingerprint_and_more")]

    operations = [
        migrations.AddField(
            model_name="planversion",
            name="telegram_publications_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AlterField(
            model_name="planversion",
            name="policy_version",
            field=models.CharField(default="BP-0.2", max_length=30),
        ),
    ]
