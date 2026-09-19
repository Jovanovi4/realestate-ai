import properties.models
from django.db import migrations, models


def copy_profile_benefits_to_properties(apps, schema_editor):
    Property = apps.get_model("properties", "Property")
    RealtorProfile = apps.get_model("properties", "RealtorProfile")
    profiles = {profile.user_id: profile for profile in RealtorProfile.objects.all()}
    for property in Property.objects.all():
        profile = profiles.get(property.owner_id)
        if not profile:
            continue
        property.landing_trust_about = profile.about or ""
        if profile.benefits:
            property.landing_benefits = profile.benefits
        property.save(update_fields=["landing_trust_about", "landing_benefits"])


class Migration(migrations.Migration):
    dependencies = [("properties", "0020_simplify_lead_processing_status")]

    operations = [
        migrations.AddField(
            model_name="property",
            name="landing_trust_about",
            field=models.TextField(blank=True, verbose_name="Текст блока преимуществ"),
        ),
        migrations.AddField(
            model_name="property",
            name="landing_benefits",
            field=models.JSONField(default=properties.models.default_realtor_benefits, verbose_name="Карточки преимуществ лендинга"),
        ),
        migrations.RunPython(copy_profile_benefits_to_properties, migrations.RunPython.noop),
    ]
