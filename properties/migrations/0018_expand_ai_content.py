from django.db import migrations, models
import django.db.models.deletion


def set_default_targets(apps, schema_editor):
    AIContent = apps.get_model("properties", "AIContent")
    targets = {
        "headline": "marketing_headline",
        "short_description": "short_description",
        "full_description": "description",
        "description": "description",
    }
    for content_type, target in targets.items():
        AIContent.objects.filter(content_type=content_type, apply_target="").update(apply_target=target)


class Migration(migrations.Migration):
    dependencies = [("properties", "0017_client_crm")]

    operations = [
        migrations.AddField(
            model_name="aicontent",
            name="apply_target",
            field=models.CharField(blank=True, choices=[("", "Не применять автоматически"), ("marketing_headline", "Заголовок объявления"), ("short_description", "Короткое описание"), ("description", "Полное описание"), ("landing_title", "Заголовок лендинга"), ("landing_subtitle", "Подзаголовок лендинга"), ("landing_about_title", "Заголовок блока «Об объекте»"), ("landing_contact_title", "Заголовок блока заявки"), ("seo_title", "SEO-заголовок"), ("seo_description", "SEO-описание"), ("profile_about", "Текст «О риелторе»")], max_length=40, verbose_name="Куда применить"),
        ),
        migrations.AddField(
            model_name="aicontent",
            name="duration_ms",
            field=models.PositiveIntegerField(default=0, verbose_name="Время генерации, мс"),
        ),
        migrations.AddField(
            model_name="aicontent",
            name="lead",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ai_contents", to="properties.lead"),
        ),
        migrations.AlterField(
            model_name="aicontent",
            name="content_type",
            field=models.CharField(choices=[("headline", "Заголовок объявления"), ("short_description", "Короткое описание"), ("full_description", "Полное описание"), ("description", "Описание (архив)"), ("landing_headline", "Первый экран лендинга"), ("landing_subtitle", "Подзаголовок лендинга"), ("landing_about", "Текст «Об объекте»"), ("seo_title", "SEO-заголовок"), ("seo_description", "SEO-описание"), ("benefits", "Преимущества для лендинга"), ("cta", "Призыв к действию"), ("audit", "Проверка готовности"), ("lead_reply", "Ответ клиенту")], max_length=30),
        ),
        migrations.RunPython(set_default_targets, migrations.RunPython.noop),
    ]
