from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from billing.enforcement import TariffDenied, require_resource_action
from billing.rules import Operation

from .agency_access import workspace_queryset
from .forms import ClientInteractionForm, OwnerContactForm, PropertyOwnerAttachForm, PropertyOwnerDetailsForm, PropertyOwnerSetupForm
from .models import Client, ClientReminder, Property, PropertyOwnerLink


def owner_link_for(user, property_pk, link_pk):
    property = get_object_or_404(workspace_queryset(Property.objects, user), pk=property_pk)
    link = get_object_or_404(PropertyOwnerLink.objects.select_related("client"), pk=link_pk, property=property)
    return property, link


def owner_access_error(request, property):
    try:
        require_resource_action(request.user, property, Operation.EDIT_CRM)
    except TariffDenied as error:
        messages.error(request, error.message)
        return True
    return False


def render_owner_settings(request, property, *, setup_form=None, details_form=None, status=200):
    links = list(property.owner_links.select_related("client"))
    for link in links:
        link.details_form = (
            details_form if details_form is not None and details_form.instance.pk == link.pk
            else PropertyOwnerDetailsForm(instance=link, prefix=f"owner-{link.pk}")
        )
    return render(request, "properties/property_owner_settings.html", {
        "property": property,
        "owner_links": links,
        "owner_setup_form": setup_form or PropertyOwnerSetupForm(user=request.user, property=property),
    }, status=status)


class PropertyOwnerSettingsView(LoginRequiredMixin, View):
    def get(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        return render_owner_settings(request, property)

    def post(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        if owner_access_error(request, property):
            return redirect("property_owner_settings", pk=pk)
        form = PropertyOwnerSetupForm(request.POST, user=request.user, property=property)
        if not form.is_valid():
            return render_owner_settings(request, property, setup_form=form, status=400)
        data = form.cleaned_data
        try:
            with transaction.atomic():
                client = data["client"]
                if client is None:
                    client = Client.objects.create(
                        owner=request.user, agency=property.agency,
                        name=data["contact_name"].strip(), phone=data["contact_phone"],
                        source="manual", contact_role="owner",
                    )
                PropertyOwnerLink.objects.create(
                    property=property, client=client, role=data["role"],
                    agreed_price=data["agreed_price"],
                    agreement_expires_on=data["agreement_expires_on"],
                    showing_instructions=data["showing_instructions"],
                    publication_approved_at=timezone.now() if data["publication_approved"] else None,
                )
        except (IntegrityError, ValidationError):
            form.add_error(None, "Не удалось связать собственника с объектом. Проверьте контакт и условия.")
            return render_owner_settings(request, property, setup_form=form, status=400)
        messages.success(request, "Собственник и условия работы сохранены.")
        return redirect("property_owner_settings", pk=pk)


class PropertyOwnerAttachView(LoginRequiredMixin, View):
    def post(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        destination = reverse("property_owner_settings", args=[pk])
        if owner_access_error(request, property):
            return redirect(destination)
        form = PropertyOwnerAttachForm(request.POST, user=request.user, property=property)
        if not form.is_valid():
            messages.error(request, "Выберите доступный контакт и его роль по объекту.")
            return redirect(destination)
        client = form.cleaned_data["client"]
        try:
            with transaction.atomic():
                PropertyOwnerLink.objects.create(property=property, client=client, role=form.cleaned_data["role"])
        except (IntegrityError, ValidationError):
            messages.info(request, "Этот контакт уже связан с объектом.")
        else:
            messages.success(request, "Контакт связан с объектом.")
        return redirect(destination)


class PropertyOwnerCreateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        property = get_object_or_404(workspace_queryset(Property.objects, request.user), pk=pk)
        destination = reverse("property_owner_settings", args=[pk])
        if owner_access_error(request, property):
            return redirect(destination)
        form = OwnerContactForm(request.POST)
        role = request.POST.get("role", "owner")
        if not form.is_valid() or role not in dict(PropertyOwnerLink.ROLE_CHOICES):
            messages.error(request, "Проверьте имя, телефон и роль собственника.")
            return redirect(destination)
        scope = Client.objects.filter(agency=property.agency)
        if property.agency_id is None:
            scope = scope.filter(owner=property.owner)
        if scope.filter(phone=form.cleaned_data["phone"]).exists():
            messages.error(request, "Контакт с этим телефоном уже есть. Выберите его из существующих контактов.")
            return redirect(destination)
        try:
            with transaction.atomic():
                client = form.save(commit=False)
                client.owner = request.user
                client.agency = property.agency
                client.source = "manual"
                client.contact_role = "owner"
                client.save()
                PropertyOwnerLink.objects.create(property=property, client=client, role=role)
        except (IntegrityError, ValidationError):
            messages.error(request, "Контакт с этим телефоном уже существует. Выберите его из списка.")
            return redirect(destination)
        else:
            messages.success(request, "Собственник добавлен к объекту.")
        return redirect(destination)


class PropertyOwnerUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk, link_pk):
        property, link = owner_link_for(request.user, pk, link_pk)
        destination = reverse("property_owner_settings", args=[pk])
        if owner_access_error(request, property):
            return redirect(destination)
        form = PropertyOwnerDetailsForm(request.POST, instance=link, prefix=f"owner-{link.pk}")
        if not form.is_valid():
            return render_owner_settings(request, property, details_form=form, status=400)
        link = form.save(commit=False)
        if form.cleaned_data["publication_approved"]:
            link.publication_approved_at = link.publication_approved_at or timezone.now()
        else:
            link.publication_approved_at = None
        link.save()
        messages.success(request, "Условия по объекту сохранены.")
        return redirect(destination)


class PropertyOwnerRemoveView(LoginRequiredMixin, View):
    def post(self, request, pk, link_pk):
        property, link = owner_link_for(request.user, pk, link_pk)
        destination = reverse("property_owner_settings", args=[pk])
        if owner_access_error(request, property):
            return redirect(destination)
        with transaction.atomic():
            ClientReminder.objects.filter(
                client=link.client, property=property, owner_showing__isnull=False,
                is_done=False, cancelled_at__isnull=True,
            ).update(cancelled_at=timezone.now())
            link.delete()
        messages.success(request, "Связь с собственником удалена. Контакт и история общения сохранены.")
        return redirect(destination)


class PropertyOwnerInteractionView(LoginRequiredMixin, View):
    def post(self, request, pk, link_pk):
        property, link = owner_link_for(request.user, pk, link_pk)
        destination = f"{reverse('client_detail', args=[link.client_id])}#client-history"
        if owner_access_error(request, property):
            return redirect(destination)
        form = ClientInteractionForm(request.POST)
        if not form.is_valid() or form.cleaned_data["interaction_type"] not in {"call", "message", "note"} or not form.cleaned_data["text"].strip():
            messages.error(request, "Укажите тип и содержание контакта.")
            return redirect(destination)
        interaction = form.save(commit=False)
        interaction.client = link.client
        interaction.property = property
        interaction.save()
        if interaction.interaction_type in {"call", "message"} and not link.client.first_contacted_at:
            link.client.first_contacted_at = interaction.created_at
            link.client.save(update_fields=["first_contacted_at", "updated_at"])
        messages.success(request, "Контакт с собственником записан.")
        return redirect(destination)
