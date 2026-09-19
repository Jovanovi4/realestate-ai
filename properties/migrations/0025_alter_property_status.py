from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0024_long_term_rent")]

    operations = [
        migrations.AlterField(
            model_name="property",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Черновик"), ("published", "Опубликован"), ("showing", "На показах"),
                    ("reserved", "Забронирован"), ("sold", "Продан"), ("archived", "В архиве"),
                    ("available", "Свободен"), ("rented", "Сдан"),
                ],
                default="draft", max_length=20, verbose_name="Статус",
            ),
        ),
    ]
