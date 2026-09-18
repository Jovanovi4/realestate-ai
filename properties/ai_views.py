from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView

from .forms import AIContentEditForm, AIRequestForm
from .models import AIContent, Lead, Property, RealtorProfile
from .services.ai_service import AIService, AIServiceError


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def render_ai_content_response(request, ai_content):
    return render(
        request,
        "properties/includes/ai_content_response.html",
        {
            "ai_content": ai_content,
            "property": ai_content.property,
            "form": AIContentEditForm(instance=ai_content),
            "history": ai_content.property.contents.all(),
        },
    )


DEFAULT_TARGETS = {
    "headline": "marketing_headline",
    "short_description": "short_description",
    "full_description": "description",
    "description": "description",
    "landing_headline": "landing_title",
    "landing_subtitle": "landing_subtitle",
    "landing_about": "description",
    "seo_title": "seo_title",
    "seo_description": "seo_description",
    "benefits": "profile_about",
    "cta": "landing_contact_title",
}


class AIAssistantView(LoginRequiredMixin, DetailView):
    model = Property
    template_name = "properties/ai_assistant.html"
    context_object_name = "property"

    def get_queryset(self):
        return Property.objects.filter(owner=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = kwargs.get("form") or AIRequestForm(leads=Lead.objects.filter(property=self.object))
        context["history"] = self.object.contents.all()
        context["usage"] = {
            "total": self.object.contents.count(),
            "ollama": self.object.contents.filter(provider="ollama").count(),
            "openai": self.object.contents.filter(provider="openai").count(),
        }
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = AIRequestForm(request.POST, leads=Lead.objects.filter(property=self.object))
        if not form.is_valid():
            if is_htmx(request):
                return render(request, "properties/includes/ai_generation_error.html", {"message": "Проверьте выбранные параметры генерации."})
            return self.render_to_response(self.get_context_data(form=form))
        mode = form.cleaned_data["mode"]
        try:
            if mode == "audit":
                profile = RealtorProfile.objects.filter(user=request.user).first()
                result = AIService.audit_property(self.object, bool(profile and (profile.phone or profile.email or profile.telegram_username)))
                results = [{**result, "content_type": "audit", "apply_target": ""}]
            elif mode in {"package", "landing"}:
                results = [{**result, "apply_target": DEFAULT_TARGETS.get(result["content_type"], "")} for result in AIService.generate_bundle(self.object, form.cleaned_data["tone"], mode)]
            elif mode == "improve":
                result = AIService.improve_text(self.object, form.cleaned_data["content_type"], form.cleaned_data["tone"], form.cleaned_data["improvement"], form.cleaned_data["source_text"])
                results = [{**result, "content_type": form.cleaned_data["content_type"], "apply_target": DEFAULT_TARGETS.get(form.cleaned_data["content_type"], "")}]
            elif mode == "lead_reply":
                lead = form.cleaned_data["lead"]
                if not lead:
                    raise AIServiceError("Выберите заявку клиента.")
                result = AIService.generate_lead_reply(self.object, lead, form.cleaned_data["tone"])
                results = [{**result, "content_type": "lead_reply", "lead": lead, "apply_target": ""}]
            else:
                result = AIService.generate_content(self.object, form.cleaned_data["content_type"], form.cleaned_data["tone"])
                results = [{**result, "content_type": form.cleaned_data["content_type"], "apply_target": DEFAULT_TARGETS.get(form.cleaned_data["content_type"], "")}]
        except AIServiceError as error:
            if is_htmx(request):
                return render(request, "properties/includes/ai_generation_error.html", {"message": str(error)})
            messages.error(request, str(error))
            return self.render_to_response(self.get_context_data(form=form))

        contents = [AIContent.objects.create(property=self.object, tone=form.cleaned_data["tone"], title=self.object.title, **result) for result in results]
        content = contents[0]
        if is_htmx(request):
            return render_ai_content_response(request, content)
        messages.success(request, "Текст создан. Проверьте и отредактируйте его перед применением.")
        return redirect("ai_content_edit", pk=content.pk)


class AIContentEditView(LoginRequiredMixin, DetailView):
    model = AIContent
    template_name = "properties/ai_content_edit.html"
    context_object_name = "ai_content"

    def get_queryset(self):
        return AIContent.objects.select_related("property").filter(property__owner=self.request.user)

    def get_template_names(self):
        if is_htmx(self.request):
            return ["properties/includes/ai_content_editor.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = kwargs.get("form") or AIContentEditForm(instance=self.object)
        context["history"] = self.object.property.contents.all()
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = AIContentEditForm(request.POST, instance=self.object)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        ai_content = form.save()
        if request.POST.get("action") == "apply":
            field_name = ai_content.apply_target or DEFAULT_TARGETS.get(ai_content.content_type)
            if not field_name:
                if is_htmx(request):
                    return render(request, "properties/includes/ai_generation_error.html", {"message": "Для этого результата выберите поле, куда его нужно применить."})
                messages.error(request, "Для этого результата выберите поле, куда его нужно применить.")
                return redirect("ai_content_edit", pk=ai_content.pk)
            if field_name == "profile_about":
                profile, _ = RealtorProfile.objects.get_or_create(user=ai_content.property.owner)
                profile.about = ai_content.edited_content
                profile.save(update_fields=["about"])
            else:
                setattr(ai_content.property, field_name, ai_content.edited_content)
                ai_content.property.save(update_fields=[field_name, "updated_at"])
            ai_content.is_applied = True
            ai_content.applied_at = timezone.now()
            ai_content.save(update_fields=["is_applied", "applied_at", "edited_content"])
            if is_htmx(request):
                return render_ai_content_response(request, ai_content)
            messages.success(request, "Текст применён к объекту.")
            return redirect("property_detail", pk=ai_content.property.pk)
        if is_htmx(request):
            return render_ai_content_response(request, ai_content)
        messages.success(request, "Правки сохранены в истории. Текст ещё не применён к объекту.")
        return redirect("ai_content_edit", pk=ai_content.pk)


class AIContentDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        ai_content = get_object_or_404(
            AIContent.objects.select_related("property"),
            pk=pk,
            property__owner=request.user,
        )
        property = ai_content.property
        property_pk = property.pk
        ai_content.delete()
        if is_htmx(request):
            return render(request, "properties/includes/ai_content_deleted_response.html", {"property": property, "history": property.contents.all()})
        messages.success(request, "Запись удалена из истории генераций.")
        return redirect("ai_assistant", pk=property_pk)


@login_required
def generate_description(request, pk):
    """Preserve the old URL, directing it to the review-first assistant workflow."""
    get_object_or_404(Property, pk=pk, owner=request.user)
    return redirect("ai_assistant", pk=pk)
