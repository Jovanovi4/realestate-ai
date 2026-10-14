from django.db import migrations


def add_alternative_block(apps, schema_editor):
    Property = apps.get_model("properties", "Property")
    for property in Property.objects.only("pk", "landing_block_order", "landing_enabled_blocks").iterator():
        order = list(property.landing_block_order or [])
        if "alternative_requests" not in order:
            order.append("alternative_requests")
        enabled = dict(property.landing_enabled_blocks or {})
        enabled.setdefault("alternative_requests", False)
        Property.objects.filter(pk=property.pk).update(
            landing_block_order=order, landing_enabled_blocks=enabled,
        )


def remove_alternative_block(apps, schema_editor):
    Property = apps.get_model("properties", "Property")
    for property in Property.objects.only("pk", "landing_block_order", "landing_enabled_blocks").iterator():
        order = [key for key in (property.landing_block_order or []) if key != "alternative_requests"]
        enabled = dict(property.landing_enabled_blocks or {})
        enabled.pop("alternative_requests", None)
        Property.objects.filter(pk=property.pk).update(
            landing_block_order=order, landing_enabled_blocks=enabled,
        )


class Migration(migrations.Migration):
    dependencies = [("properties", "0064_alter_inboundevent_channel")]

    operations = [migrations.RunPython(add_alternative_block, remove_alternative_block)]
