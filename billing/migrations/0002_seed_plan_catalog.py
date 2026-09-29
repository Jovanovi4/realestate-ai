from django.db import migrations


def seed_catalog(apps, schema_editor):
    # Frozen BP-0.1 values: do not import the mutable runtime catalog here.
    Plan = apps.get_model("billing", "Plan")
    Version = apps.get_model("billing", "PlanVersion")
    alias = schema_editor.connection.alias
    plans = [
        ("demo", "Демо", 2, 50, 1, 1, 200 * 1024**2, 0, 5, False, False),
        ("standard", "Стандарт", 20, 1000, 10, 1, 2 * 1024**3, 100, 0, True, False),
        ("business", "Бизнес", 100, 5000, 50, 5, 10 * 1024**3, 500, 0, True, True),
    ]
    for code, name, active, total, landings, seats, storage, ai, demo_ai, xml, team in plans:
        plan, _ = Plan.objects.using(alias).get_or_create(code=code, defaults={"name": name})
        Version.objects.using(alias).get_or_create(plan=plan, version=1, defaults={
            "policy_version": "BP-0.1", "active_properties_limit": active, "total_properties_limit": total,
            "landings_limit": landings, "seats_limit": seats, "storage_bytes_limit": storage,
            "ai_credits_limit": ai, "demo_ai_credits_limit": demo_ai, "xml_export_enabled": xml,
            "team_enabled": team, "grace_days": 7,
            "ai_costs": {"short_text": 1, "improve": 1, "lead_reply": 1, "full_description": 2, "package": 3, "landing": 5, "audit": 0},
            "monthly_price_minor": 0 if code == "demo" else None, "currency": "", "available": True,
        })


class Migration(migrations.Migration):
    dependencies = [("billing", "0001_initial")]
    # Leave history intact when migrating backwards; no fake payments or user grants.
    operations = [migrations.RunPython(seed_catalog, migrations.RunPython.noop)]
