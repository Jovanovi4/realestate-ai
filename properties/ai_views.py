from django.contrib.auth.decorators import login_required
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect

from .models import AIContent, Property
from .services.ai_service import AIService


@login_required
def generate_description(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    property = get_object_or_404(Property, pk=pk, owner=request.user)
    description = AIService.generate_description(property)
    property.description = description
    property.save()
    AIContent.objects.create(
        property=property,
        content_type="description",
        title=property.title,
        content=description,
    )
    return redirect("property_detail", pk=property.pk)
