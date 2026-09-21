from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0025_alter_property_status")]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="contact_purpose",
            field=models.CharField(
                choices=[
                    ("viewing", "Записаться на просмотр"),
                    ("presentation", "Получить презентацию"),
                    ("rent_terms", "Уточнить условия аренды"),
                ],
                default="viewing",
                max_length=20,
                verbose_name="Повод обращения",
            ),
        ),
    ]
