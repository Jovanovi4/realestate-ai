from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView

from .forms import AIContentEditForm, AIRequestForm
from .models import AIContent, Property
from .services.ai_service import AIService, AIServiceError


class AIAssistantView(LoginRequiredMixin, DetailView):
    model = Property
    template_name = "properties/ai_assistant.html"
    context_object_name = "property"

    def get_queryset(self):
        return Property.objects.filter(owner=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = kwargs.get("form") or AIRequestForm()
        context["history"] = self.object.contents.all()
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = AIRequestForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        try:
            result = AIService.generate_content(self.object, **form.cleaned_data)
        except AIServiceError as error:
            messages.error(request, str(error))
            return self.render_to_response(self.get_context_data(form=form))

        content = AIContent.objects.create(
            property=self.object,
            content_type=form.cleaned_data["content_type"],
            tone=form.cleaned_data["tone"],
            title=self.object.title,
            **result,
        )
        messages.success(request, "Текст создан. Проверьте и отредактируйте его перед применением.")
        return redirect("ai_content_edit", pk=content.pk)


class AIContentEditView(LoginRequiredMixin, DetailView):
    model = AIContent
    template_name = "properties/ai_content_edit.html"
    context_object_name = "ai_content"

    def get_queryset(self):
        return AIContent.objects.select_related("property").filter(property__owner=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = kwargs.get("form") or AIContentEditForm(instance=self.object)
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = AIContentEditForm(request.POST, instance=self.object)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        ai_content = form.save()
        if request.POST.get("action") == "apply":
            field_by_content_type = {
                "headline": "marketing_headline",
                "short_description": "short_description",
                "full_description": "description",
                "description": "description",
            }
            field_name = field_by_content_type[ai_content.content_type]
            setattr(ai_content.property, field_name, ai_content.edited_content)
            ai_content.property.save(update_fields=[field_name, "updated_at"])
            ai_content.is_applied = True
            ai_content.applied_at = timezone.now()
            ai_content.save(update_fields=["is_applied", "applied_at", "edited_content"])
            messages.success(request, "Текст применён к объекту.")
            return redirect("property_detail", pk=ai_content.property.pk)
        messages.success(request, "Правки сохранены в истории. Текст ещё не применён к объекту.")
        return redirect("ai_content_edit", pk=ai_content.pk)


class AIContentDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        ai_content = get_object_or_404(
            AIContent.objects.select_related("property"),
            pk=pk,
            property__owner=request.user,
        )
        property_pk = ai_content.property.pk
        ai_content.delete()
        messages.success(request, "Запись удалена из истории генераций.")
        return redirect("ai_assistant", pk=property_pk)


@login_required
def generate_description(request, pk):
    """Preserve the old URL, directing it to the review-first assistant workflow."""
    get_object_or_404(Property, pk=pk, owner=request.user)
    return redirect("ai_assistant", pk=pk)
