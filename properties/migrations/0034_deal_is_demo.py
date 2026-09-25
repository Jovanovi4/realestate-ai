from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0033_deal")]

    operations = [
        migrations.AddField(
            model_name="deal",
            name="is_demo",
            field=models.BooleanField(default=False, verbose_name="Демонстрационная сделка"),
        ),
    ]
