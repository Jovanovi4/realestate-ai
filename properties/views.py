import json
import csv
import uuid
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.db.models import Count, Max, Q
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from billing.models import BillingAccount
from billing.enforcement import TariffDenied, enabled as billing_enforcement_enabled, require_allowed, require_new_action, require_resource_action
from billing.reservations import DuplicateResourceRequest, request_identity, reserved_resources
from billing.rules import Operation, PolicyService
from billing.services import AccountService

from .forms import (
    ClientDetailsForm, ClientForm, ClientInteractionForm, ClientReminderForm, LeadActionCompleteForm, LeadForm, LeadProcessingForm,
    PropertyForm, RegistrationForm, RealtorProfileForm,
)
from .agency_access import agency_for, deletable_queryset, may_manage_agency, workspace_queryset
from .models import AIContent, Client, ClientInteraction, ClientReminder, Deal, Lead, Property, PropertyImage, RealtorProfile, UserLegalAcceptance
from .services.avito_service import AvitoExportService
from .services.cian_service import CianExportService
from .services.demo_data_service import DemoDataService
from .services.lead_notification_service import LeadNotificationService
from .services.public_form_security import PublicLeadFormSecurity
from .services.presentation_service import PresentationError, PresentationService
from .services.presentation_docx_service import PresentationDocxService
from .services.image_upload_service import prepare_image_upload
from .services.csv_security import spreadsheet_safe_cell


class PropertyListView(LoginRequiredMixin, ListView):
    model = Property
    template_name = "properties/property_list.html"
    context_object_name = "properties"
    paginate_by = 10

    @staticmethod
    def price_filter(value):
        try:
            amount = Decimal(value)
        except (InvalidOperation, TypeError):
            return None
        return amount if amount.is_finite() and amount >= 0 else None

    def get_template_names(self):
        if self.request.headers.get("HX-Request") == "true":
            return ["properties/includes/property_cards.html"]
        return [self.template_name]

    def get_queryset(self):
        queryset = workspace_queryset(Property.objects, self.request.user)
        query = self.request.GET.get("q", "").strip()
        property_type = self.request.GET.get("property_type")
        deal_type = self.request.GET.get("deal_type")
        status = self.request.GET.get("status")
        min_price = self.price_filter(self.request.GET.get("min_price"))
        max_price = self.price_filter(self.request.GET.get("max_price"))
        sort = self.request.GET.get("sort", "newest")

        if query:
            queryset = queryset.filter(
                Q(title__icontains=query) | Q(marketing_headline__icontains=query) | Q(address__icontains=query)
            )
        if property_type in dict(Property.PROPERTY_TYPES):
            queryset = queryset.filter(property_type=property_type)
        if deal_type in dict(Property.DEAL_TYPE_CHOICES):
            queryset = queryset.filter(deal_type=deal_type)
        if status in dict(Property.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        if status != "archived":
            queryset = queryset.exclude(status="archived")
        if min_price is not None:
            queryset = queryset.filter(price__gte=min_price)
        if max_price is not None:
            queryset = queryset.filter(price__lte=max_price)
        ordering = {
            "newest": "-created_at",
            "oldest": "created_at",
            "updated": "-updated_at",
            "price_asc": "price",
            "price_desc": "-price",
        }
        return queryset.order_by(ordering.get(sort, ordering["newest"]))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["property_types"] = Property.PROPERTY_TYPES
        context["deal_types"] = Property.DEAL_TYPE_CHOICES
        context["status_choices"] = Property.STATUS_CHOICES
        context["filters"] = self.request.GET
        context["archived_count"] = workspace_queryset(Property.objects, self.request.user).filter(status="archived").count()
        context["sort_choices"] = [
            ("newest", "Сначала новые"),
            ("oldest", "Сначала старые"),
            ("updated", "Недавно обновлённые"),
            ("price_asc", "Цена: по возрастанию"),
            ("price_desc", "Цена: по убыванию"),
        ]
        filter_labels = {
            "q": lambda value: f"Поиск: {value.strip()}",
            "property_type": lambda value: f"Тип: {dict(Property.PROPERTY_TYPES)[value]}",
            "deal_type": lambda value: f"Сделка: {dict(Property.DEAL_TYPE_CHOICES)[value]}",
            "status": lambda value: f"Статус: {dict(Property.STATUS_CHOICES)[value]}",
            "min_price": lambda value: f"Цена от: {value}",
            "max_price": lambda value: f"Цена до: {value}",
            "sort": lambda value: f"Сортировка: {dict(context['sort_choices'])[value]}",
        }
        active_filters = []
        for key, label_for in filter_labels.items():
            value = self.request.GET.get(key, "")
            if not value or (key == "sort" and value == "newest"):
                continue
            if key == "property_type" and value not in dict(Property.PROPERTY_TYPES):
                continue
            if key == "deal_type" and value not in dict(Property.DEAL_TYPE_CHOICES):
                continue
            if key == "status" and value not in dict(Property.STATUS_CHOICES):
                continue
            if key == "sort" and value not in dict(context["sort_choices"]):
                continue
            if key in {"min_price", "max_price"} and self.price_filter(value) is None:
                continue
            if key == "q" and not value.strip():
                continue
            remaining = self.request.GET.copy()
            remaining.pop(key, None)
            remaining.pop("page", None)
            query_string = remaining.urlencode()
            active_filters.append({
                "label": label_for(value),
                "url": f"{reverse('property_list')}?{query_string}" if query_string else reverse("property_list"),
            })
        context["active_filters"] = active_filters
        params = self.request.GET.copy()
        params.pop("page", None)
        context["pagination_query"] = params.urlencode()
        profile = RealtorProfile.objects.filter(user=self.request.user).first()
        all_user_properties = workspace_queryset(Property.objects, self.request.user)
        user_properties = all_user_properties.exclude(status="archived")
        first_property = user_properties.order_by("-created_at").first()
        onboarding_items = [
            {
                "label": "Укажите контакты для заявок",
                "done": bool(profile and (profile.phone or profile.email or profile.telegram_username)),
                "url": reverse("edit_profile"),
            },
            {
                "label": "Создайте или откройте объект",
                "done": bool(first_property),
                "url": reverse("property_detail", args=[first_property.pk]) if first_property else reverse("create_property"),
            },
            {
                "label": "Добавьте фотографию объекта",
                "done": workspace_queryset(PropertyImage.objects, self.request.user, prefix="property__").exclude(property__status="archived").exists(),
                "url": reverse("upload_images", args=[first_property.pk]) if first_property else reverse("create_property"),
            },
            {
                "label": "Заполните описание",
                "done": user_properties.exclude(description="").exists(),
                "url": f"{reverse('edit_property', args=[first_property.pk])}#texts-pane" if first_property else reverse("create_property"),
            },
            {
                "label": "Опубликуйте лендинг",
                "done": user_properties.filter(landing_published=True).exists(),
                "url": f"{reverse('edit_property', args=[first_property.pk])}#landing-pane" if first_property else reverse("create_property"),
            },
        ]
        has_properties = all_user_properties.exists()
        context["has_any_properties"] = has_properties
        context["show_welcome"] = not has_properties and not self.request.GET.get("status") and not (profile and profile.onboarding_started)
        completed_steps = sum(item["done"] for item in onboarding_items)
        context["onboarding"] = {
            "visible": self.request.GET.get("status") != "archived" and (bool(first_property) or not has_properties) and (not profile or not profile.onboarding_dismissed) and completed_steps < len(onboarding_items),
            "items": onboarding_items,
            "completed": completed_steps,
            "total": len(onboarding_items),
            "can_create_demo": not has_properties and not (profile and profile.demo_data_created),
        }
        return context


class PropertyCreateView(LoginRequiredMixin, CreateView):
    model = Property
    form_class = PropertyForm
    template_name = "properties/create_property.html"
    success_url = reverse_lazy("property_list")

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["resource_request_key"] = self.request.POST.get("request_key") or str(uuid.uuid4())
        return context

    def form_valid(self, form):
        form.instance.owner = self.request.user
        agency = agency_for(self.request.user)
        form.instance.agency = agency
        account = AccountService.ensure_agency(agency) if agency else AccountService.ensure_personal(self.request.user)
        amounts = {"active_properties": 1, "total_properties": 1}
        if form.cleaned_data.get("landing_published"):
            amounts["landings"] = 1
        logo = form.cleaned_data.get("landing_logo")
        if logo and hasattr(logo, "size"):
            amounts["storage_bytes"] = logo.size
        try:
            with reserved_resources(account=account, actor=self.request.user, action=Operation.CREATE_PROPERTY,
                    amounts=amounts, **request_identity(self.request)) as reservation:
                response = super().form_valid(form)
                reservation.result_id = str(self.object.pk)
                return response
        except DuplicateResourceRequest as duplicate:
            if duplicate.reservation.status == "consumed" and duplicate.reservation.result_id:
                return redirect("edit_property", pk=duplicate.reservation.result_id)
            return self.render_to_response(self.get_context_data(form=form), status=409)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        except ValidationError:
            form.add_error(None, "Ключ запроса уже использован для других данных. Обновите форму и повторите действие.")
            return self.render_to_response(self.get_context_data(form=form), status=409)

    def get_success_url(self):
        return reverse("edit_property", kwargs={"pk": self.object.pk})


class DemoDataCreateView(LoginRequiredMixin, View):
    def post(self, request):
        try:
            property = DemoDataService.create_for_user(request.user)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("property_list")
        if property:
            messages.success(request, "Демо-кабинет готов: объект, лендинг, заявки и клиент уже созданы.")
        else:
            messages.info(request, "Демо-данные уже были созданы для этого кабинета.")
        return redirect("property_list")


class OnboardingStartManualView(LoginRequiredMixin, View):
    def post(self, request):
        profile, _ = RealtorProfile.objects.get_or_create(user=request.user)
        profile.onboarding_started = True
        profile.save(update_fields=["onboarding_started"])
        return redirect("create_property")


class OnboardingDismissView(LoginRequiredMixin, View):
    def post(self, request):
        profile, _ = RealtorProfile.objects.get_or_create(user=request.user)
        profile.onboarding_dismissed = True
        profile.onboarding_started = True
        profile.save(update_fields=["onboarding_dismissed", "onboarding_started"])
        return redirect("property_list")


class PropertyUpdateView(LoginRequiredMixin, UpdateView):
    model = Property
    form_class = PropertyForm
    template_name = "properties/edit_property.html"
    context_object_name = "property"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def get_queryset(self):
        queryset = workspace_queryset(Property.objects, self.request.user)
        return queryset.select_for_update() if self.request.method == "POST" else queryset

    def post(self, request, *args, **kwargs):
        # Serialize full form saves with archive/restore so a stale editor
        # cannot overwrite a concurrent lifecycle change after validation.
        visible = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=kwargs["pk"])
        try:
            with transaction.atomic():
                account = AccountService.ensure_for_resource(visible)
                BillingAccount.objects.select_for_update().get(pk=account.pk)
                require_allowed(PolicyService.evaluate(account, request.user, Operation.EDIT_CRM, resource=visible))
                return super().post(request, *args, **kwargs)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("edit_property", pk=kwargs["pk"])

    def form_valid(self, form):
        amounts = {}
        if not self.object.landing_published and form.cleaned_data.get("landing_published"):
            amounts["landings"] = 1
        logo = form.cleaned_data.get("landing_logo")
        if logo and hasattr(logo, "size"):
            amounts["storage_bytes"] = logo.size
        if amounts:
            account = AccountService.ensure_for_resource(self.object)
            action = Operation.PUBLISH_LANDING if "landings" in amounts else Operation.UPLOAD_FILES
            try:
                with reserved_resources(account=account, actor=self.request.user, action=action,
                        amounts=amounts, resource=self.object):
                    return super().form_valid(form)
            except TariffDenied as error:
                form.add_error(None, error.message)
                return self.render_to_response(self.get_context_data(form=form), status=403)
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("property_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["show_text_hint"] = not bool(self.object.description or self.object.short_description)
        context["show_landing_hint"] = not self.object.landing_published
        return context


class PropertyDetailView(LoginRequiredMixin, DetailView):
    model = Property
    template_name = "properties/property_detail.html"
    context_object_name = "property"

    def get_queryset(self):
        return workspace_queryset(Property.objects, self.request.user).prefetch_related("images")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        image_count = self.object.images.count()
        has_primary_image = self.object.images.filter(is_primary=True).exists()
        profile = RealtorProfile.objects.filter(user=self.object.owner).first()
        has_contacts = bool(profile and (profile.phone or profile.email or profile.telegram_username))
        detail_url = reverse("property_detail", kwargs={"pk": self.object.pk})
        edit_url = reverse("edit_property", kwargs={"pk": self.object.pk})
        upload_url = reverse("upload_images", kwargs={"pk": self.object.pk})
        tasks = []
        if not self.object.price:
            tasks.append({"text": "Укажите цену объекта", "url": edit_url})
        if not self.object.address:
            tasks.append({"text": "Добавьте адрес объекта", "url": edit_url})
        if not has_primary_image:
            tasks.append({
                "text": "Добавьте главное фото" if image_count == 0 else "Назначьте главную фотографию",
                "url": upload_url if image_count == 0 else f"{detail_url}#photos-pane",
            })
        if image_count < 3:
            tasks.append({"text": f"Добавьте ещё {3 - image_count} фото", "url": upload_url})
        if not (self.object.short_description or self.object.description):
            tasks.append({"text": "Добавьте описание объекта", "url": f"{edit_url}#texts-pane"})
        if not self.object.landing_published:
            tasks.append({"text": "Настройте и опубликуйте лендинг", "url": f"{edit_url}#landing-pane"})
        if not has_contacts:
            tasks.append({
                "text": "Укажите контакты риелтора" if self.object.owner_id == self.request.user.pk else "Попросите автора объекта указать контакты",
                "url": reverse("edit_profile") if self.object.owner_id == self.request.user.pk else reverse("agency_dashboard"),
            })
        context["readiness_tasks"] = tasks
        context["readiness_total"] = 7
        context["readiness_completed"] = context["readiness_total"] - len(tasks)
        context["image_count"] = image_count
        context["property_setup_hint"] = not (self.object.price and self.object.address and has_primary_image)
        context["can_delete"] = not self.object.agency_id or may_manage_agency(self.request.user)
        context["property_deals"] = workspace_queryset(Deal.objects.filter(property=self.object), self.request.user).select_related("client")[:5]
        return context


class LandingListView(LoginRequiredMixin, ListView):
    model = Property
    template_name = "properties/landing_list.html"
    context_object_name = "properties"
    paginate_by = 10

    def get_queryset(self):
        queryset = workspace_queryset(Property.objects, self.request.user).exclude(status="archived").annotate(landing_leads_count=Count("leads"))
        status = self.request.GET.get("status", "")
        if status == "published":
            queryset = queryset.filter(landing_published=True)
        elif status == "draft":
            queryset = queryset.filter(landing_published=False)
        return queryset.order_by("-landing_published", "-updated_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        all_landings = workspace_queryset(Property.objects, self.request.user).exclude(status="archived")
        context.update(
            {
                "selected_status": self.request.GET.get("status", ""),
                "published_count": all_landings.filter(landing_published=True).count(),
                "draft_count": all_landings.filter(landing_published=False).count(),
                "pagination_query": self._pagination_query(),
            }
        )
        return context

    def _pagination_query(self):
        params = self.request.GET.copy()
        params.pop("page", None)
        return params.urlencode()


class PropertyLandingUnpublishView(LoginRequiredMixin, View):
    def post(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        try:
            require_resource_action(request.user, property, Operation.UNPUBLISH_LANDING)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("landing_list")
        if property.landing_published:
            property.landing_published = False
            property.save(update_fields=["landing_published", "updated_at"])
            messages.success(request, "Лендинг снят с публикации.")
        next_url = request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            return redirect(next_url)
        return redirect("landing_list")


class PropertyStatusUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        status = request.POST.get("status")
        visible = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        account = AccountService.ensure_for_resource(visible)
        try:
            with transaction.atomic():
                BillingAccount.objects.select_for_update().get(pk=account.pk)
                property = get_object_or_404(workspace_queryset(Property.objects.select_for_update(), request.user), pk=pk)
                if status in dict(Property.STATUS_CHOICES):
                    if (status == "archived" or property.status == "archived") and property.agency_id and not may_manage_agency(request.user):
                        raise PermissionDenied
                    if property.status == "archived" and status != "archived":
                        with reserved_resources(account=account, actor=request.user, action=Operation.RESTORE_PROPERTY,
                                amounts={"active_properties": 1}, resource=property):
                            property.status = status
                            property.save(update_fields=["status", "updated_at"])
                    else:
                        action = Operation.ARCHIVE_PROPERTY if status == "archived" and property.status != "archived" else Operation.EDIT_CRM
                        require_allowed(PolicyService.evaluate(account, request.user, action, resource=property))
                        property.status = status
                        property.save(update_fields=["status", "updated_at"])
        except TariffDenied as error:
            if request.headers.get("HX-Request") == "true":
                return render(request, "properties/includes/property_status_badge.html", {"property": visible, "billing_error": error.message})
            messages.error(request, error.message)
            return redirect("property_detail", pk=pk)
        if request.headers.get("HX-Request") != "true":
            return redirect("property_detail", pk=property.pk)
        return render(request, "properties/includes/property_status_badge.html", {"property": property})


def check_xml_export(actor, properties):
    if not billing_enforcement_enabled():
        return
    seen = set()
    for property in properties:
        scope = (property.agency_id, None if property.agency_id else property.owner_id)
        if scope in seen:
            continue
        seen.add(scope)
        account = AccountService.ensure_for_resource(property)
        require_allowed(PolicyService.evaluate(account, actor, Operation.XML_EXPORT))


class AvitoExportView(LoginRequiredMixin, View):
    template_name = "properties/avito_export.html"

    def get_properties_and_profile(self):
        properties = workspace_queryset(Property.objects, self.request.user).filter(avito_export=True).prefetch_related("images")
        profile = RealtorProfile.objects.filter(user=self.request.user).first()
        return properties, profile

    def get(self, request):
        properties, profile = self.get_properties_and_profile()
        valid_properties, invalid_properties = AvitoExportService.get_exportable(properties, profile)
        if request.GET.get("download") == "1":
            if not valid_properties:
                return redirect("avito_export")
            try:
                check_xml_export(request.user, valid_properties)
            except TariffDenied as error:
                messages.error(request, error.message)
                return redirect("avito_export")
            xml_content = AvitoExportService.build_xml(
                valid_properties,
                profile,
                request.build_absolute_uri("/").rstrip("/"),
            )
            response = HttpResponse(xml_content, content_type="application/xml; charset=utf-8")
            response["Content-Disposition"] = 'attachment; filename="avito-properties.xml"'
            from billing.usage import observe_xml_export

            observe_xml_export(valid_properties, request.user, "avito")
            return response
        return render(
            request,
            self.template_name,
            {
                "valid_properties": valid_properties,
                "invalid_properties": invalid_properties,
                "selected_count": properties.count(),
            },
        )


class CianExportView(LoginRequiredMixin, View):
    template_name = "properties/cian_export.html"

    def get_properties_and_profile(self):
        properties = workspace_queryset(Property.objects, self.request.user).filter(cian_export=True).prefetch_related("images")
        profile = RealtorProfile.objects.filter(user=self.request.user).first()
        return properties, profile

    def get(self, request):
        properties, profile = self.get_properties_and_profile()
        valid_properties, invalid_properties = CianExportService.get_exportable(properties, profile)
        if request.GET.get("download") == "1":
            if not valid_properties:
                return redirect("cian_export")
            try:
                check_xml_export(request.user, valid_properties)
            except TariffDenied as error:
                messages.error(request, error.message)
                return redirect("cian_export")
            xml_content = CianExportService.build_xml(
                valid_properties,
                profile,
                request.build_absolute_uri("/").rstrip("/"),
            )
            response = HttpResponse(xml_content, content_type="application/xml; charset=utf-8")
            response["Content-Disposition"] = 'attachment; filename="cian-properties.xml"'
            from billing.usage import observe_xml_export

            observe_xml_export(valid_properties, request.user, "cian")
            return response
        return render(
            request,
            self.template_name,
            {
                "valid_properties": valid_properties,
                "invalid_properties": invalid_properties,
                "selected_count": properties.count(),
            },
        )


class PropertyPresentationPDFView(LoginRequiredMixin, View):
    def get(self, request, pk):
        property = get_object_or_404(
            workspace_queryset(Property.objects, request.user).prefetch_related("images"),
            pk=pk,
        )
        profile = RealtorProfile.objects.filter(user=request.user).first()
        def option_enabled(name):
            values = request.GET.getlist(name)
            return values[-1] == "1" if values else True
        try:
            photo_limit = int(request.GET.get("photos", 6))
        except (TypeError, ValueError):
            photo_limit = 6
        photo_limit = min(max(photo_limit, 0), 8)
        presentation_template = request.GET.get("template", "classic")
        if presentation_template not in {"classic", "premium"}:
            presentation_template = "classic"
        try:
            pdf_content = PresentationService.build_pdf(
                property,
                profile,
                include_facts=option_enabled("facts"),
                include_description=option_enabled("description"),
                include_benefits=option_enabled("benefits"),
                include_contacts=option_enabled("contacts"),
                photo_limit=photo_limit,
                template=presentation_template,
            )
        except PresentationError as error:
            messages.error(request, str(error))
            return redirect("property_detail", pk=property.pk)
        filename = f"presentation-{property.landing_slug or property.pk}.pdf"
        response = HttpResponse(pdf_content, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class PropertyPresentationDOCXView(LoginRequiredMixin, View):
    def get(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user).prefetch_related("images"), pk=pk)
        profile = RealtorProfile.objects.filter(user=request.user).first()
        def enabled(name):
            values = request.GET.getlist(name)
            return values[-1] == "1" if values else True
        try:
            photo_limit = min(max(int(request.GET.get("photos", 6)), 0), 8)
        except (TypeError, ValueError):
            photo_limit = 6
        content = PresentationDocxService.build_docx(property, profile, include_facts=enabled("facts"), include_description=enabled("description"), include_benefits=enabled("benefits"), include_contacts=enabled("contacts"), photo_limit=photo_limit)
        response = HttpResponse(content, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        response["Content-Disposition"] = f'attachment; filename="presentation-{property.landing_slug or property.pk}.docx"'
        return response


class PropertyDeleteView(LoginRequiredMixin, View):
    """Keep the old POST URL compatible, but never hard-delete CRM history."""

    def post(self, request, pk):
        visible = get_object_or_404(deletable_queryset(Property.objects, request.user), pk=pk)
        try:
            require_resource_action(request.user, visible, Operation.ARCHIVE_PROPERTY)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("property_detail", pk=pk)
        with transaction.atomic():
            property = get_object_or_404(deletable_queryset(Property.objects.select_for_update(), request.user), pk=pk)
            if property.status != "archived":
                property.status = "archived"
                property.save(update_fields=["status", "updated_at"])
        messages.success(request, "Объект в архиве. Фотографии, заявки и сделки сохранены.")
        return redirect(f"{reverse('property_list')}?status=archived")


class PropertyRestoreView(LoginRequiredMixin, View):
    def post(self, request, pk):
        visible = get_object_or_404(deletable_queryset(Property.objects, request.user), pk=pk)
        account = AccountService.ensure_for_resource(visible)
        try:
            with reserved_resources(account=account, actor=request.user, action=Operation.RESTORE_PROPERTY,
                    amounts={"active_properties": 1}, resource=visible):
                property = get_object_or_404(deletable_queryset(Property.objects.select_for_update(), request.user), pk=pk)
                if property.status == "archived":
                    previous = property.status_before_archive
                    property.status = previous if previous in dict(Property.STATUS_CHOICES) and previous != "archived" else "draft"
                    property.save(update_fields=["status", "updated_at"])
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("property_detail", pk=pk)
        messages.success(request, "Объект восстановлен. Публикацию лендинга включите отдельно после проверки.")
        return redirect("property_detail", pk=pk)


class RealtorProfileUpdateView(LoginRequiredMixin, UpdateView):
    model = RealtorProfile
    form_class = RealtorProfileForm
    template_name = "properties/profile_form.html"

    def get_object(self, queryset=None):
        profile, _ = RealtorProfile.objects.get_or_create(user=self.request.user)
        return profile

    def get_success_url(self):
        return reverse_lazy("edit_profile")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        properties = workspace_queryset(Property.objects, self.request.user).exclude(status="archived")
        context["statistics"] = {
            "properties": properties.count(),
            "published_landings": properties.filter(landing_published=True).count(),
            "new_leads": workspace_queryset(Lead.objects, self.request.user, prefix="property__").filter(status="new").count(),
        }
        return context


class ClientListView(LoginRequiredMixin, ListView):
    model = Client
    template_name = "properties/client_list.html"
    context_object_name = "clients"
    paginate_by = 10

    @staticmethod
    def valid_date(value):
        if not value:
            return None
        try:
            return parse_date(value)
        except ValueError:
            return None

    def get_queryset(self):
        queryset = workspace_queryset(Client.objects, self.request.user).prefetch_related("leads")
        query = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        source = self.request.GET.get("source", "")
        date_from = self.request.GET.get("date_from", "")
        date_to = self.request.GET.get("date_to", "")
        sort = self.request.GET.get("sort", "newest")
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(phone__icontains=query) | Q(preferred_area__icontains=query))
        if status in dict(Client.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        if source in dict(Client.SOURCE_CHOICES):
            queryset = queryset.filter(source=source)
        if self.valid_date(date_from):
            queryset = queryset.filter(created_at__date__gte=date_from)
        if self.valid_date(date_to):
            queryset = queryset.filter(created_at__date__lte=date_to)
        ordering = {
            "newest": "-created_at",
            "oldest": "created_at",
            "updated": "-updated_at",
            "name_asc": "name",
            "name_desc": "-name",
            "leads_desc": "-lead_count",
        }
        if sort == "leads_desc":
            queryset = queryset.annotate(lead_count=Count("leads"))
        return queryset.order_by(ordering.get(sort, ordering["newest"]))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        all_clients = workspace_queryset(Client.objects, self.request.user)
        now = timezone.now()
        start_tomorrow = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        pending_reminders = workspace_queryset(ClientReminder.objects, self.request.user).filter(client__isnull=False, is_done=False).select_related("client")
        overdue_reminders = pending_reminders.filter(due_at__lt=now)
        today_reminders = pending_reminders.filter(due_at__gte=now, due_at__lt=start_tomorrow)
        contacted = [client for client in all_clients.exclude(first_contacted_at__isnull=True) if client.first_contacted_at]
        response_hours = [
            (client.first_contacted_at - client.created_at).total_seconds() / 3600
            for client in contacted
            if client.first_contacted_at >= client.created_at
        ]
        total = all_clients.count()
        won = all_clients.filter(status="won").count()
        context.update(
            {
                "status_choices": Client.STATUS_CHOICES,
                "source_choices": Client.SOURCE_CHOICES,
                "filters": self.request.GET,
                "sort_choices": [
                    ("newest", "Сначала новые"),
                    ("oldest", "Сначала старые"),
                    ("updated", "Недавно обновлённые"),
                    ("name_asc", "Имя: А–Я"),
                    ("name_desc", "Имя: Я–А"),
                    ("leads_desc", "Больше заявок"),
                ],
                "metrics": {
                    "new_week": all_clients.filter(created_at__gte=timezone.now() - timedelta(days=7)).count(),
                    "active": all_clients.exclude(status__in=["won", "lost"]).count(),
                    "conversion": round(won / total * 100) if total else 0,
                    "response_hours": round(sum(response_hours) / len(response_hours), 1) if response_hours else None,
                },
                "overdue_reminders": overdue_reminders[:5],
                "today_reminders": today_reminders[:5],
                "overdue_reminder_count": overdue_reminders.count(),
                "today_reminder_count": today_reminders.count(),
            }
        )
        params = self.request.GET.copy()
        params.pop("page", None)
        context["pagination_query"] = params.urlencode()
        filters = self.request.GET
        status_labels = dict(Client.STATUS_CHOICES)
        source_labels = dict(Client.SOURCE_CHOICES)
        sort_labels = dict(context["sort_choices"])
        filter_items = [
            ("q", f"Поиск: {filters.get('q', '').strip()}" if filters.get("q", "").strip() else None),
            ("status", f"Этап: {status_labels[filters['status']]}" if filters.get("status") in status_labels else None),
            ("source", f"Источник: {source_labels[filters['source']]}" if filters.get("source") in source_labels else None),
            ("date_from", f"С даты: {filters['date_from']}" if self.valid_date(filters.get("date_from")) else None),
            ("date_to", f"По дату: {filters['date_to']}" if self.valid_date(filters.get("date_to")) else None),
            ("sort", f"Сортировка: {sort_labels[filters['sort']]}" if filters.get("sort") in sort_labels and filters.get("sort") != "newest" else None),
        ]
        active_filters = []
        for key, label in filter_items:
            if not label:
                continue
            remaining = filters.copy()
            remaining.pop(key, None)
            remaining.pop("page", None)
            query_string = remaining.urlencode()
            active_filters.append({
                "label": label,
                "url": f"{reverse('client_list')}?{query_string}" if query_string else reverse("client_list"),
            })
        context["active_filters"] = active_filters
        context["extra_filters_open"] = bool(filters.get("source") or filters.get("date_from") or filters.get("date_to"))
        context["has_any_clients"] = total > 0
        context["can_bulk_delete"] = not agency_for(self.request.user) or may_manage_agency(self.request.user)
        return context


class ClientCreateView(LoginRequiredMixin, CreateView):
    model = Client
    form_class = ClientForm
    template_name = "properties/client_form.html"

    def form_valid(self, form):
        form.instance.owner = self.request.user
        form.instance.agency = agency_for(self.request.user)
        try:
            require_new_action(self.request.user, Operation.EDIT_CRM, agency=form.instance.agency)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        client_scope = Client.objects.filter(agency=form.instance.agency)
        if form.instance.agency is None:
            client_scope = client_scope.filter(owner=self.request.user)
        existing_client = client_scope.filter(phone=form.cleaned_data["phone"]).first()
        if existing_client:
            form.add_error("phone", f"Клиент с этим номером уже есть в базе: {existing_client.name}.")
            return self.form_invalid(form)
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_lazy("client_detail", kwargs={"pk": self.object.pk})


class ClientDetailView(LoginRequiredMixin, UpdateView):
    model = Client
    form_class = ClientDetailsForm
    template_name = "properties/client_detail.html"
    context_object_name = "client"

    def get_queryset(self):
        return workspace_queryset(Client.objects, self.request.user)

    def form_valid(self, form):
        try:
            require_resource_action(self.request.user, self.object, Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["interaction_form"] = ClientInteractionForm()
        context["reminder_form"] = ClientReminderForm(initial={"due_at": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")})
        interaction_queryset = self.object.interactions.select_related("lead", "lead__property")
        reminder_queryset = self.object.reminders.all()
        pending_reminders = reminder_queryset.filter(is_done=False)
        lead_queryset = self.object.leads.select_related("property")
        deal_queryset = self.object.deals.select_related("property", "responsible")
        context["interactions"] = Paginator(interaction_queryset, 10).get_page(self.request.GET.get("history_page", 1))
        context["reminders"] = Paginator(reminder_queryset, 10).get_page(self.request.GET.get("reminders_page", 1))
        context["client_leads"] = Paginator(lead_queryset, 10).get_page(self.request.GET.get("leads_page", 1))
        context["client_deals"] = Paginator(deal_queryset, 10).get_page(self.request.GET.get("deals_page", 1))
        context["interactions_total"] = interaction_queryset.count()
        context["reminders_total"] = reminder_queryset.count()
        context["pending_reminders_count"] = pending_reminders.count()
        context["next_reminder"] = pending_reminders.filter(due_at__isnull=False).order_by("due_at").first()
        context["next_reminder_overdue"] = bool(context["next_reminder"] and context["next_reminder"].due_at < timezone.now())
        context["client_leads_total"] = lead_queryset.count()
        context["client_deals_total"] = deal_queryset.count()
        for page_param in ("history_page", "reminders_page", "leads_page", "deals_page"):
            params = self.request.GET.copy()
            params.pop(page_param, None)
            context[f"{page_param}_query"] = params.urlencode()
        return context

    def get_success_url(self):
        return reverse_lazy("client_detail", kwargs={"pk": self.object.pk})


class ClientStatusUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        client = get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)
        try:
            require_resource_action(request.user, client, Operation.EDIT_CRM)
        except TariffDenied as error:
            if request.headers.get("HX-Request") == "true":
                return render(request, "properties/includes/client_status_control.html", {"client": client, "billing_error": error.message})
            messages.error(request, error.message)
            return redirect("client_detail", pk=client.pk)
        status = request.POST.get("status")
        if status in dict(Client.STATUS_CHOICES) and status != client.status:
            previous_status = client.get_status_display()
            client.status = status
            client.save(update_fields=["status", "updated_at"])
            ClientInteraction.objects.create(
                client=client,
                interaction_type="status",
                text=f"Статус изменён: {previous_status} → {client.get_status_display()}.",
            )
        if request.headers.get("HX-Request") == "true":
            return render(request, "properties/includes/client_status_control.html", {"client": client})
        return redirect("client_detail", pk=client.pk)


class ClientInteractionCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        client = get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)
        try:
            require_resource_action(request.user, client, Operation.EDIT_CRM)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("client_detail", pk=client.pk)
        form = ClientInteractionForm(request.POST)
        if form.is_valid():
            interaction = form.save(commit=False)
            interaction.client = client
            interaction.save()
            if interaction.interaction_type in {"call", "message"} and not client.first_contacted_at:
                client.first_contacted_at = interaction.created_at
                client.save(update_fields=["first_contacted_at", "updated_at"])
        return redirect(f"{reverse('client_detail', kwargs={'pk': client.pk})}#client-history")


class ClientReminderCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        client = get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)
        try:
            require_resource_action(request.user, client, Operation.EDIT_CRM)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("client_detail", pk=client.pk)
        form = ClientReminderForm(request.POST)
        if form.is_valid():
            reminder = form.save(commit=False)
            reminder.client = client
            reminder.owner = request.user
            reminder.save()
            messages.success(request, "Задача добавлена.")
        else:
            messages.error(request, "Не удалось добавить задачу. Проверьте текст и срок выполнения.")
        return redirect("client_detail", pk=client.pk)


class ClientReminderCompleteView(LoginRequiredMixin, View):
    def post(self, request, pk, reminder_pk):
        client = get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)
        reminder = get_object_or_404(client.reminders, pk=reminder_pk)
        if reminder.lead_id:
            messages.info(request, "Запишите итог и назначьте следующий шаг в карточке заявки.")
            return redirect(f"{reverse('lead_detail', kwargs={'pk': reminder.lead_id})}?complete=1#lead-action")
        try:
            require_resource_action(request.user, client, Operation.EDIT_CRM)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("client_detail", pk=client.pk)
        reminder.is_done = True
        reminder.completed_at = timezone.now()
        reminder.save(update_fields=["is_done", "completed_at"])
        return redirect("client_detail", pk=client.pk)


class ClientBulkActionView(LoginRequiredMixin, View):
    def post(self, request):
        client_ids = [int(pk) for pk in request.POST.getlist("client_ids") if pk.isdigit()]
        clients = workspace_queryset(Client.objects, request.user).filter(pk__in=client_ids)
        action = request.POST.get("action")
        if action == "delete":
            if not may_manage_agency(request.user) and clients.filter(agency__isnull=False).exists():
                raise PermissionDenied
            try:
                with transaction.atomic():
                    for client in clients:
                        require_resource_action(request.user, client, Operation.EDIT_CRM)
                    clients.delete()
            except TariffDenied as error:
                messages.error(request, error.message)
            except ProtectedError:
                messages.error(request, "Клиенты не удалены: у них есть CRM-история. Сначала согласуйте отдельное удаление или обезличивание данных.")
        next_url = request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            return redirect(next_url)
        return redirect("client_list")


class ClientExportView(LoginRequiredMixin, View):
    def get(self, request):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="clients.csv"'
        response.write("\ufeff")
        writer = csv.writer(response, quoting=csv.QUOTE_ALL)
        writer.writerow(["Имя", "Телефон", "Статус", "Источник", "Бюджет", "Район", "Удобное время", "Создан"])
        for client in workspace_queryset(Client.objects, request.user).order_by("-created_at"):
            writer.writerow([spreadsheet_safe_cell(value) for value in [client.name, client.phone, client.get_status_display(), client.get_source_display(), client.budget or "", client.preferred_area, client.preferred_contact_time, client.created_at.strftime("%d.%m.%Y %H:%M")]])
        return response


class ClientBoardView(LoginRequiredMixin, View):
    def get(self, request):
        clients = workspace_queryset(Client.objects, request.user).order_by("-updated_at")
        columns = [(value, label, [client for client in clients if client.status == value]) for value, label in Client.STATUS_CHOICES]
        return render(request, "properties/client_board.html", {"columns": columns})


class LeadListView(LoginRequiredMixin, ListView):
    model = Lead
    template_name = "properties/lead_list.html"
    context_object_name = "leads"
    paginate_by = 10

    def get_template_names(self):
        if self.request.headers.get("HX-Request") == "true":
            return ["properties/includes/lead_workspace.html"]
        return [self.template_name]

    def get_queryset(self):
        queryset = workspace_queryset(Lead.objects, self.request.user, prefix="property__").select_related("property", "client", "assigned_to").prefetch_related("client__leads")
        if self.request.GET.get("assigned") == "mine":
            queryset = queryset.filter(assigned_to=self.request.user)
        queue = self.request.GET.get("queue")
        if queue == "reply":
            queryset = queryset.exclude(status="archived").filter(first_contacted_at__isnull=True)
        elif queue == "overdue":
            queryset = queryset.exclude(status="archived").filter(Lead.overdue_filter(timezone.now()))
        status = self.request.GET.get("status")
        if status in dict(Lead.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        return queryset.order_by("next_action_due_at", "pk") if queue in ("reply", "overdue") else queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["status_choices"] = Lead.STATUS_CHOICES
        requested_status = self.request.GET.get("status", "")
        selected_status = requested_status if requested_status in dict(Lead.STATUS_CHOICES) else ""
        selected_assigned = self.request.GET.get("assigned") == "mine"
        selected_queue = self.request.GET.get("queue", "")
        if selected_queue not in ("reply", "overdue"):
            selected_queue = ""
        count_queryset = workspace_queryset(Lead.objects, self.request.user, prefix="property__")
        if selected_assigned:
            count_queryset = count_queryset.filter(assigned_to=self.request.user)
        total_leads = count_queryset.count()
        reply_count = count_queryset.exclude(status="archived").filter(first_contacted_at__isnull=True).count()
        overdue_count = count_queryset.exclude(status="archived").filter(Lead.overdue_filter(timezone.now())).count()
        if selected_queue == "reply":
            count_queryset = count_queryset.exclude(status="archived").filter(first_contacted_at__isnull=True)
        elif selected_queue == "overdue":
            count_queryset = count_queryset.exclude(status="archived").filter(Lead.overdue_filter(timezone.now()))
        counts = {
            row["status"]: row["total"]
            for row in count_queryset
            .values("status")
            .annotate(total=Count("id"))
        }
        filtered_total = sum(counts.values())
        context["selected_status"] = selected_status
        context["selected_assigned"] = selected_assigned
        context["selected_queue"] = selected_queue
        context["queue_now"] = timezone.now()
        context["queue_tabs"] = [
            {"value": "", "label": "Все", "count": total_leads},
            {"value": "reply", "label": "Требует ответа", "count": reply_count},
            {"value": "overdue", "label": "Просрочено", "count": overdue_count},
        ]
        context["status_tabs"] = [
            {"value": "", "label": "Все", "count": filtered_total},
            *[
                {"value": value, "label": label, "count": counts.get(value, 0)}
                for value, label in Lead.STATUS_CHOICES
            ],
        ]
        context["has_any_leads"] = total_leads > 0
        context["can_bulk_delete"] = not agency_for(self.request.user) or may_manage_agency(self.request.user)
        context["pagination_query"] = "&".join(filter(None, [f"queue={selected_queue}" if selected_queue else "", f"status={selected_status}" if selected_status else "", "assigned=mine" if selected_assigned else ""]))
        context["demo_test_completed"] = (
            self.request.GET.get("demo_test") == "1"
            and workspace_queryset(Lead.objects, self.request.user, prefix="property__").filter(is_demo=True).exists()
        )
        return context


class LeadDetailView(LoginRequiredMixin, DetailView):
    model = Lead
    template_name = "properties/lead_detail.html"
    context_object_name = "lead"

    def get_queryset(self):
        return workspace_queryset(Lead.objects, self.request.user, prefix="property__").select_related("property", "client", "assigned_to")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["processing_form"] = kwargs.get("processing_form") or LeadProcessingForm(lead=self.object)
        context["queue_now"] = timezone.now()
        context["current_task"] = self.object.action_tasks.filter(is_done=False).first()
        context["completed_actions"] = self.object.action_tasks.filter(is_done=True).order_by("-completed_at", "-pk")
        context["complete_open"] = self.request.GET.get("complete") == "1"
        context["complete_form"] = kwargs.get("complete_form") or LeadActionCompleteForm(initial={
            "task_id": context["current_task"].pk if context["current_task"] else None,
            "next_action_due_at": timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        })
        context["latest_ai_reply"] = self.object.ai_contents.filter(content_type="lead_reply").first()
        context["can_assign"] = bool(self.object.property.agency_id and may_manage_agency(self.request.user))
        context["can_delete"] = not self.object.property.agency_id or may_manage_agency(self.request.user)
        if context["can_assign"]:
            context["team_members"] = self.object.property.agency.memberships.select_related("user", "user__realtor_profile").order_by("user__username")
        active_deal = None
        if self.object.client_id:
            active_deal = workspace_queryset(Deal.objects, self.request.user).filter(
                client_id=self.object.client_id,
                property_id=self.object.property_id,
                stage__in=Deal.ACTIVE_STAGES,
            ).first()
        context["linked_deal"] = active_deal or workspace_queryset(self.object.deals.all(), self.request.user).first()
        return context


class LeadProcessView(LoginRequiredMixin, View):
    def post(self, request, pk):
        lead = get_object_or_404(
            workspace_queryset(Lead.objects, request.user, prefix="property__").select_related("property", "client"),
            pk=pk,
        )
        if lead.status == "archived":
            messages.error(request, "Заявка в архиве. Сначала восстановите её через список заявок.")
            return redirect("lead_detail", pk=pk)
        form = LeadProcessingForm(request.POST)
        action = request.POST.get("action")
        if action not in ("save", "contacted"):
            form.add_error(None, "Неизвестное действие.")
        if form.is_valid() and action == "contacted":
            if not form.cleaned_data["contact_method"]:
                form.add_error("contact_method", "Укажите способ связи.")
            if form.cleaned_data["qualification"] == "pending":
                form.add_error("qualification", "Укажите результат квалификации или выберите «Требует уточнения».")
        if form.is_valid():
            try:
                require_resource_action(request.user, lead.property, Operation.EDIT_CRM)
            except TariffDenied as error:
                form.add_error(None, error.message)
                return self.render_invalid(request, lead, form, status=403)
            with transaction.atomic():
                lead = get_object_or_404(
                    workspace_queryset(Lead.objects.select_for_update(), request.user, prefix="property__"), pk=pk,
                )
                if lead.status == "archived":
                    messages.error(request, "Заявка уже перенесена в архив.")
                    return redirect("lead_detail", pk=pk)
                if action == "contacted" and lead.first_contacted_at:
                    messages.info(request, "Первый контакт по этой заявке уже зафиксирован.")
                    return redirect("lead_detail", pk=pk)
                lead.source = form.cleaned_data["source"]
                lead.qualification = form.cleaned_data["qualification"]
                lead.next_action = form.cleaned_data["next_action"]
                lead.next_action_due_at = form.cleaned_data["next_action_due_at"]
                fields = ["source", "qualification", "next_action", "next_action_due_at", "updated_at"]
                if action == "contacted":
                    lead.first_contacted_at = timezone.now()
                    fields.append("first_contacted_at")
                    if lead.status == "new":
                        lead.status = "read"
                        fields.append("status")
                lead.save(update_fields=fields)
                if action == "contacted" and lead.client_id:
                    Client.objects.filter(pk=lead.client_id, first_contacted_at__isnull=True).update(first_contacted_at=lead.first_contacted_at)
                    ClientInteraction.objects.create(
                        client_id=lead.client_id,
                        lead=lead,
                        interaction_type=form.cleaned_data["contact_method"],
                        text=form.cleaned_data["contact_note"] or "Первый контакт по заявке.",
                    )
            messages.success(request, "Первый контакт и следующий шаг сохранены." if action == "contacted" else "Обработка заявки обновлена.")
            return redirect("lead_detail", pk=pk)
        return self.render_invalid(request, lead, form)

    @staticmethod
    def render_invalid(request, lead, form, status=200):
        detail = LeadDetailView()
        detail.request = request
        detail.object = lead
        context = detail.get_context_data(processing_form=form)
        return render(request, detail.template_name, context, status=status)


class LeadActionCompleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        lead = get_object_or_404(
            workspace_queryset(Lead.objects, request.user, prefix="property__").select_related("property", "client"), pk=pk,
        )
        if lead.status == "archived":
            messages.error(request, "Заявка в архиве. Сначала восстановите её.")
            return redirect("lead_detail", pk=pk)
        form = LeadActionCompleteForm(request.POST)
        if not form.is_valid():
            return self.render_invalid(request, lead, form)
        try:
            require_resource_action(request.user, lead.property, Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_invalid(request, lead, form, status=403)
        with transaction.atomic():
            lead = get_object_or_404(
                workspace_queryset(Lead.objects.select_for_update(), request.user, prefix="property__"), pk=pk,
            )
            task = get_object_or_404(lead.action_tasks.select_for_update(), pk=form.cleaned_data["task_id"])
            if task.is_done:
                messages.info(request, "Это действие уже завершено.")
                return redirect("lead_detail", pk=pk)
            if lead.status == "archived":
                messages.error(request, "Заявка уже перенесена в архив.")
                return redirect("lead_detail", pk=pk)
            task.is_done = True
            task.completed_at = timezone.now()
            task.outcome_note = form.cleaned_data["outcome_note"]
            task.save(update_fields=["is_done", "completed_at", "outcome_note"])
            lead.next_action = form.cleaned_data["next_action"]
            lead.next_action_due_at = form.cleaned_data["next_action_due_at"]
            lead.save(update_fields=["next_action", "next_action_due_at", "updated_at"])
            if lead.client_id:
                ClientInteraction.objects.create(
                    client_id=lead.client_id,
                    lead=lead,
                    interaction_type="note",
                    text=(
                        f"Действие «{task.text}» завершено. Итог: {task.outcome_note} "
                        f"Следующий шаг: {lead.next_action} до {timezone.localtime(lead.next_action_due_at):%d.%m.%Y %H:%M}."
                    ),
                )
        messages.success(request, "Итог сохранён. Следующее действие назначено.")
        return redirect("lead_detail", pk=pk)

    @staticmethod
    def render_invalid(request, lead, form, status=200):
        detail = LeadDetailView()
        detail.request = request
        detail.object = lead
        context = detail.get_context_data(complete_form=form)
        return render(request, detail.template_name, context, status=status)

class LeadBulkStatusUpdateView(LoginRequiredMixin, View):
    def post(self, request):
        lead_ids = request.POST.getlist("lead_ids")
        action = request.POST.get("action", "status")
        status = request.POST.get("status")
        if not lead_ids:
            messages.error(request, "Выберите хотя бы одну заявку.")
        elif action == "delete":
            leads = workspace_queryset(Lead.objects, request.user, prefix="property__").filter(pk__in=lead_ids)
            if not may_manage_agency(request.user) and leads.filter(property__agency__isnull=False).exists():
                raise PermissionDenied
            archived_count = 0
            try:
                with transaction.atomic():
                    for lead in leads.select_for_update().select_related("property").exclude(status="archived"):
                        require_resource_action(request.user, lead.property, Operation.EDIT_CRM)
                        lead.status = "archived"
                        lead.save(update_fields=["status", "updated_at"])
                        archived_count += 1
            except TariffDenied as error:
                messages.error(request, error.message)
            else:
                messages.success(request, f"В архив перенесено заявок: {archived_count}.")
        elif status not in dict(Lead.STATUS_CHOICES):
            messages.error(request, "Выберите корректный статус обработки.")
        else:
            leads = workspace_queryset(Lead.objects, request.user, prefix="property__").filter(pk__in=lead_ids)
            updated = 0
            try:
                with transaction.atomic():
                    for lead in leads.select_related("property").exclude(status=status):
                        require_resource_action(request.user, lead.property, Operation.EDIT_CRM)
                        lead.status = status
                        lead.save(update_fields=["status", "updated_at"])
                        updated += 1
            except TariffDenied as error:
                messages.error(request, error.message)
            else:
                messages.success(request, f"Статус обновлён у {updated} заявок.")

        next_url = request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            return redirect(next_url)
        return redirect("lead_list")


class LeadDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        try:
            with transaction.atomic():
                lead = get_object_or_404(deletable_queryset(Lead.objects.select_for_update().select_related("property"), request.user, prefix="property__"), pk=pk)
                require_resource_action(request.user, lead.property, Operation.EDIT_CRM)
                if lead.status != "archived":
                    lead.status = "archived"
                    lead.save(update_fields=["status", "updated_at"])
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("lead_detail", pk=pk)
        messages.success(request, "Заявка перенесена в архив. Связи со сделками и история сохранены.")
        return redirect(f"{reverse('lead_list')}?status=archived")


class PropertyImageUploadView(LoginRequiredMixin, View):
    template_name = "properties/upload_images.html"

    class CapacityChanged(Exception):
        pass

    def render_upload(self, request, property, error=None):
        return render(request, self.template_name, {
            "property": property,
            "upload_error": error,
            "max_image_mb": settings.PROPERTY_IMAGE_MAX_MB,
            "max_upload_count": settings.PROPERTY_IMAGES_PER_UPLOAD,
            "max_image_count": settings.PROPERTY_IMAGES_MAX_COUNT,
            "image_count": property.images.count(),
            "resource_request_key": request.POST.get("request_key") or str(uuid.uuid4()),
        }, status=400 if error else 200)

    def get(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        return self.render_upload(request, property)

    def post(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        uploads = request.FILES.getlist("images")
        if not uploads:
            return self.render_upload(request, property, "Выберите хотя бы одну фотографию.")
        if sum(len(request.FILES.getlist(key)) for key in request.FILES) > settings.PROPERTY_IMAGES_PER_UPLOAD:
            return self.render_upload(request, property, f"За одну загрузку можно добавить не более {settings.PROPERTY_IMAGES_PER_UPLOAD} фотографий.")
        if property.images.count() + len(uploads) > settings.PROPERTY_IMAGES_MAX_COUNT:
            return self.render_upload(request, property, f"У объекта может быть не более {settings.PROPERTY_IMAGES_MAX_COUNT} фотографий.")
        try:
            # Validate the entire batch before writing any files or DB records.
            prepared = [prepare_image_upload(upload) for upload in uploads]
        except ValidationError as error:
            return self.render_upload(request, property, " ".join(error.messages))
        account = AccountService.ensure_for_resource(property)
        try:
            with reserved_resources(account=account, actor=request.user, action=Operation.UPLOAD_FILES,
                    amounts={"storage_bytes": sum(image.size for image in prepared)}, resource=property,
                    **request_identity(request)) as reservation:
                # Serialize simultaneous uploads to the same property in PostgreSQL.
                property = workspace_queryset(Property.objects, request.user).select_for_update().get(pk=pk)
                if property.images.count() + len(prepared) > settings.PROPERTY_IMAGES_MAX_COUNT:
                    raise self.CapacityChanged
                max_order = property.images.aggregate(max_order=Max("order"))["max_order"]
                next_order = (max_order if max_order is not None else -1) + 1
                has_primary = property.images.filter(is_primary=True).exists()
                for image in prepared:
                    PropertyImage.objects.create(property=property, image=image, order=next_order, is_primary=not has_primary)
                    has_primary = True
                    next_order += 1
                reservation.result_id = str(property.pk)
        except self.CapacityChanged:
            return self.render_upload(request, property, f"У объекта может быть не более {settings.PROPERTY_IMAGES_MAX_COUNT} фотографий.")
        except DuplicateResourceRequest as duplicate:
            if duplicate.reservation.status != "consumed":
                return self.render_upload(request, property, "Загрузка уже выполняется. Подождите и обновите галерею.")
            return redirect("property_detail", pk=property.pk)
        except TariffDenied as error:
            return self.render_upload(request, property, error.message)
        except ValidationError:
            return self.render_upload(request, property, "Ключ загрузки уже использован для других файлов. Обновите страницу и повторите действие.")
        return redirect("property_detail", pk=property.pk)


class PropertyImagePrimaryView(LoginRequiredMixin, View):
    def post(self, request, pk, image_pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        image = get_object_or_404(property.images, pk=image_pk)
        try:
            require_resource_action(request.user, property, Operation.EDIT_CRM)
        except TariffDenied as error:
            if request.headers.get("HX-Request") == "true":
                return render(request, "properties/includes/property_gallery_card.html", {"property": property, "billing_error": error.message})
            messages.error(request, error.message)
            return redirect("property_detail", pk=pk)
        with transaction.atomic():
            for current in property.images.filter(is_primary=True).exclude(pk=image.pk):
                current.is_primary = False
                current.save(update_fields=["is_primary"])
            image.is_primary = True
            image.save(update_fields=["is_primary"])
        if request.headers.get("HX-Request") == "true":
            return render(request, "properties/includes/property_gallery_card.html", {"property": property})
        return redirect("property_detail", pk=property.pk)


class PropertyImageDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk, image_pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        image = get_object_or_404(property.images, pk=image_pk)
        try:
            require_resource_action(request.user, property, Operation.REMOVE_FILES)
        except TariffDenied as error:
            if request.headers.get("HX-Request") == "true":
                return render(request, "properties/includes/property_gallery_card.html", {"property": property, "billing_error": error.message})
            messages.error(request, error.message)
            return redirect("property_detail", pk=pk)
        image_file = image.image
        saved_name = image_file.name if image_file else ""
        image.delete()
        if image_file:
            try:
                image_file.delete(save=False)
            finally:
                # A missing DB reference alone does not prove deletion from storage.
                from billing.inventory import refresh_saved_file
                from billing.usage import attempt, ensure_scope

                account = attempt(lambda: ensure_scope({"agency_id": property.agency_id, "personal_user_id": None if property.agency_id else property.owner_id}))
                if account:
                    refresh_saved_file(account, saved_name)
        property.ensure_primary_image()
        if request.headers.get("HX-Request") == "true":
            return render(request, "properties/includes/property_gallery_card.html", {"property": property})
        return redirect("property_detail", pk=property.pk)


class PropertyImageReorderView(LoginRequiredMixin, View):
    def post(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        try:
            require_resource_action(request.user, property, Operation.EDIT_CRM)
        except TariffDenied as error:
            return JsonResponse({"ok": False, "error": error.message, "code": error.code}, status=403)
        try:
            image_ids = json.loads(request.body).get("image_ids", [])
            image_ids = [int(image_id) for image_id in image_ids]
        except (TypeError, ValueError, json.JSONDecodeError):
            return JsonResponse({"ok": False, "error": "Некорректный порядок фотографий."}, status=400)

        existing_images = list(property.images.all())
        existing_ids = {image.pk for image in existing_images}
        if len(image_ids) != len(existing_ids) or len(image_ids) != len(set(image_ids)) or set(image_ids) != existing_ids:
            return JsonResponse({"ok": False, "error": "Список фотографий изменился. Обновите страницу."}, status=400)

        images_by_id = {image.pk: image for image in existing_images}
        for position, image_id in enumerate(image_ids):
            images_by_id[image_id].order = position
        for image in existing_images:
            image.save(update_fields=["order"])
        return JsonResponse({"ok": True})


def register(request):
    if request.user.is_authenticated:
        return redirect("property_list")

    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save()
            UserLegalAcceptance.objects.create(
                user=user,
                terms_accepted_at=timezone.now(),
                terms_version=settings.SERVICE_TERMS_VERSION,
                personal_data_consent_at=timezone.now(),
                personal_data_consent_version=settings.PERSONAL_DATA_CONSENT_VERSION,
            )
        login(request, user)
        return redirect("property_list")
    return render(request, "registration/register.html", {"form": form})


def legal_context(document_version):
    return {
        "operator_name": settings.LEGAL_OPERATOR_NAME,
        "operator_inn": settings.LEGAL_OPERATOR_INN,
        "operator_ogrn": settings.LEGAL_OPERATOR_OGRN,
        "operator_address": settings.LEGAL_OPERATOR_ADDRESS,
        "privacy_email": settings.LEGAL_PRIVACY_EMAIL,
        "data_storage_location": settings.LEGAL_DATA_STORAGE_LOCATION,
        "document_version": document_version,
    }


def privacy_policy(request):
    return render(request, "properties/legal/privacy_policy.html", legal_context(settings.PERSONAL_DATA_POLICY_VERSION))


def personal_data_consent(request):
    return render(request, "properties/legal/personal_data_consent.html", legal_context(settings.PERSONAL_DATA_CONSENT_VERSION))


def service_terms(request):
    return render(request, "properties/legal/service_terms.html", legal_context(settings.SERVICE_TERMS_VERSION))


class PublicLandingView(View):
    template_names = {
        "classic": "properties/public_landing.html",
        "modern": "properties/public_landing_modern.html",
        "premium": "properties/public_landing_premium.html",
    }

    def get_template_name(self, property):
        return self.template_names.get(property.landing_template, self.template_names["classic"])

    @staticmethod
    def get_landing_order(property):
        valid_keys = [key for key, _ in Property.LANDING_BLOCKS]
        order = property.landing_block_order or valid_keys
        if set(order) != set(valid_keys) or len(order) != len(valid_keys):
            order = valid_keys
        return {block: index + 1 for index, block in enumerate(order)}

    def get_context(self, property, profile, form, **extra):
        valid_keys = [key for key, _ in Property.LANDING_BLOCKS]
        enabled = property.landing_enabled_blocks or {}
        return {
            "property": property,
            "profile": profile,
            "form": form,
            "landing_order": self.get_landing_order(property),
            "landing_enabled": {key: bool(enabled.get(key, True)) for key in valid_keys},
            "turnstile_enabled": settings.TURNSTILE_ENABLED,
            "turnstile_site_key": settings.TURNSTILE_SITE_KEY,
            "demo_landing": property.is_demo,
            **extra,
        }

    def get_property(self, slug):
        return get_object_or_404(
            Property.objects.exclude(status="archived").select_related("owner").prefetch_related("images"),
            landing_slug=slug,
            landing_published=True,
        )

    def access_response(self, property):
        if not billing_enforcement_enabled():
            return None
        try:
            account = AccountService.for_resource(property)
            decision = PolicyService.evaluate(account, None, Operation.PUBLIC_LANDING, resource=property)
        except BillingAccount.DoesNotExist:
            decision = None
        if decision is None or not decision.allowed:
            return render(self.request, "properties/public_landing_paused.html", status=503)
        return None

    def get(self, request, slug):
        property = self.get_property(slug)
        paused = self.access_response(property)
        if paused is not None:
            return paused
        profile = RealtorProfile.objects.filter(user=property.owner).first()
        return render(request, self.get_template_name(property), self.get_context(property, profile, LeadForm(property=property)))

    def post(self, request, slug):
        property = self.get_property(slug)
        paused = self.access_response(property)
        if paused is not None:
            return paused
        profile = RealtorProfile.objects.filter(user=property.owner).first()
        if request.POST.get("website", "").strip():
            return render(request, self.get_template_name(property), self.get_context(property, profile, LeadForm(property=property), sent=True))
        if not PublicLeadFormSecurity.allow_submission(request, property.pk):
            form = LeadForm(request.POST, property=property)
            form.add_error(None, "Слишком много попыток. Подождите несколько минут и попробуйте снова.")
            return render(request, self.get_template_name(property), self.get_context(property, profile, form))
        form = LeadForm(request.POST, property=property)
        if form.is_valid():
            if not PublicLeadFormSecurity.verify_turnstile(request):
                form.add_error(None, "Не удалось подтвердить, что вы не робот. Попробуйте ещё раз.")
                return render(request, self.get_template_name(property), self.get_context(property, profile, form))
            lead = form.save(commit=False)
            lead.property = property
            lead.interest_type = "long_rent" if property.deal_type == "rent" else "buy"
            lead.personal_data_consent_at = timezone.now()
            lead.personal_data_consent_version = settings.PERSONAL_DATA_CONSENT_VERSION
            lead.is_demo = property.is_demo
            client_lookup = {"agency": property.agency, "phone": form.cleaned_data["phone"]}
            if property.agency_id is None:
                client_lookup["owner"] = property.owner
            client, created = Client.objects.get_or_create(
                **client_lookup,
                defaults={"owner": property.owner, "name": form.cleaned_data["name"], "source": "landing"},
            )
            if not created and not client.name and form.cleaned_data["name"]:
                client.name = form.cleaned_data["name"]
                client.save(update_fields=["name", "updated_at"])
            lead.client = client
            lead.save()
            ClientInteraction.objects.create(
                client=client,
                lead=lead,
                interaction_type="note",
                text=f"Новая заявка с лендинга «{property.title}» · {lead.get_contact_purpose_display()} · {lead.get_interest_type_display()}.",
            )
            lead_url = request.build_absolute_uri(reverse("lead_detail", kwargs={"pk": lead.pk}))
            if not property.is_demo:
                transaction.on_commit(
                    lambda lead=lead, profile=profile, lead_url=lead_url: LeadNotificationService.notify_new_lead(
                        lead, profile, lead_url
                    )
                )
            return render(
                request,
                self.get_template_name(property),
                self.get_context(
                    property,
                    profile,
                    LeadForm(property=property),
                    sent=True,
                    demo_test_completed=property.is_demo and request.user == property.owner,
                ),
            )
        return render(request, self.get_template_name(property), self.get_context(property, profile, form))


@method_decorator(xframe_options_sameorigin, name="dispatch")
class PropertyLandingPreviewView(LoginRequiredMixin, PublicLandingView):
    def get(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects.select_related("owner").prefetch_related("images"), request.user), pk=pk)
        profile = RealtorProfile.objects.filter(user=property.owner).first()
        return render(request, self.get_template_name(property), self.get_context(property, profile, LeadForm(property=property), preview=True))

    def post(self, request, pk):
        return self.get(request, pk)
