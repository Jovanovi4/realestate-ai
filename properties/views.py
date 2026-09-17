from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import LeadForm, LeadStatusForm, PropertyForm, RegistrationForm, RealtorProfileForm
from .models import Lead, Property, PropertyImage, RealtorProfile


class PropertyListView(LoginRequiredMixin, ListView):
    model = Property
    template_name = "properties/property_list.html"
    context_object_name = "properties"

    def get_queryset(self):
        return Property.objects.filter(owner=self.request.user)


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
        return reverse_lazy("property_list")


class LeadListView(LoginRequiredMixin, ListView):
    model = Lead
    template_name = "properties/lead_list.html"
    context_object_name = "leads"
    paginate_by = 25

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
        for image in request.FILES.getlist("images"):
            PropertyImage.objects.create(property=property, image=image)
        return redirect("property_detail", pk=property.pk)


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

    def get_property(self, slug):
        return get_object_or_404(
            Property.objects.select_related("owner").prefetch_related("images"),
            landing_slug=slug,
            landing_published=True,
        )

    def get(self, request, slug):
        property = self.get_property(slug)
        profile = RealtorProfile.objects.filter(user=property.owner).first()
        return render(request, self.get_template_name(property), {"property": property, "profile": profile, "form": LeadForm()})

    def post(self, request, slug):
        property = self.get_property(slug)
        profile = RealtorProfile.objects.filter(user=property.owner).first()
        form = LeadForm(request.POST)
        if form.is_valid():
            lead = form.save(commit=False)
            lead.property = property
            lead.save()
            return render(request, self.get_template_name(property), {"property": property, "profile": profile, "form": LeadForm(), "sent": True})
        return render(request, self.get_template_name(property), {"property": property, "profile": profile, "form": form})
