import hashlib
import json
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.core.paginator import Paginator
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView
from billing.enforcement import TariffDenied, require_resource_action
from billing.rules import Operation
from billing.usage import DuplicateAIRequest, observe_ai_generation

from .forms import AIContentEditForm, AILeadReplyForm, AIRequestForm
from .agency_access import workspace_queryset
from .models import AIContent, Lead, Property, RealtorProfile
from .services.ai_service import AIService, AIServiceError


def is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def ai_request_identity(request):
    """Bind a client retry key to the exact endpoint and submitted parameters."""
    raw = request.headers.get("Idempotency-Key") or request.POST.get("request_key")
    try:
        key = uuid.UUID(raw) if raw else uuid.uuid4()
    except (TypeError, ValueError):
        raise ValidationError("Некорректный ключ запроса.")
    payload = [(name, request.POST.getlist(name)) for name in sorted(request.POST) if name not in {"csrfmiddlewaretoken", "request_key"}]
    fingerprint = hashlib.sha256(json.dumps([request.path, payload], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"request_key": key, "request_fingerprint": fingerprint}


def ai_retry_error(error):
    if isinstance(error, DuplicateAIRequest):
        return "Запрос уже выполняется или его результат требует проверки. Повторите позже."
    return "Этот ключ уже использован для другого запроса. Обновите страницу и повторите действие."


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


def save_generated_contents(property, tone, results, *, lead=None):
    contents = []
    for result in results:
        payload = dict(result)
        content_type = payload.pop("content_type")
        apply_target = payload.pop("apply_target", DEFAULT_TARGETS.get(content_type, ""))
        contents.append(AIContent.objects.create(property=property, lead=lead, content_type=content_type,
            tone=tone, title=property.title, apply_target=apply_target, **payload))
    return contents


class AIEnabledMixin:
    """Keep AI endpoints unavailable when a deployment disables the feature."""

    def dispatch(self, request, *args, **kwargs):
        if not settings.AI_ENABLED:
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class AIInlineGenerateView(AIEnabledMixin, LoginRequiredMixin, View):
    """Return one generated text for the inline editor without saving it to the property."""

    def post(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        target = request.POST.get("target", "")
        tone = request.POST.get("tone", "business")
        mode = request.POST.get("mode", "generate")
        source_text = request.POST.get("source_text", "").strip()
        improvement = request.POST.get("improvement", "")

        if not is_inline_target(target):
            return JsonResponse({"error": "Для этого поля генерация пока недоступна."}, status=400)
        if tone not in dict(AIContent.TONE_CHOICES):
            return JsonResponse({"error": "Выберите корректный тон текста."}, status=400)

        if mode == "improve" and not source_text:
            return JsonResponse({"error": "Введите текст, который нужно улучшить."}, status=400)
        if mode not in {"generate", "improve"}:
            return JsonResponse({"error": "Неизвестное действие ИИ."}, status=400)

        def generate():
            if mode == "improve":
                content_type = "benefits" if target.startswith("landing_benefit_") else AIService.INLINE_FIELD_INSTRUCTIONS[target][0]
                result = AIService.improve_text(property, content_type, tone, improvement, source_text)
            else:
                content_type, result = AIService.generate_inline_content(property, target, tone)
            return [{**result, "content_type": content_type,
                "apply_target": target if target in dict(AIContent.APPLY_TARGET_CHOICES) else ""}]

        action = "improve" if mode == "improve" else "full_description" if target == "description" else "short_text"
        try:
            contents = observe_ai_generation(resource=property, actor=request.user, ai_action=action,
                generate=generate, save=lambda results: save_generated_contents(property, tone, results),
                **ai_request_identity(request))
        except TariffDenied as error:
            return JsonResponse({"error": error.message, "code": error.code}, status=403)
        except (DuplicateAIRequest, ValidationError) as error:
            return JsonResponse({"error": ai_retry_error(error)}, status=409)
        except AIServiceError as error:
            return JsonResponse({"error": str(error)}, status=400)
        return JsonResponse({"content": contents[0].content})


class AIBundleGenerateView(AIEnabledMixin, LoginRequiredMixin, View):
    """Generate a review-first set of texts for one property."""

    def post(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        tone = request.POST.get("tone", "business")
        bundle_type = request.POST.get("bundle", "package")
        if tone not in dict(AIContent.TONE_CHOICES):
            return JsonResponse({"error": "Выберите корректный тон текста."}, status=400)
        if bundle_type not in {"package", "landing"}:
            return JsonResponse({"error": "Неизвестный сценарий генерации."}, status=400)
        try:
            generated = observe_ai_generation(resource=property, actor=request.user, ai_action=bundle_type,
                generate=lambda: AIService.generate_bundle(property, tone, bundle_type),
                save=lambda results: save_generated_contents(property, tone, results), **ai_request_identity(request))
        except TariffDenied as error:
            return JsonResponse({"error": error.message, "code": error.code}, status=403)
        except (DuplicateAIRequest, ValidationError) as error:
            return JsonResponse({"error": ai_retry_error(error)}, status=409)
        except AIServiceError as error:
            return JsonResponse({"error": str(error)}, status=400)
        contents = [{"target": content.apply_target, "content": content.content} for content in generated]
        return JsonResponse({"contents": contents})


class AIAssistantView(AIEnabledMixin, LoginRequiredMixin, DetailView):
    model = Property
    template_name = "properties/ai_assistant.html"
    context_object_name = "property"

    def get_queryset(self):
        return workspace_queryset(Property.objects, self.request.user)

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
        def generate():
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
            return results

        action = mode if mode in {"audit", "package", "landing", "improve"} else "full_description" if form.cleaned_data["content_type"] in {"full_description", "description"} else "short_text"
        try:
            contents = observe_ai_generation(resource=self.object, actor=request.user, ai_action=action,
                generate=generate, save=lambda results: save_generated_contents(self.object, form.cleaned_data["tone"], results),
                **ai_request_identity(request))
        except TariffDenied as error:
            if is_htmx(request):
                return render(request, "properties/includes/ai_generation_error.html", {"message": error.message, "billing_denied": True})
            messages.error(request, error.message)
            return self.render_to_response(self.get_context_data(form=form))
        except (DuplicateAIRequest, ValidationError) as error:
            if is_htmx(request):
                return render(request, "properties/includes/ai_generation_error.html", {"message": ai_retry_error(error)}, status=409)
            messages.error(request, ai_retry_error(error))
            return self.render_to_response(self.get_context_data(form=form))
        except AIServiceError as error:
            if is_htmx(request):
                return render(request, "properties/includes/ai_generation_error.html", {"message": str(error)})
            messages.error(request, str(error))
            return self.render_to_response(self.get_context_data(form=form))

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
        return workspace_queryset(AIContent.objects.select_related("property"), self.request.user, prefix="property__")

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
        try:
            require_resource_action(request.user, self.object.property, Operation.EDIT_CRM)
        except TariffDenied as error:
            if is_htmx(request):
                return render(request, "properties/includes/ai_generation_error.html", {"message": error.message, "billing_denied": True})
            messages.error(request, error.message)
            return redirect("edit_property", pk=self.object.property.pk)
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
            workspace_queryset(AIContent.objects.select_related("property"), request.user, prefix="property__"),
            pk=pk,
        )
        messages.info(request, "История генераций сохранена как служебный журнал и недоступна для удаления из интерфейса.")
        return redirect(f"{reverse('edit_property', kwargs={'pk': ai_content.property.pk})}#texts-pane")


class AILeadReplyView(AIEnabledMixin, LoginRequiredMixin, View):
    def post(self, request, pk):
        lead = get_object_or_404(
            workspace_queryset(Lead.objects.select_related("property"), request.user, prefix="property__"),
            pk=pk,
        )
        form = AILeadReplyForm(request.POST)
        if not form.is_valid():
            if is_htmx(request):
                return render(request, "properties/includes/lead_ai_reply.html", {"lead": lead, "error": "Выберите корректный тон ответа."})
            messages.error(request, "Выберите корректный тон ответа.")
            return redirect("lead_detail", pk=lead.pk)
        try:
            contents = observe_ai_generation(resource=lead.property, actor=request.user, ai_action="lead_reply",
                generate=lambda: [{**AIService.generate_lead_reply(lead.property, lead, form.cleaned_data["tone"]), "content_type": "lead_reply", "apply_target": ""}],
                save=lambda results: save_generated_contents(lead.property, form.cleaned_data["tone"], results, lead=lead),
                **ai_request_identity(request))
        except TariffDenied as error:
            if is_htmx(request):
                return render(request, "properties/includes/lead_ai_reply.html", {"lead": lead, "error": error.message, "billing_denied": True})
            messages.error(request, error.message)
            return redirect("lead_detail", pk=lead.pk)
        except (DuplicateAIRequest, ValidationError) as error:
            if is_htmx(request):
                return render(request, "properties/includes/lead_ai_reply.html", {"lead": lead, "error": ai_retry_error(error)}, status=409)
            messages.error(request, ai_retry_error(error))
            return redirect("lead_detail", pk=lead.pk)
        except AIServiceError as error:
            if is_htmx(request):
                return render(request, "properties/includes/lead_ai_reply.html", {"lead": lead, "error": str(error)})
            messages.error(request, str(error))
            return redirect("lead_detail", pk=lead.pk)
        ai_content = contents[0]
        if is_htmx(request):
            return render(request, "properties/includes/lead_ai_reply.html", {"lead": lead, "ai_content": ai_content})
        messages.success(request, "ИИ подготовил ответ клиенту.")
        return redirect("lead_detail", pk=lead.pk)


@login_required
def generate_description(request, pk):
    """Preserve the old URL while moving work to the inline text editor."""
    property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
    if not settings.AI_ENABLED:
        messages.info(request, "ИИ-помощник отключён на этом стенде.")
        return redirect("property_detail", pk=pk)
    return redirect(f"{reverse('edit_property', kwargs={'pk': property.pk})}#texts-pane")
