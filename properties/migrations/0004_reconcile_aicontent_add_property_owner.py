# Generated manually to reconcile the historical AIContent migration with its model.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0003_aicontent"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="property",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="properties",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Риелтор",
            ),
        ),
        migrations.RemoveField(model_name="aicontent", name="full_description"),
        migrations.RemoveField(model_name="aicontent", name="headline"),
        migrations.RemoveField(model_name="aicontent", name="instagram"),
        migrations.RemoveField(model_name="aicontent", name="landing"),
        migrations.RemoveField(model_name="aicontent", name="short_description"),
        migrations.RemoveField(model_name="aicontent", name="updated_at"),
        migrations.AddField(model_name="aicontent", name="content", field=models.TextField(default=""), preserve_default=False),
        migrations.AddField(model_name="aicontent", name="content_type", field=models.CharField(default="description", max_length=30), preserve_default=False),
        migrations.AddField(model_name="aicontent", name="language", field=models.CharField(default="ru", max_length=10)),
        migrations.AddField(model_name="aicontent", name="title", field=models.CharField(blank=True, max_length=255)),
        migrations.AlterField(
            model_name="aicontent",
            name="property",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="contents", to="properties.property"),
        ),
    ]
