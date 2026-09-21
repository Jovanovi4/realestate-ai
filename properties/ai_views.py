from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.core.paginator import Paginator
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView

from .forms import AIContentEditForm, AILeadReplyForm, AIRequestForm
from .models import AIContent, Lead, Property, RealtorProfile
from .services.ai_service import AIService, AIServiceError


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def get_ai_history_context(property, page_number=1):
    history = property.contents.exclude(content_type="lead_reply")
    paginator = Paginator(history, 10)
    return {
        "history": paginator.get_page(page_number),
        "history_total": paginator.count,
    }


def render_ai_content_response(request, ai_content):
    context = {
        "ai_content": ai_content,
        "property": ai_content.property,
        "form": AIContentEditForm(instance=ai_content),
    }
    context.update(get_ai_history_context(ai_content.property))
    return render(
        request,
        "properties/includes/ai_content_response.html",
        context,
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
    "benefits": "landing_trust_about",
    "cta": "landing_contact_title",
}

INLINE_TARGETS = set(AIService.INLINE_FIELD_INSTRUCTIONS)


def is_inline_target(target):
    return target in INLINE_TARGETS or (
        target.startswith("landing_benefit_")
        and target.rsplit("_", 1)[-1] in {"title", "description"}
        and target.split("_")[2] in {"1", "2", "3"}
    )


class AIEnabledMixin:
    """Keep AI endpoints unavailable when a deployment disables the feature."""

    def dispatch(self, request, *args, **kwargs):
        if not settings.AI_ENABLED:
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class AIInlineGenerateView(AIEnabledMixin, LoginRequiredMixin, View):
    """Return one generated text for the inline editor without saving it to the property."""

    def post(self, request, pk):
        property = get_object_or_404(Property, pk=pk, owner=request.user)
        target = request.POST.get("target", "")
        tone = request.POST.get("tone", "business")
        mode = request.POST.get("mode", "generate")
        source_text = request.POST.get("source_text", "").strip()
        improvement = request.POST.get("improvement", "")

        if not is_inline_target(target):
            return JsonResponse({"error": "Для этого поля генерация пока недоступна."}, status=400)
        if tone not in dict(AIContent.TONE_CHOICES):
            return JsonResponse({"error": "Выберите корректный тон текста."}, status=400)

        try:
            if mode == "improve":
                if not source_text:
                    return JsonResponse({"error": "Введите текст, который нужно улучшить."}, status=400)
                content_type = "benefits" if target.startswith("landing_benefit_") else AIService.INLINE_FIELD_INSTRUCTIONS[target][0]
                result = AIService.improve_text(property, content_type, tone, improvement, source_text)
            elif mode == "generate":
                content_type, result = AIService.generate_inline_content(property, target, tone)
            elif mode != "generate":
                return JsonResponse({"error": "Неизвестное действие ИИ."}, status=400)
        except AIServiceError as error:
            return JsonResponse({"error": str(error)}, status=400)

        apply_target = target if target in dict(AIContent.APPLY_TARGET_CHOICES) else ""
        AIContent.objects.create(
            property=property,
            content_type=content_type,
            tone=tone,
            title=property.title,
            apply_target=apply_target,
            **result,
        )
        return JsonResponse({"content": result["content"]})


class AIBundleGenerateView(AIEnabledMixin, LoginRequiredMixin, View):
    """Generate a review-first set of texts for one property."""

    def post(self, request, pk):
        property = get_object_or_404(Property, pk=pk, owner=request.user)
        tone = request.POST.get("tone", "business")
        bundle_type = request.POST.get("bundle", "package")
        if tone not in dict(AIContent.TONE_CHOICES):
            return JsonResponse({"error": "Выберите корректный тон текста."}, status=400)
        if bundle_type not in {"package", "landing"}:
            return JsonResponse({"error": "Неизвестный сценарий генерации."}, status=400)
        try:
            results = AIService.generate_bundle(property, tone, bundle_type)
        except AIServiceError as error:
            return JsonResponse({"error": str(error)}, status=400)

        contents = []
        for result in results:
            content_type = result["content_type"]
            content_payload = {key: value for key, value in result.items() if key != "content_type"}
            AIContent.objects.create(
                property=property,
                content_type=content_type,
                tone=tone,
                title=property.title,
                apply_target=DEFAULT_TARGETS[content_type],
                **content_payload,
            )
            contents.append({"target": DEFAULT_TARGETS[content_type], "content": result["content"]})
        return JsonResponse({"contents": contents})


class AIAssistantView(AIEnabledMixin, LoginRequiredMixin, DetailView):
    model = Property
    template_name = "properties/ai_assistant.html"
    context_object_name = "property"

    def get_queryset(self):
        return Property.objects.filter(owner=self.request.user)

    def get_template_names(self):
        if is_htmx(self.request) and self.request.GET.get("history_page"):
            return ["properties/includes/ai_history.html"]
        return [self.template_name]

    def get(self, request, *args, **kwargs):
        property = self.get_object()
        messages.info(request, "ИИ-инструменты теперь находятся рядом с текстовыми полями объекта.")
        return redirect(f"{reverse('edit_property', kwargs={'pk': property.pk})}#texts-pane")

    def post(self, request, *args, **kwargs):
        property = self.get_object()
        messages.info(request, "ИИ-инструменты теперь находятся рядом с текстовыми полями объекта.")
        return redirect(f"{reverse('edit_property', kwargs={'pk': property.pk})}#texts-pane")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = kwargs.get("form") or AIRequestForm()
        context.update(get_ai_history_context(self.object, self.request.GET.get("history_page", 1)))
        context["usage"] = {
            "total": self.object.contents.exclude(content_type="lead_reply").count(),
        }
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = AIRequestForm(request.POST)
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


class AIContentEditView(AIEnabledMixin, LoginRequiredMixin, DetailView):
    model = AIContent
    template_name = "properties/ai_content_edit.html"
    context_object_name = "ai_content"

    def get_queryset(self):
        return AIContent.objects.select_related("property").filter(property__owner=self.request.user)

    def get_template_names(self):
        if is_htmx(self.request):
            return ["properties/includes/ai_content_editor.html"]
        return [self.template_name]

    def get(self, request, *args, **kwargs):
        ai_content = self.get_object()
        messages.info(request, "История скрыта из рабочего интерфейса. Используйте ИИ непосредственно в поле текста.")
        return redirect(f"{reverse('edit_property', kwargs={'pk': ai_content.property.pk})}#texts-pane")

    def post(self, request, *args, **kwargs):
        ai_content = self.get_object()
        messages.info(request, "История скрыта из рабочего интерфейса. Используйте ИИ непосредственно в поле текста.")
        return redirect(f"{reverse('edit_property', kwargs={'pk': ai_content.property.pk})}#texts-pane")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form"] = kwargs.get("form") or AIContentEditForm(instance=self.object)
        context.update(get_ai_history_context(self.object.property))
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


class AIContentDeleteView(AIEnabledMixin, LoginRequiredMixin, View):
    def post(self, request, pk):
        ai_content = get_object_or_404(
            AIContent.objects.select_related("property"),
            pk=pk,
            property__owner=request.user,
        )
        messages.info(request, "История генераций сохранена как служебный журнал и недоступна для удаления из интерфейса.")
        return redirect(f"{reverse('edit_property', kwargs={'pk': ai_content.property.pk})}#texts-pane")


class AILeadReplyView(AIEnabledMixin, LoginRequiredMixin, View):
    def post(self, request, pk):
        lead = get_object_or_404(
            Lead.objects.select_related("property"),
            pk=pk,
            property__owner=request.user,
        )
        form = AILeadReplyForm(request.POST)
        if not form.is_valid():
            if is_htmx(request):
                return render(request, "properties/includes/lead_ai_reply.html", {"lead": lead, "error": "Выберите корректный тон ответа."})
            messages.error(request, "Выберите корректный тон ответа.")
            return redirect("lead_detail", pk=lead.pk)
        try:
            result = AIService.generate_lead_reply(lead.property, lead, form.cleaned_data["tone"])
        except AIServiceError as error:
            if is_htmx(request):
                return render(request, "properties/includes/lead_ai_reply.html", {"lead": lead, "error": str(error)})
            messages.error(request, str(error))
            return redirect("lead_detail", pk=lead.pk)
        ai_content = AIContent.objects.create(
            property=lead.property,
            lead=lead,
            content_type="lead_reply",
            tone=form.cleaned_data["tone"],
            title=lead.property.title,
            apply_target="",
            **result,
        )
        if is_htmx(request):
            return render(request, "properties/includes/lead_ai_reply.html", {"lead": lead, "ai_content": ai_content})
        messages.success(request, "ИИ подготовил ответ клиенту.")
        return redirect("lead_detail", pk=lead.pk)


@login_required
def generate_description(request, pk):
    """Preserve the old URL while moving work to the inline text editor."""
    property = get_object_or_404(Property, pk=pk, owner=request.user)
    if not settings.AI_ENABLED:
        messages.info(request, "ИИ-помощник отключён на этом стенде.")
        return redirect("property_detail", pk=pk)
    return redirect(f"{reverse('edit_property', kwargs={'pk': property.pk})}#texts-pane")
