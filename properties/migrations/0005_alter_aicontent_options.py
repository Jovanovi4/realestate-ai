from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("properties", "0004_reconcile_aicontent_add_property_owner")]

    operations = [
        migrations.AlterModelOptions(
            name="aicontent",
            options={
                "ordering": ["-created_at"],
                "verbose_name": "ИИ-контент",
                "verbose_name_plural": "ИИ-контент",
            },
        ),
    ]
