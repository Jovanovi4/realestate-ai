from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0027_property_landing_visual_settings")]

    operations = [
        migrations.AddField(
            model_name="realtorprofile",
            name="telegram_chat_id",
            field=models.CharField(
                blank=True,
                help_text="Приватный ID чата с ботом. Он не показывается посетителям лендингов.",
                max_length=64,
                verbose_name="Telegram chat ID для уведомлений",
            ),
        ),
    ]
