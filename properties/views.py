import json
import csv
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import (
    ClientDetailsForm, ClientForm, ClientInteractionForm, ClientReminderForm, LeadForm,
    PropertyForm, RegistrationForm, RealtorProfileForm,
)
from .models import AIContent, Client, ClientInteraction, ClientReminder, Lead, Property, PropertyImage, RealtorProfile
from .services.avito_service import AvitoExportService


class PropertyListView(LoginRequiredMixin, ListView):
    model = Property
    template_name = "properties/property_list.html"
    context_object_name = "properties"

    def get_template_names(self):
        if self.request.headers.get("HX-Request") == "true":
            return ["properties/includes/property_cards.html"]
        return [self.template_name]

    def get_queryset(self):
        queryset = Property.objects.filter(owner=self.request.user)
        query = self.request.GET.get("q", "").strip()
        property_type = self.request.GET.get("property_type")
        status = self.request.GET.get("status")
        min_price = self.request.GET.get("min_price")
        max_price = self.request.GET.get("max_price")
        sort = self.request.GET.get("sort", "newest")

        if query:
            queryset = queryset.filter(Q(title__icontains=query) | Q(address__icontains=query))
        if property_type in dict(Property.PROPERTY_TYPES):
            queryset = queryset.filter(property_type=property_type)
        if status in dict(Property.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        if min_price:
            queryset = queryset.filter(price__gte=min_price)
        if max_price:
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
        context["status_choices"] = Property.STATUS_CHOICES
        context["filters"] = self.request.GET
        context["sort_choices"] = [
            ("newest", "Сначала новые"),
            ("oldest", "Сначала старые"),
            ("updated", "Недавно обновлённые"),
            ("price_asc", "Цена: по возрастанию"),
            ("price_desc", "Цена: по убыванию"),
        ]
        return context


class PropertyCreateView(LoginRequiredMixin, CreateView):
    model = Property
    form_class = PropertyForm
    template_name = "properties/create_property.html"
    success_url = reverse_lazy("property_list")

    def form_valid(self, form):
        form.instance.owner = self.request.user
        return super().form_valid(form)


class PropertyUpdateView(LoginRequiredMixin, UpdateView):
    model = Property
    form_class = PropertyForm
    template_name = "properties/edit_property.html"
    context_object_name = "property"

    def get_queryset(self):
        return Property.objects.filter(owner=self.request.user)

    def get_success_url(self):
        return reverse_lazy("property_detail", kwargs={"pk": self.object.pk})


class PropertyDetailView(LoginRequiredMixin, DetailView):
    model = Property
    template_name = "properties/property_detail.html"
    context_object_name = "property"

    def get_queryset(self):
        return Property.objects.filter(owner=self.request.user).prefetch_related("images")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        image_count = self.object.images.count()
        tasks = []
        if not self.object.price:
            tasks.append({"text": "Укажите цену объекта", "url": reverse("edit_property", kwargs={"pk": self.object.pk})})
        if not self.object.address:
            tasks.append({"text": "Добавьте адрес объекта", "url": reverse("edit_property", kwargs={"pk": self.object.pk})})
        if image_count < 3:
            tasks.append({"text": f"Добавьте ещё {3 - image_count} фото", "url": reverse("upload_images", kwargs={"pk": self.object.pk})})
        if not (self.object.short_description or self.object.description):
            tasks.append({"text": "Добавьте описание объекта", "url": reverse("edit_property", kwargs={"pk": self.object.pk})})
        if not self.object.landing_published:
            tasks.append({"text": "Настройте и опубликуйте лендинг", "url": f"{reverse('edit_property', kwargs={'pk': self.object.pk})}#landing-pane"})
        context["readiness_tasks"] = tasks
        context["readiness_completed"] = 5 - len(tasks)
        context["image_count"] = image_count
        return context


class LandingListView(LoginRequiredMixin, ListView):
    model = Property
    template_name = "properties/landing_list.html"
    context_object_name = "properties"

    def get_queryset(self):
        queryset = Property.objects.filter(owner=self.request.user).annotate(landing_leads_count=Count("leads"))
        status = self.request.GET.get("status", "")
        if status == "published":
            queryset = queryset.filter(landing_published=True)
        elif status == "draft":
            queryset = queryset.filter(landing_published=False)
        return queryset.order_by("-landing_published", "-updated_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        all_landings = Property.objects.filter(owner=self.request.user)
        context.update(
            {
                "selected_status": self.request.GET.get("status", ""),
                "published_count": all_landings.filter(landing_published=True).count(),
                "draft_count": all_landings.filter(landing_published=False).count(),
            }
        )
        return context


class PropertyLandingUnpublishView(LoginRequiredMixin, View):
    def post(self, request, pk):
        property = get_object_or_404(Property, pk=pk, owner=request.user)
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
        property = get_object_or_404(Property, pk=pk, owner=request.user)
        status = request.POST.get("status")
        if status in dict(Property.STATUS_CHOICES):
            property.status = status
            property.save(update_fields=["status", "updated_at"])
        if request.headers.get("HX-Request") != "true":
            return redirect("property_detail", pk=property.pk)
        return render(request, "properties/includes/property_status_badge.html", {"property": property})


class AvitoExportView(LoginRequiredMixin, View):
    template_name = "properties/avito_export.html"

    def get_properties_and_profile(self):
        properties = Property.objects.filter(
            owner=self.request.user,
            avito_export=True,
        ).prefetch_related("images")
        profile = RealtorProfile.objects.filter(user=self.request.user).first()
        return properties, profile

    def get(self, request):
        properties, profile = self.get_properties_and_profile()
        valid_properties, invalid_properties = AvitoExportService.get_exportable(properties, profile)
        if request.GET.get("download") == "1":
            if not valid_properties:
                return redirect("avito_export")
            xml_content = AvitoExportService.build_xml(
                valid_properties,
                profile,
                request.build_absolute_uri("/").rstrip("/"),
            )
            response = HttpResponse(xml_content, content_type="application/xml; charset=utf-8")
            response["Content-Disposition"] = 'attachment; filename="avito-properties.xml"'
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


class PropertyDeleteView(LoginRequiredMixin, DeleteView):
    model = Property

    def get_queryset(self):
        return Property.objects.filter(owner=self.request.user)

    def get_success_url(self):
        return reverse_lazy("property_list")

    def _delete_image_files(self):
        for property_image in self.object.images.all():
            if property_image.image:
                property_image.image.delete(save=False)

    def form_valid(self, form):
        self._delete_image_files()
        return super().form_valid(form)

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        self._delete_image_files()
        return super().delete(request, *args, **kwargs)


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
        properties = Property.objects.filter(owner=self.request.user)
        context["statistics"] = {
            "properties": properties.count(),
            "published_landings": properties.filter(landing_published=True).count(),
            "new_leads": Lead.objects.filter(property__owner=self.request.user, status="new").count(),
        }
        return context


class ClientListView(LoginRequiredMixin, ListView):
    model = Client
    template_name = "properties/client_list.html"
    context_object_name = "clients"
    paginate_by = 30

    def get_queryset(self):
        queryset = Client.objects.filter(owner=self.request.user).prefetch_related("leads")
        query = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        source = self.request.GET.get("source", "")
        date_from = self.request.GET.get("date_from", "")
        date_to = self.request.GET.get("date_to", "")
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(phone__icontains=query) | Q(preferred_area__icontains=query))
        if status in dict(Client.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        if source in dict(Client.SOURCE_CHOICES):
            queryset = queryset.filter(source=source)
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        all_clients = Client.objects.filter(owner=self.request.user)
        now = timezone.now()
        start_tomorrow = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        pending_reminders = ClientReminder.objects.filter(client__owner=self.request.user, is_done=False).select_related("client")
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
                "metrics": {
                    "new_week": all_clients.filter(created_at__gte=timezone.now() - timedelta(days=7)).count(),
                    "active": all_clients.exclude(status__in=["won", "lost"]).count(),
                    "conversion": round(won / total * 100) if total else 0,
                    "response_hours": round(sum(response_hours) / len(response_hours), 1) if response_hours else None,
                },
                "overdue_reminders": pending_reminders.filter(due_at__lt=now)[:5],
                "today_reminders": pending_reminders.filter(due_at__gte=now, due_at__lt=start_tomorrow)[:5],
            }
        )
        return context


class ClientCreateView(LoginRequiredMixin, CreateView):
    model = Client
    form_class = ClientForm
    template_name = "properties/client_form.html"

    def form_valid(self, form):
        form.instance.owner = self.request.user
        existing_client = Client.objects.filter(owner=self.request.user, phone=form.cleaned_data["phone"]).first()
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
        return Client.objects.filter(owner=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["interaction_form"] = ClientInteractionForm()
        context["reminder_form"] = ClientReminderForm(initial={"due_at": (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")})
        context["interactions"] = self.object.interactions.select_related("lead")
        context["reminders"] = self.object.reminders.all()
        return context

    def get_success_url(self):
        return reverse_lazy("client_detail", kwargs={"pk": self.object.pk})


class ClientStatusUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        client = get_object_or_404(Client, pk=pk, owner=request.user)
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
        client = get_object_or_404(Client, pk=pk, owner=request.user)
        form = ClientInteractionForm(request.POST)
        if form.is_valid():
            interaction = form.save(commit=False)
            interaction.client = client
            interaction.save()
            if interaction.interaction_type in {"call", "message"} and not client.first_contacted_at:
                client.first_contacted_at = interaction.created_at
                client.save(update_fields=["first_contacted_at", "updated_at"])
        return redirect("client_detail", pk=client.pk)


class ClientReminderCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        client = get_object_or_404(Client, pk=pk, owner=request.user)
        form = ClientReminderForm(request.POST)
        if form.is_valid():
            reminder = form.save(commit=False)
            reminder.client = client
            reminder.save()
        return redirect("client_detail", pk=client.pk)


class ClientReminderCompleteView(LoginRequiredMixin, View):
    def post(self, request, pk, reminder_pk):
        client = get_object_or_404(Client, pk=pk, owner=request.user)
        reminder = get_object_or_404(client.reminders, pk=reminder_pk)
        reminder.is_done = True
        reminder.completed_at = timezone.now()
        reminder.save(update_fields=["is_done", "completed_at"])
        return redirect("client_detail", pk=client.pk)


class ClientBulkActionView(LoginRequiredMixin, View):
    def post(self, request):
        client_ids = [int(pk) for pk in request.POST.getlist("client_ids") if pk.isdigit()]
        clients = Client.objects.filter(owner=request.user, pk__in=client_ids)
        action = request.POST.get("action")
        if action == "delete":
            clients.delete()
        return redirect("client_list")


class ClientExportView(LoginRequiredMixin, View):
    def get(self, request):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="clients.csv"'
        response.write("\ufeff")
        writer = csv.writer(response)
        writer.writerow(["Имя", "Телефон", "Статус", "Источник", "Бюджет", "Район", "Удобное время", "Создан"])
        for client in Client.objects.filter(owner=request.user).order_by("-created_at"):
            writer.writerow([client.name, client.phone, client.get_status_display(), client.get_source_display(), client.budget or "", client.preferred_area, client.preferred_contact_time, client.created_at.strftime("%d.%m.%Y %H:%M")])
        return response


class ClientBoardView(LoginRequiredMixin, View):
    def get(self, request):
        clients = Client.objects.filter(owner=request.user).order_by("-updated_at")
        columns = [(value, label, [client for client in clients if client.status == value]) for value, label in Client.STATUS_CHOICES]
        return render(request, "properties/client_board.html", {"columns": columns})


class LeadListView(LoginRequiredMixin, ListView):
    model = Lead
    template_name = "properties/lead_list.html"
    context_object_name = "leads"
    paginate_by = 25

    def get_template_names(self):
        if self.request.headers.get("HX-Request") == "true":
            return ["properties/includes/lead_workspace.html"]
        return [self.template_name]

    def get_queryset(self):
        queryset = Lead.objects.filter(property__owner=self.request.user).select_related("property", "client").prefetch_related("client__leads")
        status = self.request.GET.get("status")
        if status in dict(Lead.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["status_choices"] = Lead.STATUS_CHOICES
        context["selected_status"] = self.request.GET.get("status", "")
        return context


class LeadDetailView(LoginRequiredMixin, DetailView):
    model = Lead
    template_name = "properties/lead_detail.html"
    context_object_name = "lead"

    def get_queryset(self):
        return Lead.objects.filter(property__owner=self.request.user).select_related("property", "client")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["latest_ai_reply"] = self.object.ai_contents.filter(content_type="lead_reply").first()
        return context

class LeadBulkStatusUpdateView(LoginRequiredMixin, View):
    def post(self, request):
        lead_ids = request.POST.getlist("lead_ids")
        action = request.POST.get("action", "status")
        status = request.POST.get("status")
        if not lead_ids:
            messages.error(request, "Выберите хотя бы одну заявку.")
        elif action == "delete":
            leads = Lead.objects.filter(pk__in=lead_ids, property__owner=request.user)
            deleted_count = leads.count()
            leads.delete()
            messages.success(request, f"Удалено заявок: {deleted_count}.")
        elif status not in dict(Lead.STATUS_CHOICES):
            messages.error(request, "Выберите корректный статус обработки.")
        else:
            leads = Lead.objects.filter(pk__in=lead_ids, property__owner=request.user)
            updated = leads.exclude(status=status).update(status=status, updated_at=timezone.now())
            messages.success(request, f"Статус обновлён у {updated} заявок.")

        next_url = request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            return redirect(next_url)
        return redirect("lead_list")


class LeadDeleteView(LoginRequiredMixin, DeleteView):
    model = Lead

    def get_queryset(self):
        return Lead.objects.filter(property__owner=self.request.user)

    def get_success_url(self):
        return reverse_lazy("lead_list")


class PropertyImageUploadView(LoginRequiredMixin, View):
    template_name = "properties/upload_images.html"

    def get(self, request, pk):
        property = get_object_or_404(Property, pk=pk, owner=request.user)
        return render(request, self.template_name, {"property": property})

    def post(self, request, pk):
        property = get_object_or_404(Property, pk=pk, owner=request.user)
        next_order = (property.images.aggregate(max_order=Max("order"))["max_order"] or -1) + 1
        has_primary = property.images.filter(is_primary=True).exists()
        for image in request.FILES.getlist("images"):
            PropertyImage.objects.create(
                property=property,
                image=image,
                order=next_order,
                is_primary=not has_primary,
            )
            has_primary = True
            next_order += 1
        return redirect("property_detail", pk=property.pk)


class PropertyImagePrimaryView(LoginRequiredMixin, View):
    def post(self, request, pk, image_pk):
        property = get_object_or_404(Property, pk=pk, owner=request.user)
        image = get_object_or_404(property.images, pk=image_pk)
        with transaction.atomic():
            property.images.update(is_primary=False)
            image.is_primary = True
            image.save(update_fields=["is_primary"])
        if request.headers.get("HX-Request") == "true":
            return render(request, "properties/includes/property_gallery_card.html", {"property": property})
        return redirect("property_detail", pk=property.pk)


class PropertyImageDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk, image_pk):
        property = get_object_or_404(Property, pk=pk, owner=request.user)
        image = get_object_or_404(property.images, pk=image_pk)
        image_file = image.image
        image.delete()
        if image_file:
            image_file.delete(save=False)
        property.ensure_primary_image()
        if request.headers.get("HX-Request") == "true":
            return render(request, "properties/includes/property_gallery_card.html", {"property": property})
        return redirect("property_detail", pk=property.pk)


class PropertyImageReorderView(LoginRequiredMixin, View):
    def post(self, request, pk):
        property = get_object_or_404(Property, pk=pk, owner=request.user)
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
        PropertyImage.objects.bulk_update(existing_images, ["order"])
        return JsonResponse({"ok": True})


def register(request):
    if request.user.is_authenticated:
        return redirect("property_list")

    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("property_list")
    return render(request, "registration/register.html", {"form": form})


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
            **extra,
        }

    def get_property(self, slug):
        return get_object_or_404(
            Property.objects.select_related("owner").prefetch_related("images"),
            landing_slug=slug,
            landing_published=True,
        )

    def get(self, request, slug):
        property = self.get_property(slug)
        profile = RealtorProfile.objects.filter(user=property.owner).first()
        return render(request, self.get_template_name(property), self.get_context(property, profile, LeadForm()))

    def post(self, request, slug):
        property = self.get_property(slug)
        profile = RealtorProfile.objects.filter(user=property.owner).first()
        form = LeadForm(request.POST)
        if form.is_valid():
            lead = form.save(commit=False)
            lead.property = property
            client, created = Client.objects.get_or_create(
                owner=property.owner,
                phone=form.cleaned_data["phone"],
                defaults={"name": form.cleaned_data["name"], "source": "landing"},
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
                text=f"Новая заявка с лендинга «{property.title}».",
            )
            return render(request, self.get_template_name(property), self.get_context(property, profile, LeadForm(), sent=True))
        return render(request, self.get_template_name(property), self.get_context(property, profile, form))


@method_decorator(xframe_options_sameorigin, name="dispatch")
class PropertyLandingPreviewView(LoginRequiredMixin, PublicLandingView):
    def get(self, request, pk):
        property = get_object_or_404(Property.objects.select_related("owner").prefetch_related("images"), pk=pk, owner=request.user)
        profile = RealtorProfile.objects.filter(user=request.user).first()
        return render(request, self.get_template_name(property), self.get_context(property, profile, LeadForm(), preview=True))

    def post(self, request, pk):
        return self.get(request, pk)
