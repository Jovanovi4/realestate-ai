from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0022_alter_aicontent_apply_target")]

    operations = [
        migrations.AddField(
            model_name="property",
            name="cian_export",
            field=models.BooleanField(default=False, verbose_name="Включить в экспорт ЦИАН"),
        ),
    ]
