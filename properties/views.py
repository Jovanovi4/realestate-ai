import json

from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Max, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import LeadForm, LeadStatusForm, PropertyForm, RegistrationForm, RealtorProfileForm
from .models import Lead, Property, PropertyImage, RealtorProfile
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
        return Property.objects.filter(owner=self.request.user)


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
        queryset = Lead.objects.filter(property__owner=self.request.user).select_related("property")
        status = self.request.GET.get("status")
        if status in dict(Lead.STATUS_CHOICES):
            queryset = queryset.filter(status=status)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["status_choices"] = Lead.STATUS_CHOICES
        context["selected_status"] = self.request.GET.get("status", "")
        return context


class LeadDetailView(LoginRequiredMixin, UpdateView):
    model = Lead
    form_class = LeadStatusForm
    template_name = "properties/lead_detail.html"
    context_object_name = "lead"

    def get_queryset(self):
        return Lead.objects.filter(property__owner=self.request.user).select_related("property")

    def get_success_url(self):
        return reverse_lazy("lead_detail", kwargs={"pk": self.object.pk})


class LeadStatusUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        lead = get_object_or_404(
            Lead.objects.select_related("property"),
            pk=pk,
            property__owner=request.user,
        )
        status = request.POST.get("status")
        if status in dict(Lead.STATUS_CHOICES):
            lead.status = status
            lead.save(update_fields=["status", "updated_at"])
        if request.headers.get("HX-Request") == "true":
            return render(request, "properties/includes/lead_status_control.html", {"lead": lead})
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
        return {
            "property": property,
            "profile": profile,
            "form": form,
            "landing_order": self.get_landing_order(property),
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
            lead.save()
            return render(request, self.get_template_name(property), self.get_context(property, profile, LeadForm(), sent=True))
        return render(request, self.get_template_name(property), self.get_context(property, profile, form))
