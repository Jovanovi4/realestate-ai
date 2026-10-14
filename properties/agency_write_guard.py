"""Serialize team writes with closure, regardless of tariff enforcement mode."""

from django.db import connection, transaction
from django.http import HttpResponse
from django.urls import reverse

from .models import Agency, AgencyMembership


class AgencyWriteGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in {"GET", "HEAD", "OPTIONS"} or not request.user.is_authenticated:
            return self.get_response(request)
        agency_id = AgencyMembership.objects.filter(
            user=request.user, agency__closed_at__isnull=True,
        ).values_list("agency_id", flat=True).first()
        if agency_id is None:
            return self.get_response(request)
        with transaction.atomic():
            exclusive = request.path_info in {reverse("agency_close"), reverse("agency_rename")}
            if connection.vendor == "postgresql" and not exclusive:
                # FOR SHARE lets teammates write concurrently, while closure's
                # FOR UPDATE waits until each in-flight request has finished.
                with connection.cursor() as cursor:
                    cursor.execute(
                        f'SELECT closed_at FROM "{Agency._meta.db_table}" WHERE id = %s FOR SHARE',
                        [agency_id],
                    )
                    row = cursor.fetchone()
                exists = row is not None
                closed_at = row[0] if row else None
            else:
                agency = Agency.objects.select_for_update().filter(pk=agency_id).first()
                exists = agency is not None
                closed_at = agency.closed_at if agency else None
            if not exists:
                return HttpResponse("Команда не найдена. Обновите страницу.", status=409)
            if closed_at:
                return HttpResponse("Команда закрыта. Обновите страницу.", status=409)
            return self.get_response(request)
