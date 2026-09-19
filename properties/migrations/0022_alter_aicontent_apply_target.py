from django.db import migrations, models


def move_legacy_ai_target(apps, schema_editor):
    AIContent = apps.get_model("properties", "AIContent")
    AIContent.objects.filter(apply_target="profile_about").update(apply_target="landing_trust_about")


class Migration(migrations.Migration):
    dependencies = [("properties", "0021_property_landing_benefits")]

    operations = [
        migrations.AlterField(
            model_name="aicontent",
            name="apply_target",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Не применять автоматически"),
                    ("marketing_headline", "Заголовок объявления"),
                    ("short_description", "Короткое описание"),
                    ("description", "Полное описание"),
                    ("landing_title", "Заголовок лендинга"),
                    ("landing_subtitle", "Подзаголовок лендинга"),
                    ("landing_about_title", "Заголовок блока «Об объекте»"),
                    ("landing_contact_title", "Заголовок блока заявки"),
                    ("seo_title", "SEO-заголовок"),
                    ("seo_description", "SEO-описание"),
                    ("landing_trust_about", "Текст блока преимуществ"),
                ],
                max_length=40,
                verbose_name="Куда применить",
            ),
        ),
        migrations.RunPython(move_legacy_ai_target, migrations.RunPython.noop),
    ]
