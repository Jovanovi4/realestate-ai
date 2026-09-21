from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0026_lead_contact_purpose")]

    operations = [
        migrations.AddField(
            model_name="property",
            name="landing_accent",
            field=models.CharField(
                choices=[("template", "По шаблону"), ("graphite", "Графитовый"), ("blue", "Синий"), ("emerald", "Изумрудный"), ("terracotta", "Терракотовый")],
                default="template", max_length=20, verbose_name="Акцентный цвет",
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="landing_button_style",
            field=models.CharField(
                choices=[("template", "По шаблону"), ("rounded", "Скруглённые"), ("strict", "Строгие")],
                default="template", max_length=20, verbose_name="Форма кнопок",
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="landing_gallery_style",
            field=models.CharField(
                choices=[("large", "Крупное фото"), ("grid", "Сетка фото")],
                default="large", max_length=20, verbose_name="Вид галереи",
            ),
        ),
        migrations.AddField(
            model_name="property",
            name="landing_hero_layout",
            field=models.CharField(
                choices=[("split", "Фото рядом с текстом"), ("background", "Фото на фоне")],
                default="split", max_length=20, verbose_name="Вид первого экрана",
            ),
        ),
    ]
