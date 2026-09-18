from django.db import migrations


def add_trust_block(apps, schema_editor):
    Property = apps.get_model("properties", "Property")
    default_order = ["hero", "facts", "description", "gallery", "mortgage", "trust", "contact"]
    for property in Property.objects.all().only("id", "landing_block_order"):
        order = property.landing_block_order
        if not isinstance(order, list):
            property.landing_block_order = default_order
        elif "trust" not in order:
            insert_at = order.index("contact") if "contact" in order else len(order)
            property.landing_block_order = order[:insert_at] + ["trust"] + order[insert_at:]
        else:
            continue
        property.save(update_fields=["landing_block_order"])


def remove_trust_block(apps, schema_editor):
    Property = apps.get_model("properties", "Property")
    for property in Property.objects.all().only("id", "landing_block_order"):
        order = property.landing_block_order
        if isinstance(order, list) and "trust" in order:
            property.landing_block_order = [block for block in order if block != "trust"]
            property.save(update_fields=["landing_block_order"])


class Migration(migrations.Migration):

    dependencies = [
        ("properties", "0015_realtorprofile_about"),
    ]

    operations = [
        migrations.RunPython(add_trust_block, remove_trust_block),
    ]
