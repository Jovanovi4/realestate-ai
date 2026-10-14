import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_lead_workspace(apps, schema_editor):
    Lead = apps.get_model("properties", "Lead")
    for lead in Lead.objects.select_related("property").iterator(chunk_size=500):
        lead.owner_id = lead.property.owner_id
        lead.agency_id = lead.property.agency_id
        lead.save(update_fields=["owner", "agency"])


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0059_agency_closure"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="lead", name="owner",
            field=models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.CASCADE,
                                    related_name="leads", null=True),
        ),
        migrations.AddField(
            model_name="lead", name="agency",
            field=models.ForeignKey(to="properties.agency", on_delete=django.db.models.deletion.PROTECT,
                                    related_name="leads", null=True, blank=True),
        ),
        migrations.AlterField(
            model_name="lead", name="property",
            field=models.ForeignKey(to="properties.property", on_delete=django.db.models.deletion.PROTECT,
                                    related_name="leads", null=True, blank=True),
        ),
        migrations.RunPython(backfill_lead_workspace, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="lead", name="owner",
            field=models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.CASCADE,
                                    related_name="leads"),
        ),
    ]
