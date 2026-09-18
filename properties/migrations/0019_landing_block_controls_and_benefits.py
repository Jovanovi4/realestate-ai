import properties.models
from django.db import migrations, models


def enable_existing_blocks(apps, schema_editor):
    Property = apps.get_model("properties", "Property")
    keys = ["hero", "facts", "description", "gallery", "mortgage", "trust", "contact"]
    for property in Property.objects.all().only("id", "landing_enabled_blocks"):
        property.landing_enabled_blocks = {key: True for key in keys}
        property.save(update_fields=["landing_enabled_blocks"])


class Migration(migrations.Migration):
    dependencies = [("properties", "0018_expand_ai_content")]
    operations = [
        migrations.AddField(
            model_name="property",
            name="landing_enabled_blocks",
            field=models.JSONField(default=properties.models.default_landing_enabled_blocks, verbose_name="Включённые блоки лендинга"),
        ),
        migrations.AddField(
            model_name="realtorprofile",
            name="benefits",
            field=models.JSONField(default=properties.models.default_realtor_benefits, verbose_name="Преимущества риелтора"),
        ),
        migrations.RunPython(enable_existing_blocks, migrations.RunPython.noop),
    ]
