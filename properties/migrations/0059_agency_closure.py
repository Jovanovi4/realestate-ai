import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("properties", "0058_agencyinvitation_revoked_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="agency",
            name="owner",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="owned_agencies", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="agency",
            name="closed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="agency",
            name="closed_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="closed_agencies", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="agency",
            name="closure_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddConstraint(
            model_name="agency",
            constraint=models.UniqueConstraint(fields=("owner",), condition=Q(closed_at__isnull=True), name="unique_active_agency_owner"),
        ),
    ]
