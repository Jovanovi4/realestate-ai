from django.db import migrations


def assign_existing_leads(apps, schema_editor):
    Lead = apps.get_model("properties", "Lead")
    for lead in Lead.objects.filter(assigned_to__isnull=True).select_related("property").iterator():
        lead.assigned_to_id = lead.property.owner_id
        lead.save(update_fields=["assigned_to"])


class Migration(migrations.Migration):
    dependencies = [("properties", "0036_agency_agencyinvitation_agencymembership_auditevent_and_more")]
    operations = [migrations.RunPython(assign_existing_leads, migrations.RunPython.noop)]
