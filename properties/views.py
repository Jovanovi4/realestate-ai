from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from .forms import PropertyForm, RegistrationForm
from .models import Property, PropertyImage


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
