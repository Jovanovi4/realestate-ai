import base64
import copy
import json
import csv
import uuid
from decimal import Decimal, InvalidOperation
from datetime import timedelta
from urllib.parse import parse_qs, urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.db.models import Case, CharField, Count, DateTimeField, F, IntegerField, Max, Q, Value, When
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
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
from billing.enforcement import TariffDenied, enabled as billing_enforcement_enabled, require_allowed, require_new_action, require_resource_action, telegram_publication_decision
from billing.reservations import DuplicateResourceRequest, request_identity, reserved_resources
from billing.rules import Operation, PolicyService
from billing.services import AccountService

from .forms import (
    ClientDetailsForm, ClientForm, ClientInteractionForm, ClientPropertyReactionForm, ClientPropertySuggestionForm, ClientReminderForm, ClientSearchForm, LeadActionCompleteForm, LeadDeclineForm, LeadForm, LeadProcessingForm, OwnerContactForm, OwnerInteractionForm,
    PropertyForm, PublicOwnerRequestForm, PublicSearchRequestForm, RegistrationForm, RealtorProfileForm,
)
from .agency_access import agency_for, deletable_queryset, may_manage_agency, workspace_queryset
from .board_pagination import paged_board_columns
from .models import AIContent, Agency, AgencyMembership, Client, ClientInteraction, ClientReminder, Deal, InboundEvent, LandingDailyView, Lead, OwnerInquiry, Property, PropertyImage, PropertyOwnerLink, PublicIntakeLink, RealtorProfile, TelegramFeed, UserLegalAcceptance
from .services.avito_service import AvitoExportService
from .services.cian_service import CianExportService
from .services.demo_data_service import DemoDataService
from .services.landing_copy import landing_heading, landing_trust_copy
from .services.lead_notification_service import LeadNotificationService
from .services.lead_outcome_service import convert_lead_to_deal
from .services.telegram_public_service import feed_for, feed_link, property_link, public_bot_ready
from .services.public_form_security import PublicLeadFormSecurity
from .public_intake_views import submit_public_request
from .services.presentation_service import PresentationError, PresentationService
from .services.presentation_docx_service import PresentationDocxService
from .services.image_upload_service import prepare_image_upload
from .services.csv_security import spreadsheet_safe_cell
from .services.client_relationship_service import activate_client_relationship, change_client_relationship, relationship_change_error
from .services.client_merge_service import possible_matches
from .services.crm_analytics_service import ANALYTICS_TZ, AnalyticsPeriod
from .landing_colors import landing_custom_palette
from .landing_presets import editor_preset_context


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
        owner_client_id = self.request.GET.get("owner_client", "")
        if owner_client_id and (not owner_client_id.isascii() or not owner_client_id.isdigit()):
            raise Http404
        self.owner_filter_client = get_object_or_404(
            workspace_queryset(Client.objects, self.request.user), pk=owner_client_id,
        ) if owner_client_id else None
        if self.owner_filter_client:
            queryset = queryset.filter(owner_links__client=self.owner_filter_client)
        query = self.request.GET.get("q", "").strip()
        segment = self.request.GET.get("segment")
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
        if segment == "residential":
            queryset = queryset.filter(property_type__in=Property.RESIDENTIAL_TYPES)
        elif segment == "commercial":
            queryset = queryset.filter(property_type__in=Property.COMMERCIAL_TYPES)
        if property_type in dict(Property.PROPERTY_TYPES):
            queryset = queryset.filter(property_type=property_type)
        if deal_type in dict(Property.DEAL_TYPE_CHOICES):
            queryset = queryset.filter(deal_type=deal_type)
        if status in dict(Property.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        if status != "archived" and not self.owner_filter_client:
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
        context["owner_filter_client"] = self.owner_filter_client
        context["property_types"] = Property.PROPERTY_TYPES
        context["segment_choices"] = [("residential", "Жилая"), ("commercial", "Коммерческая")]
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
            "segment": lambda value: f"Сегмент: {dict(context['segment_choices'])[value]}",
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
            if key == "segment" and value not in dict(context["segment_choices"]):
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
        if self.owner_filter_client:
            remaining = self.request.GET.copy()
            remaining.pop("owner_client", None)
            remaining.pop("page", None)
            query_string = remaining.urlencode()
            active_filters.insert(0, {
                "label": f"Собственник: {self.owner_filter_client.name}",
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
        context["show_welcome"] = not has_properties and not self.owner_filter_client and not self.request.GET.get("status") and not (profile and profile.onboarding_started)
        completed_steps = sum(item["done"] for item in onboarding_items)
        context["onboarding"] = {
            "visible": not self.owner_filter_client and self.request.GET.get("status") != "archived" and (bool(first_property) or not has_properties) and (not profile or not profile.onboarding_dismissed) and completed_steps < len(onboarding_items),
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

    def inquiry_for_request(self):
        inquiry_id = self.request.POST.get("owner_inquiry") if self.request.method == "POST" else self.request.GET.get("owner_inquiry")
        if not inquiry_id:
            return None
        if not inquiry_id.isdigit():
            raise Http404
        return get_object_or_404(workspace_queryset(OwnerInquiry.objects, self.request.user), pk=inquiry_id)

    def get(self, request, *args, **kwargs):
        inquiry = self.inquiry_for_request()
        if inquiry and (inquiry.status == "archived" or inquiry.result != "agreed" or inquiry.property_id):
            return redirect("owner_inquiry_detail", pk=inquiry.pk)
        return super().get(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        inquiry = self.inquiry_for_request()
        if inquiry and inquiry.status != "archived" and inquiry.result == "agreed" and not inquiry.property_id:
            initial.update({
                "title": inquiry.address or f"Объект собственника {inquiry.client.name}",
                "address": inquiry.address, "description": inquiry.description,
                "price": inquiry.expected_price, "currency": inquiry.currency,
                "deal_type": inquiry.deal_type, "status": "draft",
            })
        return initial

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["resource_request_key"] = self.request.POST.get("request_key") or str(uuid.uuid4())
        context["owner_inquiry"] = self.inquiry_for_request()
        context.update(editor_preset_context(self.request.user))
        return context

    def form_valid(self, form):
        inquiry = self.inquiry_for_request()
        if inquiry and (inquiry.status == "archived" or inquiry.result != "agreed" or inquiry.property_id):
            form.add_error(None, "Из этого обращения пока нельзя создать объект.")
            return self.render_to_response(self.get_context_data(form=form), status=409)
        form.instance.owner = inquiry.assigned_to if inquiry else self.request.user
        agency = inquiry.agency if inquiry else agency_for(self.request.user)
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
                if inquiry:
                    inquiry = OwnerInquiry.objects.select_for_update().get(pk=inquiry.pk)
                    if inquiry.status == "archived" or inquiry.result != "agreed" or inquiry.property_id:
                        raise ValidationError("Объект из этого обращения уже создан или сотрудничество не согласовано.")
                response = super().form_valid(form)
                if inquiry:
                    PropertyOwnerLink.objects.create(
                        property=self.object, client=inquiry.client,
                        role="landlord" if inquiry.deal_type == "rent" else "owner",
                    )
                    inquiry.property = self.object
                    inquiry.save(update_fields=["property", "updated_at"])
                reservation.result_id = str(self.object.pk)
                return response
        except DuplicateResourceRequest as duplicate:
            if duplicate.reservation.status == "consumed" and duplicate.reservation.result_id:
                return redirect("edit_property", pk=duplicate.reservation.result_id)
            return self.render_to_response(self.get_context_data(form=form), status=409)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        except ValidationError as error:
            form.add_error(None, error.messages[0] if inquiry else "Ключ запроса уже использован для других данных. Обновите форму и повторите действие.")
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
        context.update(editor_preset_context(self.request.user, self.object))
        return context


class PropertyDetailView(LoginRequiredMixin, DetailView):
    model = Property
    template_name = "properties/property_detail.html"
    context_object_name = "property"

    def get_queryset(self):
        return workspace_queryset(Property.objects, self.request.user).prefetch_related("images")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        owner_links = list(self.object.owner_links.select_related("client").all())
        today = timezone.localdate()
        for link in owner_links:
            link.agreement_expired = bool(link.agreement_expires_on and link.agreement_expires_on < today)
        context["owner_links"] = owner_links
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
        if not self.object.area:
            tasks.append({"text": "Укажите площадь объекта", "url": edit_url})
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
        context["telegram_bot_ready"] = public_bot_ready()
        context["telegram_bot_username"] = settings.TELEGRAM_PUBLIC_BOT_USERNAME
        context["telegram_property_url"] = property_link(self.object)
        context["telegram_publish_denial"] = ""
        feed = TelegramFeed.objects.filter(agency=self.object.agency).first() if self.object.agency_id else TelegramFeed.objects.filter(owner=self.object.owner).first()
        if context["telegram_bot_ready"]:
            feed = feed or feed_for(self.object)
            context["telegram_feed_url"] = feed_link(feed)
        if context["telegram_bot_ready"] and not self.object.telegram_published:
            decision = telegram_publication_decision(self.request.user, self.object)
            if not decision.allowed:
                context["telegram_publish_denial"] = decision.message
        if self.object.telegram_published:
            context["telegram_subscriber_count"] = feed.subscribers.filter(active=True).count() if feed else 0
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
            "new_leads": workspace_queryset(Lead.objects, self.request.user).filter(status="new", outcome="open").count(),
        }
        context["closed_agencies"] = Agency.objects.filter(owner=self.request.user, closed_at__isnull=False).order_by("-closed_at")
        return context


def client_panel_query(raw_query):
    """Keep only list filters when returning from an inline client action."""
    supplied = parse_qs(raw_query)
    params = {}
    if supplied.get("role", [""])[0] == "owner":
        params["role"] = "owner"
    query = supplied.get("q", [""])[0].strip()[:100]
    if query:
        params["q"] = query
    for key, choices in (("status", Client.STATUS_CHOICES), ("source", Client.SOURCE_CHOICES)):
        value = supplied.get(key, [""])[0]
        if value in dict(choices):
            params[key] = value
    allowed_sorts = {"newest", "oldest", "updated", "name_asc", "name_desc", "leads_desc", "objects_desc"}
    sort = supplied.get("sort", [""])[0]
    if sort in allowed_sorts:
        params["sort"] = sort
    for key in ("date_from", "date_to"):
        value = supplied.get(key, [""])[0]
        if value:
            try:
                if parse_date(value):
                    params[key] = value
            except ValueError:
                pass
    page = supplied.get("page", [""])[0]
    if page.isdecimal() and len(page) <= 6 and int(page) > 0:
        params["page"] = page
    return urlencode(params)


def client_panel_details(request, client, **overrides):
    """A small version of the client overview for the list side panel."""
    priority_lead = client.leads.select_related("property", "assigned_to__realtor_profile").filter(
        outcome="open", first_contacted_at__isnull=True,
    ).exclude(status="archived").order_by("created_at", "pk").first()
    priority_owner_inquiry = client.owner_inquiries.select_related("assigned_to__realtor_profile").filter(
        property__isnull=True,
    ).exclude(result="declined").exclude(status="archived").order_by("next_action_due_at", "pk").first()
    next_reminder = client.reminders.select_related("owner__realtor_profile", "lead").filter(
        is_done=False, cancelled_at__isnull=True,
    ).order_by(F("due_at").asc(nulls_last=True), "pk").first()
    editable_reminder = bool(
        next_reminder and not priority_lead and not priority_owner_inquiry
        and next_reminder.owner_id == request.user.pk and not next_reminder.lead_id
        and not next_reminder.owner_showing_id
    )
    last_discussion = client.interactions.exclude(
        interaction_type="status",
    ).exclude(text__in=("", "Первый контакт по заявке.")).exclude(
        interaction_type="note", text__startswith="Действие «",
    ).order_by("-created_at", "-pk").first()
    last_outcome = client.reminders.filter(
        is_done=True, cancelled_at__isnull=True, completed_at__isnull=False,
    ).exclude(outcome_note="").order_by("-completed_at", "-pk").first()
    latest_discussion = None
    if last_outcome and (not last_discussion or last_outcome.completed_at >= last_discussion.created_at):
        latest_discussion = {"kind": "Итог действия", "text": last_outcome.outcome_note, "at": last_outcome.completed_at}
    elif last_discussion:
        latest_discussion = {"kind": last_discussion.get_interaction_type_display(), "text": last_discussion.text, "at": last_discussion.created_at}
    return {
        "selected_client": client,
        "panel_priority_lead": priority_lead,
        "panel_priority_owner_inquiry": priority_owner_inquiry if not priority_lead else None,
        "panel_next_reminder": next_reminder,
        "panel_reminder_editable": editable_reminder,
        "panel_task_form": overrides.get("panel_task_form") or ClientReminderForm(
            instance=next_reminder if editable_reminder else None,
            initial={} if editable_reminder else {"due_at": timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")},
        ),
        "panel_latest_discussion": latest_discussion,
        "panel_leads_count": client.leads.count(),
        "panel_deals_count": client.deals.count(),
        "panel_owned_properties_count": client.owned_properties.count() if client.is_property_owner else 0,
        "panel_now": timezone.now(),
        "panel_status_error": overrides.get("panel_status_error"),
        "panel_status_value": overrides.get("panel_status_value", client.status),
        "panel_task_error": overrides.get("panel_task_error"),
        "panel_task_open": overrides.get("panel_task_open", False),
        "panel_status_open": overrides.get("panel_status_open", False),
    }


def is_client_panel_request(request):
    return request.headers.get("HX-Request") == "true" and request.POST.get("panel_context") == "client_list"


def render_client_panel(request, client, **overrides):
    overrides.setdefault("panel_status_value", request.POST.get("status", client.status))
    context = client_panel_details(request, client, **overrides)
    context["client_panel_query"] = client_panel_query(request.POST.get("panel_list_query", ""))
    return render(request, "properties/includes/client_panel.html", context)


def client_panel_saved(message):
    return HttpResponse(status=204, headers={"HX-Trigger": json.dumps({"clientPanelSaved": {"message": message}})})


class ClientListView(LoginRequiredMixin, ListView):
    model = Client
    template_name = "properties/client_list.html"
    context_object_name = "clients"
    paginate_by = 10

    def get_template_names(self):
        if (self.request.headers.get("HX-Request") == "true"
                and self.request.headers.get("HX-History-Restore-Request") != "true"):
            return ["properties/includes/client_workspace.html"]
        return [self.template_name]

    @staticmethod
    def valid_date(value):
        if not value:
            return None
        try:
            return parse_date(value)
        except ValueError:
            return None

    def get_queryset(self):
        queryset = workspace_queryset(Client.objects, self.request.user).annotate(lead_count=Count("leads", distinct=True))
        if self.request.GET.get("role") == "owner":
            queryset = queryset.filter(contact_role__in=("owner", "both")).annotate(
                owner_property_count=Count("owned_properties", distinct=True),
            ).prefetch_related("owned_properties__property")
        else:
            queryset = queryset.filter(contact_role__in=("seeker", "both"))
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
            "objects_desc": "-owner_property_count",
        }
        if sort == "objects_desc" and self.request.GET.get("role") != "owner":
            sort = "newest"
        return queryset.order_by(ordering.get(sort, ordering["newest"]), "-pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["owner_mode"] = self.request.GET.get("role") == "owner"
        context["reset_url"] = f"{reverse('client_list')}?role=owner" if context["owner_mode"] else reverse("client_list")
        now = timezone.now()
        start_tomorrow = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        pending_reminders = workspace_queryset(ClientReminder.objects, self.request.user).filter(
            client__isnull=False, owner=self.request.user, is_done=False, cancelled_at__isnull=True,
        ).select_related("client")
        pending_reminders = pending_reminders.filter(client__contact_role__in=("owner", "both") if context["owner_mode"] else ("seeker", "both"))
        overdue_reminders = pending_reminders.filter(due_at__lt=now)
        today_reminders = pending_reminders.filter(due_at__gte=now, due_at__lt=start_tomorrow)
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
                    ("objects_desc", "Больше объектов") if context["owner_mode"] else ("leads_desc", "Больше заявок"),
                ],
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
            ("status", f"Отношения: {status_labels[filters['status']]}" if filters.get("status") in status_labels else None),
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
        context["has_any_clients"] = bool(context["page_obj"].paginator.count) or workspace_queryset(
            Client.objects, self.request.user,
        ).filter(contact_role__in=("owner", "both") if context["owner_mode"] else ("seeker", "both")).exists()
        context["can_bulk_delete"] = not agency_for(self.request.user) or may_manage_agency(self.request.user)
        query = self.request.GET.copy()
        query.pop("client", None)
        query["page"] = str(context["page_obj"].number)
        context["client_panel_query"] = client_panel_query(query.urlencode())
        requested_client = self.request.GET.get("client", "")
        if requested_client:
            role = ("owner", "both") if context["owner_mode"] else ("seeker", "both")
            selected_client = get_object_or_404(
                workspace_queryset(Client.objects, self.request.user).filter(contact_role__in=role),
                pk=requested_client if requested_client.isdecimal() else None,
            )
            context.update(client_panel_details(self.request, selected_client))
            visible_ids = [item.pk for item in context["clients"]]
            if visible_ids:
                if selected_client.pk in visible_ids and len(visible_ids) > 1:
                    context["next_client_id"] = visible_ids[(visible_ids.index(selected_client.pk) + 1) % len(visible_ids)]
                elif selected_client.pk not in visible_ids:
                    context["next_client_id"] = visible_ids[0]
        return context


class ClientCreateView(LoginRequiredMixin, CreateView):
    model = Client
    form_class = ClientForm
    template_name = "properties/client_form.html"

    def get_form_class(self):
        return OwnerContactForm if self.request.GET.get("role") == "owner" else ClientForm

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "owner_mode": self.request.GET.get("role") == "owner"}

    def form_valid(self, form):
        form.instance.owner = self.request.user
        form.instance.agency = agency_for(self.request.user)
        if self.request.GET.get("role") == "owner":
            form.instance.contact_role = "owner"
            form.instance.source = "manual"
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
            form.add_error("phone", f"Контакт с этим номером уже есть в базе: {existing_client.name}.")
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

    def get_form_class(self):
        return OwnerContactForm if self.object.contact_role == "owner" else ClientDetailsForm

    def form_valid(self, form):
        try:
            require_resource_action(self.request.user, self.object, Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["possible_client_matches"] = possible_matches(
            self.object, workspace_queryset(Client.objects, self.request.user),
        )
        context["can_merge_contacts"] = not self.object.agency_id or may_manage_agency(self.request.user)
        if self.object.is_property_owner:
            owner_properties = self.object.owned_properties.select_related("property").order_by("-created_at", "-pk")
            owner_inquiries = self.object.owner_inquiries.select_related("property", "assigned_to").order_by("-created_at", "-pk")
            context["owner_properties_total"] = owner_properties.count()
            context["owner_properties"] = owner_properties[:3]
            context["owner_inquiries_total"] = owner_inquiries.count()
            context["owner_inquiries"] = owner_inquiries[:3]
        context["priority_owner_inquiry"] = self.object.owner_inquiries.select_related("assigned_to__realtor_profile").filter(
            property__isnull=True,
        ).exclude(result="declined").exclude(status="archived").order_by("next_action_due_at", "pk").first()
        context["search_form"] = ClientSearchForm(instance=self.object)
        context["suggestion_form"] = ClientPropertySuggestionForm(owner=self.request.user, client=self.object)
        suggestions = list(self.object.property_suggestions.select_related("property", "added_by"))
        active_deals = {
            deal.property_id: deal for deal in self.object.deals.filter(stage__in=Deal.ACTIVE_STAGES).order_by("-created_at")
        }
        for suggestion in suggestions:
            suggestion.reaction_form = ClientPropertyReactionForm(instance=suggestion, prefix=f"reaction-{suggestion.pk}")
            suggestion.active_deal = active_deals.get(suggestion.property_id)
            if suggestion.property.landing_published and suggestion.property.landing_slug:
                suggestion.share_url = self.request.build_absolute_uri(
                    reverse("public_landing", args=[suggestion.property.landing_slug]),
                )
        context["suggestions"] = suggestions
        available = workspace_queryset(Property.objects, self.request.user).exclude(
            status__in=("archived", "reserved", "sold", "rented"),
        ).exclude(pk__in=[item.property_id for item in suggestions])
        search_segment = self.object.search_segment
        if not search_segment and self.object.search_property_type:
            search_segment = "commercial" if self.object.search_property_type in Property.COMMERCIAL_TYPES else "residential"
        if not search_segment and self.object.search_commercial_purpose:
            search_segment = "commercial"
        if not search_segment and self.object.search_rooms_min:
            search_segment = "residential"
        if search_segment == "commercial":
            available = available.filter(property_type__in=Property.COMMERCIAL_TYPES)
        elif search_segment == "residential":
            available = available.filter(property_type__in=Property.RESIDENTIAL_TYPES)
        if self.object.search_deal_type:
            available = available.filter(deal_type=self.object.search_deal_type)
        if self.object.search_property_type:
            available = available.filter(property_type=self.object.search_property_type)
        if self.object.search_rooms_min and search_segment != "commercial":
            available = available.filter(rooms__gte=self.object.search_rooms_min)
        if self.object.search_area_min is not None:
            available = available.filter(area__gte=self.object.search_area_min)
        if self.object.search_area_max is not None:
            available = available.filter(area__lte=self.object.search_area_max)
        if self.object.search_commercial_purpose and search_segment == "commercial":
            available = available.filter(
                commercial_purpose_search__contains=self.object.search_commercial_purpose.strip().casefold(),
            )
        if self.object.preferred_area:
            available = available.filter(address__icontains=self.object.preferred_area.strip())
        if self.object.budget and self.object.search_currency:
            available = available.filter(currency=self.object.search_currency, price__lte=self.object.budget)
        context["matching_properties"] = available.order_by("-updated_at")[:8]
        context["has_search_criteria"] = any((
            self.object.search_deal_type, search_segment, self.object.search_property_type,
            self.object.search_rooms_min if search_segment != "commercial" else None,
            self.object.search_area_min is not None, self.object.search_area_max is not None,
            self.object.search_commercial_purpose if search_segment == "commercial" else "",
            self.object.preferred_area, self.object.budget,
        ))
        context["interaction_form"] = kwargs.get("interaction_form") or (
            OwnerInteractionForm(client=self.object) if self.object.is_property_owner else ClientInteractionForm()
        )
        context["reminder_form"] = ClientReminderForm(initial={"due_at": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")})
        interaction_queryset = self.object.interactions.select_related("lead", "lead__property", "property")
        reminder_queryset = self.object.reminders.select_related("owner__realtor_profile", "lead", "deal")
        pending_reminders = reminder_queryset.filter(is_done=False, cancelled_at__isnull=True)
        lead_queryset = self.object.leads.select_related("property", "assigned_to__realtor_profile").order_by("-created_at", "-pk")
        context["priority_lead"] = lead_queryset.filter(
            outcome="open", first_contacted_at__isnull=True,
        ).exclude(status="archived").order_by("created_at", "pk").first()
        context["has_uncontacted_leads"] = bool(context["priority_lead"])
        if context["has_uncontacted_leads"]:
            interaction_type = context["interaction_form"].fields["interaction_type"]
            interaction_type.choices = [
                (value, label) for value, label in interaction_type.choices
                if value not in {"call", "message"}
            ]
        last_discussion = self.object.interactions.exclude(
            interaction_type="status",
        ).exclude(text__in=("", "Первый контакт по заявке.")).exclude(
            interaction_type="note", text__startswith="Действие «",
        ).order_by("-created_at", "-pk").first()
        last_outcome = self.object.reminders.filter(
            is_done=True, cancelled_at__isnull=True, completed_at__isnull=False,
        ).exclude(outcome_note="").order_by("-completed_at", "-pk").first()
        if last_outcome and (not last_discussion or last_outcome.completed_at >= last_discussion.created_at):
            context["latest_client_discussion"] = {
                "kind": "Итог действия", "text": last_outcome.outcome_note, "at": last_outcome.completed_at,
            }
        elif last_discussion:
            context["latest_client_discussion"] = {
                "kind": last_discussion.get_interaction_type_display(), "text": last_discussion.text,
                "at": last_discussion.created_at,
            }
        deal_queryset = self.object.deals.select_related("property", "responsible__realtor_profile")
        context["interactions"] = Paginator(interaction_queryset, 10).get_page(self.request.GET.get("history_page", 1))
        context["client_leads"] = Paginator(lead_queryset, 10).get_page(kwargs.get("contact_form_page") or self.request.GET.get("leads_page", 1))
        context["client_deals"] = Paginator(deal_queryset, 10).get_page(self.request.GET.get("deals_page", 1))
        contact_draft = kwargs.get("contact_draft")
        contact_leads = (
            lead_queryset.filter(outcome="open", first_contacted_at__isnull=True).exclude(status="archived")
            if contact_draft else context["client_leads"]
        )
        lead_pages = {}
        if contact_draft:
            lead_pages = {
                lead_id: index // 10 + 1
                for index, lead_id in enumerate(lead_queryset.values_list("pk", flat=True))
            }
        contact_forms = []
        for lead in contact_leads:
            if lead.outcome != "open" or lead.first_contacted_at or lead.status == "archived":
                continue
            form = kwargs.get("contact_form") if kwargs.get("contact_form_lead_id") == lead.pk else LeadProcessingForm(lead=lead)
            if contact_draft and not form.is_bound:
                form.initial.update({
                    "contact_method": contact_draft["method"], "contact_note": contact_draft["note"],
                })
            for field_name, field in form.fields.items():
                field.widget.attrs["id"] = f"id_client_lead_{lead.pk}_{field_name}"
            contact_forms.append({
                "lead": lead, "form": form,
                "page_number": lead_pages.get(lead.pk, context["client_leads"].number),
            })
        context["contact_forms"] = contact_forms
        context["contact_draft"] = contact_draft
        context["open_client_leads_tab"] = bool(kwargs.get("contact_form") or contact_draft)
        context["interactions_total"] = interaction_queryset.count()
        context["next_reminder"] = (
            pending_reminders.filter(due_at__isnull=False).order_by("due_at", "pk").first()
            or pending_reminders.order_by("pk").first()
        )
        if context["priority_lead"]:
            primary_reminder = pending_reminders.filter(lead=context["priority_lead"]).first()
        elif context["priority_owner_inquiry"]:
            primary_reminder = None
        else:
            primary_reminder = context["next_reminder"]
        other_reminders = reminder_queryset.exclude(pk=primary_reminder.pk) if primary_reminder else reminder_queryset
        context["reminders"] = Paginator(other_reminders, 10).get_page(self.request.GET.get("reminders_page", 1))
        context["reminders_total"] = other_reminders.count()
        context["pending_reminders_count"] = (
            pending_reminders.exclude(pk=primary_reminder.pk).count() if primary_reminder else pending_reminders.count()
        )
        context["next_reminder_overdue"] = bool(
            context["next_reminder"] and context["next_reminder"].due_at
            and context["next_reminder"].due_at < timezone.now()
        )
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
    @transaction.atomic
    def post(self, request, pk):
        client = get_object_or_404(workspace_queryset(Client.objects.select_for_update(), request.user), pk=pk)
        try:
            require_resource_action(request.user, client, Operation.EDIT_CRM)
        except TariffDenied as error:
            if is_client_panel_request(request):
                return render_client_panel(request, client, panel_status_error=error.message)
            if request.headers.get("HX-Request") == "true":
                return render(request, "properties/includes/client_status_control.html", {"client": client, "billing_error": error.message})
            messages.error(request, error.message)
            return redirect("client_detail", pk=client.pk)
        status = request.POST.get("status")
        if status not in dict(Client.STATUS_CHOICES):
            error = "Выберите состояние отношений. Показ и итог по объекту меняйте в сделке."
            if is_client_panel_request(request):
                return render_client_panel(request, client, panel_status_error=error)
            if request.headers.get("HX-Request") == "true":
                return render(request, "properties/includes/client_status_control.html", {"client": client, "status_error": error})
            messages.error(request, error)
            return redirect("client_detail", pk=client.pk)
        error = relationship_change_error(client, status)
        if error:
            if is_client_panel_request(request):
                return render_client_panel(request, client, panel_status_error=error)
            if request.headers.get("HX-Request") == "true":
                return render(request, "properties/includes/client_status_control.html", {"client": client, "status_error": error})
            messages.error(request, error)
            return redirect("client_detail", pk=client.pk)
        change_client_relationship(client, status)
        if is_client_panel_request(request):
            return client_panel_saved("Отношения обновлены.")
        if request.headers.get("HX-Request") == "true":
            return render(request, "properties/includes/client_status_control.html", {"client": client})
        return redirect("client_detail", pk=client.pk)


class ClientBoardStatusUpdateView(LoginRequiredMixin, View):
    """Move one visible client between relationship columns."""

    def post(self, request, pk):
        visible = get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)
        target = request.POST.get("status", "")
        expected = request.POST.get("expected_status", "")
        statuses = {key for key, _ in Client.STATUS_CHOICES}
        if target not in statuses or expected not in statuses:
            return JsonResponse({"error": "Выберите состояние отношений заново."}, status=400)

        try:
            require_resource_action(request.user, visible, Operation.EDIT_CRM)
        except TariffDenied as error:
            return JsonResponse({"error": error.message}, status=403)

        with transaction.atomic():
            client = get_object_or_404(
                workspace_queryset(Client.objects.select_for_update(), request.user), pk=pk,
            )
            if client.status != expected:
                return JsonResponse({"error": "Отношения с клиентом уже изменились. Обновите страницу и повторите перенос."}, status=409)
            if target == client.status:
                return JsonResponse({"status": client.status, "changed": False})
            error = relationship_change_error(client, target)
            if error:
                return JsonResponse({"error": error}, status=409)
            change_client_relationship(client, target)
        return JsonResponse({"status": target, "changed": True})


class ClientInteractionCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        client = get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)
        try:
            require_resource_action(request.user, client, Operation.EDIT_CRM)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("client_detail", pk=client.pk)
        post_data = request.POST
        if request.POST.get("intent") == "first_contact":
            post_data = request.POST.copy()
            if post_data.get("interaction_type") not in {"call", "message"}:
                post_data["interaction_type"] = "call"
        form = OwnerInteractionForm(post_data, client=client) if client.is_property_owner else ClientInteractionForm(post_data)
        if form.is_valid():
            if form.cleaned_data["interaction_type"] in {"call", "message"} and client.leads.filter(
                outcome="open", first_contacted_at__isnull=True,
            ).exclude(status="archived").exists():
                detail = ClientDetailView()
                detail.request = request
                detail.object = client
                context = detail.get_context_data(contact_draft={
                    "method": form.cleaned_data["interaction_type"],
                    "note": form.cleaned_data["text"],
                    "next_action": form.cleaned_data["next_action"],
                    "next_action_due_at": form.cleaned_data["next_action_due_at"],
                })
                return render(request, detail.template_name, context)
            with transaction.atomic():
                interaction = form.save(commit=False)
                interaction.client = client
                interaction.save()
                if form.cleaned_data["next_action"]:
                    ClientReminder.objects.create(
                        client=client, owner=request.user, agency=client.agency,
                        property=interaction.property,
                        text=form.cleaned_data["next_action"],
                        due_at=form.cleaned_data["next_action_due_at"],
                    )
                if interaction.interaction_type in {"call", "message"} and not client.first_contacted_at:
                    client.first_contacted_at = interaction.created_at
                    client.save(update_fields=["first_contacted_at", "updated_at"])
                    activate_client_relationship(client.pk, reason="Первый контакт зафиксирован.")
            if form.cleaned_data["next_action"]:
                messages.success(request, "Контакт записан. Следующий шаг добавлен в задачи.")
        else:
            detail = ClientDetailView()
            detail.request = request
            detail.object = client
            context = detail.get_context_data(interaction_form=form)
            context["open_client_history_tab"] = True
            return render(request, detail.template_name, context, status=400)
        return redirect(f"{reverse('client_detail', kwargs={'pk': client.pk})}#client-history")


class ClientReminderCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        client = get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)
        try:
            require_resource_action(request.user, client, Operation.EDIT_CRM)
        except TariffDenied as error:
            if is_client_panel_request(request):
                return render_client_panel(request, client, panel_task_error=error.message, panel_task_open=True)
            messages.error(request, error.message)
            return redirect("client_detail", pk=client.pk)
        form = ClientReminderForm(request.POST)
        if form.is_valid():
            reminder = form.save(commit=False)
            reminder.client = client
            reminder.owner = request.user
            reminder.save()
            if is_client_panel_request(request):
                return client_panel_saved("Задача добавлена.")
            messages.success(request, "Задача добавлена.")
        else:
            if is_client_panel_request(request):
                return render_client_panel(request, client, panel_task_form=form, panel_task_open=True)
            messages.error(request, "Не удалось добавить задачу. Проверьте текст и срок выполнения.")
        return redirect("client_detail", pk=client.pk)


class ClientPanelTaskUpdateView(LoginRequiredMixin, View):
    @transaction.atomic
    def post(self, request, pk, reminder_pk):
        client = get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)
        reminder = get_object_or_404(
            client.reminders.select_for_update(), pk=reminder_pk, owner=request.user,
            is_done=False, cancelled_at__isnull=True, lead__isnull=True, owner_showing__isnull=True,
        )
        try:
            require_resource_action(request.user, client, Operation.EDIT_CRM)
            require_resource_action(request.user, reminder, Operation.EDIT_CRM)
        except TariffDenied as error:
            if is_client_panel_request(request):
                return render_client_panel(request, client, panel_task_error=error.message, panel_task_open=True)
            messages.error(request, error.message)
            return redirect("client_detail", pk=client.pk)
        form = ClientReminderForm(request.POST, instance=reminder)
        if not form.is_valid():
            if is_client_panel_request(request):
                return render_client_panel(request, client, panel_task_form=form, panel_task_open=True)
            messages.error(request, "Проверьте текст задачи и срок выполнения.")
            return redirect("client_detail", pk=client.pk)
        form.save()
        if is_client_panel_request(request):
            return client_panel_saved("Задача обновлена.")
        messages.success(request, "Задача обновлена.")
        return redirect("client_detail", pk=client.pk)


class ClientReminderCompleteView(LoginRequiredMixin, View):
    def post(self, request, pk, reminder_pk):
        client = get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)
        reminder = get_object_or_404(client.reminders, pk=reminder_pk, owner=request.user)
        if reminder.lead_id:
            messages.info(request, "Запишите итог и назначьте следующий шаг в карточке заявки.")
            return redirect(f"{reverse('lead_detail', kwargs={'pk': reminder.lead_id})}?complete=1#lead-action")
        if reminder.owner_showing_id:
            from .owner_feedback_views import showing_feedback_url
            messages.info(request, "Запишите, что сообщили собственнику, в карточке показа.")
            return redirect(showing_feedback_url(reminder.owner_showing_id))
        try:
            require_resource_action(request.user, client, Operation.EDIT_CRM)
        except TariffDenied as error:
            if is_client_panel_request(request):
                return render_client_panel(request, client, panel_task_error=error.message, panel_task_open=True)
            messages.error(request, error.message)
            return redirect("client_detail", pk=client.pk)
        reminder.is_done = True
        reminder.completed_at = timezone.now()
        reminder.save(update_fields=["is_done", "completed_at"])
        if is_client_panel_request(request):
            return client_panel_saved("Задача выполнена.")
        return redirect("client_detail", pk=client.pk)


class ClientBulkActionView(LoginRequiredMixin, View):
    def post(self, request):
        client_ids = sorted({int(pk) for pk in request.POST.getlist("client_ids") if pk.isdigit()})
        clients = workspace_queryset(Client.objects, request.user).filter(pk__in=client_ids)
        action = request.POST.get("action")
        if action == "status":
            status = request.POST.get("status")
            if status not in dict(Client.STATUS_CHOICES):
                messages.error(request, "Выберите состояние отношений для выбранных клиентов.")
            elif not client_ids:
                messages.error(request, "Выберите хотя бы одного клиента.")
            else:
                try:
                    with transaction.atomic():
                        selected = list(workspace_queryset(
                            Client.objects.select_for_update(), request.user,
                        ).filter(pk__in=client_ids).order_by("pk"))
                        if len(selected) != len(client_ids):
                            raise ValidationError("Некоторые выбранные клиенты недоступны. Обновите список.")
                        for client in selected:
                            require_resource_action(request.user, client, Operation.EDIT_CRM)
                            error = relationship_change_error(client, status)
                            if error:
                                raise ValidationError(f"{client.name}: {error}")
                        changed = sum(change_client_relationship(client, status) for client in selected)
                    if changed:
                        messages.success(request, f"Отношения изменены у {changed} клиентов.")
                    else:
                        messages.info(request, "У выбранных клиентов уже установлены эти отношения.")
                except TariffDenied as error:
                    messages.error(request, error.message)
                except ValidationError as error:
                    messages.error(request, " ".join(error.messages))
        elif action == "delete":
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
        writer.writerow(["Имя", "Телефон", "Отношения", "Источник", "Бюджет", "Район", "Удобное время", "Создан"])
        for client in workspace_queryset(Client.objects, request.user).filter(contact_role__in=("seeker", "both")).order_by("-created_at"):
            writer.writerow([spreadsheet_safe_cell(value) for value in [client.name, client.phone, client.get_status_display(), client.get_source_display(), client.budget or "", client.preferred_area, client.preferred_contact_time, client.created_at.strftime("%d.%m.%Y %H:%M")]])
        return response


class ClientBoardView(LoginRequiredMixin, View):
    def get(self, request):
        clients = workspace_queryset(Client.objects, request.user).filter(contact_role__in=("seeker", "both"))

        def hydrate(ids):
            return clients.filter(pk__in=ids).annotate(
                active_deals_count=Count("deals", filter=Q(deals__stage__in=Deal.ACTIVE_STAGES), distinct=True),
                completed_deals_count=Count("deals", filter=Q(deals__stage="won"), distinct=True),
            )

        columns = paged_board_columns(
            request, choices=Client.STATUS_CHOICES, queryset=clients, field="status", hydrate=hydrate,
            anchor_prefix="client-board",
        )
        return render(request, "properties/client_board.html", {"columns": columns})


class LeadListView(LoginRequiredMixin, ListView):
    model = Lead
    template_name = "properties/lead_list.html"
    context_object_name = "leads"
    paginate_by = 10

    def get_template_names(self):
        if (self.request.headers.get("HX-Request") == "true"
                and self.request.headers.get("HX-History-Restore-Request") != "true"):
            return ["properties/includes/lead_workspace.html"]
        return [self.template_name]

    def search_query(self):
        return self.request.GET.get("q", "").strip()[:100]

    @staticmethod
    def search_filter(query):
        return (
            Q(name__icontains=query) | Q(phone__icontains=query) | Q(message__icontains=query)
            | Q(client__name__icontains=query) | Q(client__phone__icontains=query)
            | Q(property__title__icontains=query) | Q(property__address__icontains=query)
        )

    def analytics_params(self):
        """Validated read-only drill-down filters from the analytics page."""
        if hasattr(self, "_analytics_params"):
            return self._analytics_params
        params = {}
        metric = self.request.GET.get("metric", "")
        if metric not in {"received", "contacted", "unanswered", "overdue", "active"}:
            self._analytics_params = params
            return params
        params["metric"] = metric
        scope = self.request.GET.get("scope")
        if scope == "personal":
            params["scope"] = "personal"
        elif scope == "team" and agency_for(self.request.user):
            params["scope"] = "team"
        if metric in {"received", "contacted"}:
            try:
                first = parse_date(self.request.GET.get("date_from", ""))
                last = parse_date(self.request.GET.get("date_to", ""))
                if not first or not last:
                    raise ValueError
                AnalyticsPeriod.ending_now(first_day=first, last_day=last)
            except ValueError:
                fallback = AnalyticsPeriod.ending_now()
                first, last = fallback.first_day, fallback.last_day
            params.update({"date_from": first.isoformat(), "date_to": last.isoformat()})
        source = self.request.GET.get("source", "")
        if metric == "received" and (source in dict(Lead.SOURCE_CHOICES) or source == "unknown"):
            params["source"] = source
        responsible = self.request.GET.get("responsible", "")
        agency = agency_for(self.request.user)
        if agency and may_manage_agency(self.request.user):
            if responsible == "unassigned" and params.get("scope") == "team":
                params["responsible"] = responsible
            elif responsible.isdecimal() and agency.memberships.filter(user_id=int(responsible)).exists():
                params["responsible"] = responsible
        self._analytics_params = params
        return params

    def apply_analytics_filters(self, queryset):
        params = self.analytics_params()
        if not params:
            return queryset
        queryset = queryset.filter(is_demo=False).filter(Q(property__isnull=True) | Q(property__is_demo=False)).filter(
            Q(client__isnull=True) | Q(client__is_demo=False),
        )
        if params.get("scope") == "team":
            queryset = queryset.filter(agency_id=agency_for(self.request.user).pk)
        elif params.get("scope") == "personal":
            queryset = queryset.filter(agency__isnull=True, owner=self.request.user)
        if params.get("responsible") == "unassigned":
            queryset = queryset.filter(assigned_to__isnull=True)
        elif params.get("responsible"):
            queryset = queryset.filter(assigned_to_id=int(params["responsible"]))
        metric = params["metric"]
        if metric in {"received", "contacted"}:
            first = parse_date(params["date_from"]) if "date_from" in params else None
            last = parse_date(params["date_to"]) if "date_to" in params else None
            if first and last:
                period = AnalyticsPeriod.ending_now(first_day=first, last_day=last)
                field = "created_at" if metric == "received" else "first_contacted_at"
                queryset = queryset.filter(**{f"{field}__gte": period.start, f"{field}__lt": period.end})
        elif metric == "unanswered":
            queryset = queryset.filter(outcome="open", first_contacted_at__isnull=True).exclude(status="archived")
        elif metric == "overdue":
            queryset = queryset.filter(Lead.overdue_filter(timezone.now())).exclude(status="archived")
        elif metric == "active":
            queryset = queryset.filter(outcome="open").exclude(status="archived")
        if "source" in params:
            if params["source"] == "unknown":
                queryset = queryset.exclude(source__in=dict(Lead.SOURCE_CHOICES))
            else:
                queryset = queryset.filter(source=params["source"])
        return queryset

    def get_queryset(self):
        queryset = workspace_queryset(Lead.objects, self.request.user).select_related("property", "client", "assigned_to__realtor_profile")
        queryset = self.apply_analytics_filters(queryset)
        if self.request.GET.get("assigned") == "mine":
            queryset = queryset.filter(assigned_to=self.request.user)
        status = self.request.GET.get("status")
        if status in dict(Lead.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        queue = self.selected_queue()
        if queue == "needs_response":
            queryset = queryset.filter(outcome="open", first_contacted_at__isnull=True).exclude(status="archived")
        elif queue == "in_progress":
            queryset = queryset.filter(outcome="open", first_contacted_at__isnull=False).exclude(status="archived")
        elif queue == "finished":
            queryset = queryset.exclude(outcome="open").exclude(status="archived")
        query = self.search_query()
        if query:
            queryset = queryset.filter(self.search_filter(query))
        now = timezone.now()
        active = Q(outcome="open") & ~Q(status="archived")
        leads = queryset.annotate(
            attention_rank=Case(
                When(active & Q(first_contacted_at__isnull=True, created_at__lt=now - timedelta(minutes=30)), then=Value(0)),
                When(active & Q(next_action_due_at__lt=now), then=Value(1)),
                When(active & Q(first_contacted_at__isnull=True), then=Value(2)),
                When(active, then=Value(3)),
                default=Value(4), output_field=IntegerField(),
            ),
            attention_sort_at=Case(
                When(active & Q(first_contacted_at__isnull=True), then=F("created_at")),
                When(active, then=F("next_action_due_at")),
                default=Value(None), output_field=DateTimeField(),
            ),
        )
        self.owner_inquiries = self.get_owner_inquiries()
        lead_keys = leads.order_by().annotate(
            entry_kind=Value("lead", output_field=CharField()),
            entry_id=F("pk"), sort_created_at=F("created_at"),
        ).values("entry_kind", "entry_id", "attention_rank", "attention_sort_at", "sort_created_at")
        if self.owner_inquiries is not None:
            active_owner = ~Q(result="declined") & Q(property__isnull=True) & ~Q(status="archived")
            owner_keys = self.owner_inquiries.order_by().annotate(
                entry_kind=Value("owner", output_field=CharField()),
                entry_id=F("pk"), sort_created_at=F("created_at"),
                attention_rank=Case(
                    When(active_owner & Q(first_contacted_at__isnull=True, first_response_due_at__lt=now), then=Value(0)),
                    When(active_owner & Q(next_action_due_at__lt=now), then=Value(1)),
                    When(active_owner & Q(first_contacted_at__isnull=True), then=Value(2)),
                    When(active_owner, then=Value(3)),
                    default=Value(4), output_field=IntegerField(),
                ),
                attention_sort_at=Case(
                    When(active_owner & Q(first_contacted_at__isnull=True), then=F("first_response_due_at")),
                    When(active_owner, then=F("next_action_due_at")),
                    default=Value(None), output_field=DateTimeField(),
                ),
            ).values("entry_kind", "entry_id", "attention_rank", "attention_sort_at", "sort_created_at")
            lead_keys = lead_keys.union(owner_keys, all=True)
        return lead_keys.order_by("attention_rank", "attention_sort_at", "-sort_created_at", "-entry_id", "entry_kind")

    def get_owner_inquiries(self):
        # Owner requests have their own workflow. Only their list projection is shared with leads.
        if self.analytics_params():
            return None
        queryset = workspace_queryset(OwnerInquiry.objects, self.request.user)
        status = self.request.GET.get("status")
        if status in dict(OwnerInquiry.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        if self.request.GET.get("assigned") == "mine":
            queryset = queryset.filter(assigned_to=self.request.user)
        queue = self.selected_queue()
        active = ~Q(result="declined") & Q(property__isnull=True) & ~Q(status="archived")
        if queue == "needs_response":
            queryset = queryset.filter(active, first_contacted_at__isnull=True)
        elif queue == "in_progress":
            queryset = queryset.filter(active, first_contacted_at__isnull=False)
        elif queue == "finished":
            queryset = queryset.filter(Q(result="declined") | Q(property__isnull=False)).exclude(status="archived")
        query = self.search_query()
        if query:
            queryset = queryset.filter(
                Q(client__name__icontains=query) | Q(client__phone__icontains=query)
                | Q(address__icontains=query) | Q(description__icontains=query),
            )
        return queryset

    def selected_queue(self):
        requested = self.request.GET.get("queue", "")
        if requested in {"needs_response", "in_progress", "finished"}:
            return requested
        return ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        keys = list(context["leads"])
        lead_ids = [item["entry_id"] for item in keys if item["entry_kind"] == "lead"]
        owner_ids = [item["entry_id"] for item in keys if item["entry_kind"] == "owner"]
        leads_by_id = {item.pk: item for item in workspace_queryset(Lead.objects, self.request.user).select_related(
            "property", "client", "assigned_to__realtor_profile",
        ).filter(pk__in=lead_ids)}
        owners_by_id = {item.pk: item for item in workspace_queryset(OwnerInquiry.objects, self.request.user).select_related(
            "client", "property", "assigned_to__realtor_profile",
        ).filter(pk__in=owner_ids)}
        for inquiry in owners_by_id.values():
            inquiry.is_owner_inquiry = True
        context["leads"] = [
            (leads_by_id if item["entry_kind"] == "lead" else owners_by_id)[item["entry_id"]]
            for item in keys
        ]
        context["selectable_lead_ids_json"] = json.dumps([str(pk) for pk in lead_ids])
        context["selectable_owner_ids_json"] = json.dumps([str(pk) for pk in owner_ids])
        context["selectable_record_count"] = len(keys)
        can_bulk_delete = not agency_for(self.request.user) or may_manage_agency(self.request.user)
        context["status_choices"] = [
            choice for choice in Lead.STATUS_CHOICES if can_bulk_delete or choice[0] != "archived"
        ]
        requested_status = self.request.GET.get("status", "")
        selected_status = requested_status if requested_status in dict(Lead.STATUS_CHOICES) else ""
        selected_queue = self.selected_queue()
        selected_assigned = self.request.GET.get("assigned") == "mine"
        selected_q = self.search_query()
        count_queryset = workspace_queryset(Lead.objects, self.request.user)
        has_any_leads = count_queryset.exists() or workspace_queryset(OwnerInquiry.objects, self.request.user).exists()
        analytics_params = self.analytics_params()
        count_queryset = self.apply_analytics_filters(count_queryset)
        if selected_assigned:
            count_queryset = count_queryset.filter(assigned_to=self.request.user)
        if selected_q:
            count_queryset = count_queryset.filter(self.search_filter(selected_q))
        counts = {
            row["status"]: row["total"]
            for row in count_queryset
            .values("status")
            .annotate(total=Count("id"))
        }
        owner_count_queryset = workspace_queryset(OwnerInquiry.objects, self.request.user)
        if selected_assigned:
            owner_count_queryset = owner_count_queryset.filter(assigned_to=self.request.user)
        if selected_q:
            owner_count_queryset = owner_count_queryset.filter(
                Q(client__name__icontains=selected_q) | Q(client__phone__icontains=selected_q)
                | Q(address__icontains=selected_q) | Q(description__icontains=selected_q),
            )
        if analytics_params:
            owner_count_queryset = owner_count_queryset.none()
        filtered_total = sum(counts.values()) + owner_count_queryset.count()
        owner_status_counts = {
            row["status"]: row["total"] for row in owner_count_queryset.values("status").annotate(total=Count("pk"))
        }
        context["selected_status"] = selected_status
        context["selected_queue"] = selected_queue
        context["selected_assigned"] = selected_assigned
        context["selected_q"] = selected_q
        context["lead_filter_count"] = sum(bool(value) for value in (
            selected_q, selected_queue, selected_status, analytics_params,
        ))
        context["analytics_params"] = analytics_params
        context["analytics_query"] = urlencode(analytics_params)
        context["analytics_label"] = {
            "received": "Заявки за период", "contacted": "Первый контакт за период",
            "unanswered": "Без первого ответа", "overdue": "Просроченные заявки",
            "active": "Открытые заявки",
        }.get(analytics_params.get("metric"), "")
        context["analytics_source_label"] = (
            dict(Lead.SOURCE_CHOICES).get(analytics_params.get("source"), "Не указан")
            if "source" in analytics_params else ""
        )
        context["queue_now"] = timezone.now()
        if self.request.headers.get("HX-Request") != "true":
            mail_scope = Q(link__owner=self.request.user)
            agency = agency_for(self.request.user)
            if agency:
                mail_scope |= Q(link__agency=agency)
            context["pending_email_count"] = InboundEvent.objects.filter(
                mail_scope, channel="email", lead__isnull=True,
                owner_inquiry__isnull=True, ignored_at__isnull=True,
            ).count()

        def filter_url(*, status="", queue="", assigned=False):
            params = dict(analytics_params)
            if status:
                params["status"] = status
            if queue:
                params["queue"] = queue
            elif not status and not analytics_params:
                params["queue"] = "all"
            if assigned:
                params["assigned"] = "mine"
            if selected_q:
                params["q"] = selected_q
            return reverse("lead_list") + (f"?{urlencode(params)}" if params else "")
        context["status_tabs"] = [
            {"value": "", "label": "Все", "count": filtered_total, "url": filter_url(assigned=selected_assigned)},
            *[
                {"value": value, "label": label, "count": counts.get(value, 0) + owner_status_counts.get(value, 0), "url": filter_url(status=value, assigned=selected_assigned)}
                for value, label in Lead.STATUS_CHOICES
            ],
        ]
        queue_counts = count_queryset.aggregate(
            needs_response=Count("pk", filter=Q(outcome="open", first_contacted_at__isnull=True) & ~Q(status="archived")),
            in_progress=Count("pk", filter=Q(outcome="open", first_contacted_at__isnull=False) & ~Q(status="archived")),
            finished=Count("pk", filter=~Q(outcome="open") & ~Q(status="archived")),
        )
        active_owner = ~Q(result="declined") & Q(property__isnull=True) & ~Q(status="archived")
        owner_queue_counts = owner_count_queryset.aggregate(
            needs_response=Count("pk", filter=active_owner & Q(first_contacted_at__isnull=True)),
            in_progress=Count("pk", filter=active_owner & Q(first_contacted_at__isnull=False)),
            finished=Count("pk", filter=(Q(result="declined") | Q(property__isnull=False)) & ~Q(status="archived")),
        )
        queue_counts = {key: queue_counts[key] + owner_queue_counts[key] for key in queue_counts}
        context["queue_tabs"] = [
            {"value": value, "label": label, "count": queue_counts[value], "url": filter_url(queue=value, assigned=selected_assigned)}
            for value, label in (
                ("needs_response", "Требуют ответа"),
                ("in_progress", "В работе"),
                ("finished", "Завершены"),
            )
        ]
        context["all_leads_url"] = filter_url(assigned=selected_assigned)
        context["unread_leads_url"] = filter_url(status="new", assigned=selected_assigned)
        context["archive_leads_url"] = filter_url(status="archived", assigned=selected_assigned)
        context["all_assigned_url"] = filter_url(status=selected_status, queue=selected_queue, assigned=False)
        context["mine_assigned_url"] = filter_url(status=selected_status, queue=selected_queue, assigned=True)
        queue_params = (
            {"queue": selected_queue} if selected_queue
            else {"queue": "all"} if not selected_status and not analytics_params
            else {}
        )
        clear_params = {
            **analytics_params,
            **({"status": selected_status} if selected_status else {}),
            **queue_params,
            **({"assigned": "mine"} if selected_assigned else {}),
        }
        context["clear_search_url"] = reverse("lead_list") + (f"?{urlencode(clear_params)}" if clear_params else "")
        context["has_any_leads"] = has_any_leads
        context["can_bulk_delete"] = can_bulk_delete
        context["pagination_query"] = urlencode({
            **analytics_params,
            **({"status": selected_status} if selected_status else {}),
            **queue_params,
            **({"assigned": "mine"} if selected_assigned else {}),
            **({"q": selected_q} if selected_q else {}),
        })
        context["lead_panel_query"] = urlencode({
            **analytics_params,
            **({"status": selected_status} if selected_status else {}),
            **queue_params,
            **({"assigned": "mine"} if selected_assigned else {}),
            **({"q": selected_q} if selected_q else {}),
            "page": context["page_obj"].number,
        })
        requested_lead = self.request.GET.get("lead", "")
        if requested_lead:
            selected_lead = get_object_or_404(
                workspace_queryset(Lead.objects, self.request.user)
                .select_related("property", "client", "assigned_to__realtor_profile"),
                pk=requested_lead if requested_lead.isdecimal() else None,
            )
            detail = LeadDetailView()
            detail.request = self.request
            detail.object = selected_lead
            context.update(detail.get_context_data())
            context["selected_lead"] = selected_lead
            response_due = selected_lead.first_response_due_at if not selected_lead.first_contacted_at else None
            action_due = selected_lead.next_action_due_at
            context["panel_first_response_is_next"] = bool(response_due and response_due <= action_due)
            context["panel_deadline"] = min(response_due, action_due) if response_due else action_due
            context["panel_deadline_overdue"] = context["panel_deadline"] < context["queue_now"]
            visible_ids = [item.pk for item in context["leads"] if isinstance(item, Lead)]
            if visible_ids:
                if selected_lead.pk in visible_ids and len(visible_ids) > 1:
                    context["next_lead_id"] = visible_ids[(visible_ids.index(selected_lead.pk) + 1) % len(visible_ids)]
                elif selected_lead.pk not in visible_ids:
                    context["next_lead_id"] = visible_ids[0]
        context["demo_test_completed"] = (
            self.request.GET.get("demo_test") == "1"
            and workspace_queryset(Lead.objects, self.request.user).filter(is_demo=True).exists()
        )
        return context


class LeadDetailView(LoginRequiredMixin, DetailView):
    model = Lead
    template_name = "properties/lead_detail.html"
    context_object_name = "lead"

    def get_queryset(self):
        return workspace_queryset(Lead.objects, self.request.user).select_related("property", "client", "assigned_to__realtor_profile")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["processing_form"] = kwargs.get("processing_form") or LeadProcessingForm(lead=self.object)
        context["decline_form"] = kwargs.get("decline_form") or LeadDeclineForm()
        context["queue_now"] = timezone.now()
        first_response_due = self.object.first_response_due_at
        context["lead_deadlines_match"] = bool(
            first_response_due and self.object.next_action_due_at
            and timezone.localtime(first_response_due).replace(second=0, microsecond=0)
            == timezone.localtime(self.object.next_action_due_at).replace(second=0, microsecond=0)
        )
        context["current_task"] = self.object.action_tasks.filter(is_done=False).first()
        context["completed_actions"] = self.object.action_tasks.filter(is_done=True).order_by("-completed_at", "-pk")
        last_contact = self.object.interactions.filter(interaction_type__in=("call", "message")).exclude(
            text__in=("", "Первый контакт по заявке."),
        ).order_by("-created_at", "-pk").first()
        last_action = self.object.action_tasks.filter(
            is_done=True, cancelled_at__isnull=True, completed_at__isnull=False,
        ).exclude(outcome_note="").order_by("-completed_at", "-pk").first()
        if last_action and (not last_contact or last_action.completed_at >= last_contact.created_at):
            context["contact_summary"] = {
                "label": "Последний итог", "text": last_action.outcome_note, "at": last_action.completed_at,
            }
        elif last_contact:
            context["contact_summary"] = {
                "label": "Последний контакт", "text": last_contact.text, "at": last_contact.created_at,
            }
        context["contact_open"] = self.request.GET.get("contact") == "1" and not self.object.first_contacted_at
        context["complete_open"] = self.request.GET.get("complete") == "1"
        context["complete_form"] = kwargs.get("complete_form") or LeadActionCompleteForm(initial={
            "task_id": context["current_task"].pk if context["current_task"] else None,
            "next_action_due_at": timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        })
        context["can_assign"] = bool(self.object.outcome == "open" and self.object.agency_id and may_manage_agency(self.request.user))
        context["can_delete"] = not self.object.agency_id or may_manage_agency(self.request.user)
        if context["can_assign"]:
            context["team_members"] = self.object.agency.memberships.select_related("user", "user__realtor_profile").order_by("user__username")
        active_deal = None
        if self.object.client_id:
            active_deal = workspace_queryset(Deal.objects, self.request.user).filter(
                client_id=self.object.client_id,
                property_id=self.object.property_id,
                stage__in=Deal.ACTIVE_STAGES,
            ).first()
        context["linked_deal"] = self.object.outcome_deal or (active_deal if self.object.outcome == "open" else None) or workspace_queryset(self.object.deals.all(), self.request.user).first()
        context["can_convert"] = bool(self.object.property_id and self.object.outcome == "open" and self.object.status != "archived" and self.object.client_id and self.object.first_contacted_at and self.object.qualification == "qualified")
        if not self.object.property_id and self.object.outcome == "open" and self.object.status != "archived":
            from .forms import LeadPropertyForm
            context["attach_property_form"] = LeadPropertyForm(lead=self.object)
            context["can_attach_property"] = context["attach_property_form"].fields["property"].queryset.exists()
        return context


def is_lead_panel_request(request):
    return request.headers.get("HX-Request") == "true" and request.POST.get("panel_context") == "lead_list"


def render_lead_panel(request, lead, **kwargs):
    detail = LeadDetailView()
    detail.request = request
    detail.object = lead
    context = detail.get_context_data(**kwargs)
    response_due = lead.first_response_due_at if not lead.first_contacted_at else None
    action_due = lead.next_action_due_at
    context["panel_first_response_is_next"] = bool(response_due and response_due <= action_due)
    context["panel_deadline"] = min(response_due, action_due) if response_due else action_due
    context["panel_deadline_overdue"] = context["panel_deadline"] < context["queue_now"]
    context["panel_error"] = kwargs.get("panel_error")
    # Only known list filters may be returned to the close link.
    saved = parse_qs(request.POST.get("panel_list_query", ""))
    params = {}
    if saved.get("status", [""])[0] in dict(Lead.STATUS_CHOICES):
        params["status"] = saved["status"][0]
    if saved.get("queue", [""])[0] in {"all", "needs_response", "in_progress", "finished"}:
        params["queue"] = saved["queue"][0]
    if saved.get("assigned", [""])[0] == "mine":
        params["assigned"] = "mine"
    query = saved.get("q", [""])[0].strip()[:100]
    if query:
        params["q"] = query
    page = saved.get("page", [""])[0]
    if page.isdecimal() and len(page) <= 6 and int(page) > 0:
        params["page"] = page
    context["lead_panel_query"] = urlencode(params)
    return render(request, "properties/includes/lead_panel.html", context)


def lead_panel_saved(message, *, level="success"):
    return HttpResponse(status=204, headers={"HX-Trigger": json.dumps({"leadPanelSaved": {"message": message, "level": level}})})


def lead_panel_feedback(request, lead, message, *, level="success", redirect_to="lead_detail"):
    if is_lead_panel_request(request):
        if level == "error":
            return render_lead_panel(request, lead, panel_error=message)
        return lead_panel_saved(message, level=level)
    getattr(messages, level)(request, message)
    return redirect(redirect_to, pk=lead.pk)


class LeadProcessView(LoginRequiredMixin, View):
    def post(self, request, pk):
        lead = get_object_or_404(
            workspace_queryset(Lead.objects, request.user).select_related("property", "client"),
            pk=pk,
        )
        from_client = request.POST.get("return_to_client") == "1" and bool(lead.client_id)
        if lead.status == "archived" or lead.outcome != "open":
            if is_lead_panel_request(request):
                return render_lead_panel(request, lead, panel_error="Заявка уже завершена или находится в архиве.")
            messages.error(request, "Заявка уже завершена или находится в архиве.")
            return self.redirect_after(request, lead, from_client)
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
                require_resource_action(request.user, lead, Operation.EDIT_CRM)
            except TariffDenied as error:
                form.add_error(None, error.message)
                return self.render_invalid(request, lead, form, status=403, from_client=from_client)
            with transaction.atomic():
                lead = get_object_or_404(
                    workspace_queryset(Lead.objects.select_for_update(), request.user), pk=pk,
                )
                if lead.status == "archived" or lead.outcome != "open":
                    if is_lead_panel_request(request):
                        return render_lead_panel(request, lead, panel_error="Заявка уже завершена или находится в архиве.")
                    messages.error(request, "Заявка уже завершена или находится в архиве.")
                    return self.redirect_after(request, lead, from_client)
                if action == "contacted" and lead.first_contacted_at:
                    if is_lead_panel_request(request):
                        return lead_panel_saved("Первый контакт по этой заявке уже зафиксирован.", level="info")
                    messages.info(request, "Первый контакт по этой заявке уже зафиксирован.")
                    return self.redirect_after(request, lead, from_client)
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
                    activate_client_relationship(lead.client_id, reason="Первый контакт по заявке зафиксирован.", lead=lead)
                    ClientInteraction.objects.create(
                        client_id=lead.client_id,
                        lead=lead,
                        interaction_type=form.cleaned_data["contact_method"],
                        text=form.cleaned_data["contact_note"] or "Первый контакт по заявке.",
                    )
            message = "Первый контакт и следующий шаг сохранены." if action == "contacted" else "Обработка заявки обновлена."
            if is_lead_panel_request(request):
                return lead_panel_saved(message)
            messages.success(request, message)
            return self.redirect_after(request, lead, from_client)
        return self.render_invalid(request, lead, form, from_client=from_client)

    @staticmethod
    def redirect_after(request, lead, from_client):
        if from_client:
            page = request.POST.get("leads_page", "")
            query = f"?leads_page={page}" if page.isdigit() else ""
            return redirect(f"{reverse('client_detail', kwargs={'pk': lead.client_id})}{query}#client-leads")
        return redirect("lead_detail", pk=lead.pk)

    @staticmethod
    def render_invalid(request, lead, form, status=200, from_client=False):
        if is_lead_panel_request(request):
            return render_lead_panel(request, lead, processing_form=form)
        if from_client:
            detail = ClientDetailView()
            detail.request = request
            detail.object = lead.client
            context = detail.get_context_data(
                contact_form=form, contact_form_lead_id=lead.pk, contact_form_page=request.POST.get("leads_page", ""),
            )
            return render(request, detail.template_name, context, status=status)
        detail = LeadDetailView()
        detail.request = request
        detail.object = lead
        context = detail.get_context_data(processing_form=form)
        return render(request, detail.template_name, context, status=status)


class LeadActionCompleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        lead = get_object_or_404(
            workspace_queryset(Lead.objects, request.user).select_related("property", "client"), pk=pk,
        )
        if lead.status == "archived" or lead.outcome != "open":
            return lead_panel_feedback(request, lead, "Заявка уже завершена или находится в архиве.", level="error")
        form = LeadActionCompleteForm(request.POST)
        if not form.is_valid():
            return self.render_invalid(request, lead, form)
        try:
            require_resource_action(request.user, lead, Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_invalid(request, lead, form, status=403)
        with transaction.atomic():
            lead = get_object_or_404(
                workspace_queryset(Lead.objects.select_for_update(), request.user), pk=pk,
            )
            task = get_object_or_404(lead.action_tasks.select_for_update(), pk=form.cleaned_data["task_id"])
            if task.is_done:
                return lead_panel_feedback(request, lead, "Это действие уже завершено.", level="info")
            if lead.status == "archived" or lead.outcome != "open":
                return lead_panel_feedback(request, lead, "Заявка уже завершена или находится в архиве.", level="error")
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
        return lead_panel_feedback(request, lead, "Итог сохранён. Следующее действие назначено.")

    @staticmethod
    def render_invalid(request, lead, form, status=200):
        if is_lead_panel_request(request):
            return render_lead_panel(request, lead, complete_form=form)
        detail = LeadDetailView()
        detail.request = request
        detail.object = lead
        context = detail.get_context_data(complete_form=form)
        return render(request, detail.template_name, context, status=status)


class LeadOutcomeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        lead = get_object_or_404(
            workspace_queryset(Lead.objects, request.user).select_related("property"), pk=pk,
        )
        action = request.POST.get("action")
        if action not in ("decline", "reopen", "convert_existing"):
            if is_lead_panel_request(request):
                return render_lead_panel(request, lead, panel_error="Неизвестное действие.")
            return redirect("lead_detail", pk=pk)
        form = LeadDeclineForm(request.POST) if action == "decline" else None
        if form and not form.is_valid():
            if is_lead_panel_request(request):
                return render_lead_panel(request, lead, decline_form=form)
            detail = LeadDetailView()
            detail.request = request
            detail.object = lead
            return render(request, detail.template_name, detail.get_context_data(decline_form=form))
        try:
            require_resource_action(request.user, lead, Operation.EDIT_CRM)
        except TariffDenied as error:
            if is_lead_panel_request(request) and form:
                return render_lead_panel(request, lead, decline_form=form, panel_error=error.message)
            return lead_panel_feedback(request, lead, error.message, level="error")
        with transaction.atomic():
            lead = get_object_or_404(
                workspace_queryset(Lead.objects.select_for_update(), request.user), pk=pk,
            )
            if lead.status == "archived":
                return lead_panel_feedback(request, lead, "Сначала восстановите заявку из архива.", level="error")
            if action == "convert_existing":
                deal_id = request.POST.get("deal_id", "")
                if not deal_id.isdecimal():
                    return lead_panel_feedback(request, lead, "Выберите сделку для заявки.", level="error")
                deal = get_object_or_404(
                    workspace_queryset(Deal.objects.select_for_update(), request.user),
                    pk=int(deal_id), stage__in=Deal.ACTIVE_STAGES,
                    client_id=lead.client_id, property_id=lead.property_id,
                )
                try:
                    convert_lead_to_deal(lead, deal)
                except ValidationError as error:
                    return lead_panel_feedback(request, lead, error.messages[0], level="error")
                message = "Заявка привязана к сделке. Следующее действие перенесено."
            elif action == "decline":
                if lead.outcome != "open":
                    return lead_panel_feedback(request, lead, "Заявка уже завершена.", level="info")
                lead.outcome = "declined"
                lead.outcome_reason = form.cleaned_data["reason"]
                lead.save(update_fields=["outcome", "outcome_reason", "updated_at"])
                if lead.client_id:
                    ClientInteraction.objects.create(
                        client_id=lead.client_id, lead=lead, interaction_type="status",
                        text=f"Заявка закрыта без сделки. Причина: {lead.outcome_reason}",
                    )
                message = "Заявка закрыта. Причина сохранена в истории."
            else:
                if lead.outcome != "declined":
                    return lead_panel_feedback(request, lead, "Заявка уже в работе или переведена в сделку.", level="info")
                lead.outcome = "open"
                lead.outcome_reason = ""
                update_fields = ["outcome", "outcome_reason", "updated_at"]
                reopened_at = timezone.now()
                if lead.next_action_due_at <= reopened_at:
                    lead.next_action_due_at = reopened_at + timedelta(minutes=30)
                    update_fields.append("next_action_due_at")
                lead.save(update_fields=update_fields)
                if lead.client_id:
                    ClientInteraction.objects.create(
                        client_id=lead.client_id, lead=lead, interaction_type="status", text="Работа по заявке возобновлена.",
                    )
                message = "Заявка снова в работе. Следующее действие восстановлено."
        return lead_panel_feedback(request, lead, message)


class LeadBulkStatusUpdateView(LoginRequiredMixin, View):
    def post(self, request):
        lead_ids = set(request.POST.getlist("lead_ids"))
        owner_ids = set(request.POST.getlist("owner_inquiry_ids"))
        action = request.POST.get("action", "status")
        status = request.POST.get("status")
        if not lead_ids and not owner_ids:
            messages.error(request, "Выберите хотя бы одну заявку.")
        elif any(not value.isascii() or not value.isdecimal() for value in lead_ids | owner_ids):
            messages.error(request, "Выберите корректные заявки.")
        elif action not in {"status", "delete"} or (action == "status" and status not in dict(Lead.STATUS_CHOICES)):
            messages.error(request, "Выберите корректный статус обработки.")
        else:
            target_status = "archived" if action == "delete" else status
            updated = 0
            try:
                with transaction.atomic():
                    leads = list(workspace_queryset(Lead.objects.select_for_update().select_related("property"), request.user).filter(pk__in=lead_ids))
                    inquiries = list(workspace_queryset(OwnerInquiry.objects.select_for_update(), request.user).filter(pk__in=owner_ids))
                    if len(leads) != len(lead_ids) or len(inquiries) != len(owner_ids):
                        raise PermissionDenied
                    if target_status == "archived" and not may_manage_agency(request.user) and any(
                        item.agency_id for item in [*leads, *inquiries]
                    ):
                        raise PermissionDenied
                    for item in [*leads, *inquiries]:
                        if item.status == target_status:
                            continue
                        require_resource_action(request.user, item, Operation.EDIT_CRM)
                        item.status = target_status
                        item.save(update_fields=["status", "updated_at"])
                        updated += 1
            except TariffDenied as error:
                messages.error(request, error.message)
            else:
                if target_status == "archived":
                    messages.success(request, f"В архив перенесено заявок: {updated}.")
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
                lead = get_object_or_404(deletable_queryset(Lead.objects.select_for_update().select_related("property"), request.user), pk=pk)
                require_resource_action(request.user, lead, Operation.EDIT_CRM)
                if lead.status != "archived":
                    lead.status = "archived"
                    lead.save(update_fields=["status", "updated_at"])
        except TariffDenied as error:
            return lead_panel_feedback(request, lead, error.message, level="error")
        if is_lead_panel_request(request):
            return lead_panel_saved("Заявка перенесена в архив. Связи со сделками и история сохранены.")
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
        order = []
        for key in property.landing_block_order or valid_keys:
            if key in valid_keys and key not in order:
                order.append(key)
        order.extend(key for key in valid_keys if key not in order)
        return {block: index + 1 for index, block in enumerate(order)}

    @staticmethod
    def intake_source_detail(property):
        return f"Лендинг · {property.title[:200]} · #{property.pk}"[:255]

    def get_context(self, property, profile, form, **extra):
        valid_keys = [key for key, _ in Property.LANDING_BLOCKS]
        enabled = property.landing_enabled_blocks or {}
        alternative_enabled = bool(enabled.get("alternative_requests", False))
        intake_available = False
        if alternative_enabled:
            if extra.get("preview"):
                intake_available = True
            else:
                lookup = {"agency": property.agency} if property.agency_id else {"owner": property.owner}
                intake_link, _ = PublicIntakeLink.objects.get_or_create(**lookup)
                intake_available = intake_link.enabled and intake_link.responsible.is_active
        source_detail = self.intake_source_detail(property)
        return {
            "property": property,
            "profile": profile,
            "landing_heading": landing_heading(property),
            **landing_trust_copy(property, profile),
            "form": form,
            "landing_order": self.get_landing_order(property),
            "landing_enabled": {key: bool(enabled.get(key, key != "alternative_requests")) for key in valid_keys},
            "intake_available": intake_available,
            "search_form": PublicSearchRequestForm(prefix="search", initial={
                "request_id": uuid.uuid4(), "source_detail": source_detail,
            }),
            "owner_form": PublicOwnerRequestForm(prefix="owner", initial={
                "request_id": uuid.uuid4(), "source_detail": source_detail,
            }),
            "turnstile_enabled": settings.TURNSTILE_ENABLED,
            "turnstile_site_key": settings.TURNSTILE_SITE_KEY,
            "demo_landing": property.is_demo,
            "landing_custom_palette": landing_custom_palette(property.landing_custom_color) if property.landing_accent == "custom" else None,
            **extra,
        }

    def get_property(self, slug):
        return get_object_or_404(
            Property.objects.exclude(status="archived").filter(agency__closed_at__isnull=True).select_related("owner").prefetch_related("images"),
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
        response = render(request, self.get_template_name(property), self.get_context(property, profile, LeadForm(property=property)))
        if not property.is_demo:
            day = timezone.now().astimezone(ANALYTICS_TZ).date()
            daily, _ = LandingDailyView.objects.get_or_create(property=property, day=day)
            LandingDailyView.objects.filter(pk=daily.pk).update(views=F("views") + 1)
        return response

    def post(self, request, slug):
        property = self.get_property(slug)
        paused = self.access_response(property)
        if paused is not None:
            return paused
        profile = RealtorProfile.objects.filter(user=property.owner).first()
        intake_kind = request.POST.get("intake_kind")
        if intake_kind:
            if intake_kind not in {"search", "owner"} or not (property.landing_enabled_blocks or {}).get("alternative_requests", False):
                raise Http404
            lookup = {"agency": property.agency} if property.agency_id else {"owner": property.owner}
            intake_link, _ = PublicIntakeLink.objects.get_or_create(**lookup)
            if not intake_link.enabled or not intake_link.responsible.is_active:
                raise Http404
            form_class = PublicSearchRequestForm if intake_kind == "search" else PublicOwnerRequestForm
            data = request.POST.copy()
            data[f"{intake_kind}-source_detail"] = self.intake_source_detail(property)
            intake_form = form_class(data, prefix=intake_kind)
            property_agent_active = bool(property.owner_id) and property.owner.is_active and (
                not property.agency_id or AgencyMembership.objects.filter(
                    agency_id=property.agency_id, user_id=property.owner_id,
                ).exists()
            )
            accepted = submit_public_request(
                request, intake_link, intake_kind, intake_form,
                channel="landing", event_prefix=f"property:{property.pk}:",
                assigned_to=property.owner if property_agent_active else intake_link.responsible,
            )
            context = self.get_context(
                property, profile, LeadForm(property=property),
                **({"sent_intake_kind": intake_kind} if accepted else {
                    f"{intake_kind}_form": intake_form, "intake_form_error_kind": intake_kind,
                }),
            )
            return render(request, self.get_template_name(property), context)
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
            with transaction.atomic():
                if property.agency_id and Agency.objects.select_for_update().get(pk=property.agency_id).closed_at:
                    raise Http404
                intake_event = None
                if form.cleaned_data["request_id"]:
                    link_lookup = {"agency": property.agency} if property.agency_id else {"owner": property.owner}
                    intake_link, _ = PublicIntakeLink.objects.get_or_create(**link_lookup)
                    intake_event, _ = InboundEvent.objects.select_for_update().get_or_create(
                        link=intake_link, channel="landing",
                        external_id=f"property:{property.pk}:{form.cleaned_data['request_id']}",
                    )
                    if intake_event.lead_id:
                        return render(request, self.get_template_name(property), self.get_context(
                            property, profile, LeadForm(property=property), sent=True,
                            demo_test_completed=property.is_demo and request.user == property.owner,
                        ))
                lead = form.save(commit=False)
                lead.property = property
                lead.source_detail = f"Лендинг · {property.title}"[:255]
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
                if client.contact_role == "owner":
                    client.contact_role = "both"
                    client.save(update_fields=["contact_role", "updated_at"])
                lead.client = client
                lead.save()
                if intake_event:
                    intake_event.lead = lead
                    intake_event.save(update_fields=["lead"])
                ClientInteraction.objects.create(
                    client=client,
                    lead=lead,
                    interaction_type="note",
                    text=f"Новая заявка с лендинга «{property.title}» · {lead.get_contact_purpose_display()} · {lead.get_interest_type_display()}.",
                )
                if not property.is_demo:
                    lead_url = request.build_absolute_uri(reverse("lead_detail", kwargs={"pk": lead.pk}))
                    LeadNotificationService.notify_new_lead(lead, profile, lead_url)
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


@method_decorator(xframe_options_sameorigin, name="dispatch")
class PropertyLandingDraftPreviewView(LoginRequiredMixin, PublicLandingView):
    """Render the submitted editor values without saving or publishing them."""
    http_method_names = ["post", "options"]

    def post(self, request, pk=None):
        if pk is None:
            # A temporary key lets the public templates query an empty image set.
            property = Property(pk=-1, owner=request.user, agency=agency_for(request.user))
        else:
            saved = get_object_or_404(
                workspace_queryset(Property.objects.select_related("owner", "agency").prefetch_related("images"), request.user),
                pk=pk,
            )
            property = copy.copy(saved)

        editor = PropertyForm(request.POST, request.FILES, instance=property, user=request.user)
        if not editor.is_valid():
            response = render(request, "properties/landing_preview_invalid.html", {"editor": editor}, status=422)
        else:
            property = editor.save(commit=False)
            uploaded_logo = editor.cleaned_data.get("landing_logo")
            preview_logo_url = None
            if uploaded_logo:
                preview_logo_url = "data:image/png;base64," + base64.b64encode(uploaded_logo.read()).decode("ascii")
            profile = RealtorProfile.objects.filter(user_id=property.owner_id).first()
            response = render(
                request,
                self.get_template_name(property),
                self.get_context(
                    property,
                    profile,
                    LeadForm(property=property),
                    preview=True,
                    preview_logo_url=preview_logo_url,
                    turnstile_enabled=False,
                ),
            )
        response["Cache-Control"] = "no-store"
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response
