"""Reusable landing appearance, scoped to the object's workspace."""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from .agency_access import agency_for, may_manage_agency, workspace_queryset
from .forms import LandingStylePresetForm
from .models import LandingStylePreset, Property


APPEARANCE_FIELDS = (
    "landing_template", "landing_accent", "landing_custom_color",
    "landing_button_style", "landing_hero_layout", "landing_gallery_style",
)


def scope_for_editor(user, property=None):
    agency = property.agency if property and property.pk else agency_for(user)
    return {"agency": agency} if agency else {"owner": user}


def preset_payload(preset):
    return {
        "id": preset.pk,
        "name": preset.name,
        "delete_url": reverse("landing_style_preset_delete", args=[preset.pk]),
        **{field: getattr(preset, field) for field in APPEARANCE_FIELDS},
    }


def editor_preset_context(user, property=None):
    scope = scope_for_editor(user, property)
    agency = scope.get("agency")
    return {
        "landing_style_presets": [preset_payload(preset) for preset in LandingStylePreset.objects.filter(**scope)],
        "landing_preset_scope_label": "команды" if agency else "личного пространства",
        "landing_preset_can_manage": not agency or may_manage_agency(user),
    }


def _request_scope(request):
    raw = request.POST.get("property_id", "")
    if raw:
        if not raw.isascii() or not raw.isdecimal():
            raise Http404("Объект не найден")
        property = get_object_or_404(workspace_queryset(Property.objects.select_related("agency"), request.user), pk=raw)
    else:
        property = None
    return scope_for_editor(request.user, property)


class LandingStylePresetCreateView(LoginRequiredMixin, View):
    http_method_names = ["post", "options"]

    def post(self, request):
        scope = _request_scope(request)
        if scope.get("agency") and not may_manage_agency(request.user):
            return JsonResponse({"error": "Пресеты команды может сохранять руководитель или менеджер."}, status=403)
        form = LandingStylePresetForm(request.POST)
        if not form.is_valid():
            first_error = next(iter(form.errors.values()))[0]
            return JsonResponse({"error": str(first_error), "fields": form.errors}, status=400)
        name = form.cleaned_data["name"]
        if LandingStylePreset.objects.filter(**scope, name__iexact=name).exists():
            return JsonResponse({"error": "Пресет с таким названием уже есть."}, status=409)
        if LandingStylePreset.objects.filter(**scope).count() >= 20:
            return JsonResponse({"error": "В одном пространстве можно сохранить до 20 пресетов."}, status=409)
        preset = form.save(commit=False)
        for key, value in scope.items():
            setattr(preset, key, value)
        try:
            with transaction.atomic():
                preset.save()
        except IntegrityError:
            return JsonResponse({"error": "Пресет с таким названием уже есть."}, status=409)
        return JsonResponse({"preset": preset_payload(preset)}, status=201)


class LandingStylePresetDeleteView(LoginRequiredMixin, View):
    http_method_names = ["post", "options"]

    def post(self, request, pk):
        scope = _request_scope(request)
        if scope.get("agency") and not may_manage_agency(request.user):
            return JsonResponse({"error": "Пресеты команды может удалять руководитель или менеджер."}, status=403)
        preset = get_object_or_404(LandingStylePreset.objects.filter(**scope), pk=pk)
        preset.delete()
        return JsonResponse({"deleted": pk})
