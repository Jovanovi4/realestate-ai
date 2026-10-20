"""Manual requests for a property search and later property attachment."""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from billing.enforcement import TariffDenied, require_new_action, require_resource_action
from billing.rules import Operation

from .agency_access import agency_for, workspace_queryset
from .forms import LeadPropertyForm, ManualLeadForm
from .models import Client, Lead


class ManualLeadCreateView(LoginRequiredMixin, View):
    template_name = "properties/lead_form.html"

    def get(self, request):
        return render(request, self.template_name, {"form": ManualLeadForm(user=request.user)})

    def post(self, request):
        form = ManualLeadForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form}, status=400)
        data = form.cleaned_data
        agency = agency_for(request.user) if data["workspace"] == "team" else None
        try:
            require_new_action(request.user, Operation.EDIT_CRM, agency=agency)
            with transaction.atomic():
                client_filter = {"agency": agency, "phone": data["phone"]}
                if agency is None:
                    client_filter["owner"] = request.user
                client = Client.objects.select_for_update().filter(**client_filter).first()
                if client is None:
                    client = Client.objects.create(
                        owner=request.user, agency=agency, name=data["name"], phone=data["phone"],
                        source=data["source"] if data["source"] in dict(Client.SOURCE_CHOICES) else "other",
                        search_deal_type="rent" if data["interest_type"] == "long_rent" else "sale",
                        search_notes=data["message"].strip(),
                    )
                elif client.contact_role == "owner":
                    client.contact_role = "both"
                    client.save(update_fields=["contact_role", "updated_at"])
                lead = Lead(
                    owner=request.user, agency=agency, client=client, assigned_to=data["assigned_to"],
                    name=data["name"], phone=data["phone"], message=data["message"].strip(),
                    interest_type=data["interest_type"], source=data["source"], source_detail=data["source_detail"].strip(),
                    next_action=data["next_action"].strip(),
                    next_action_due_at=timezone.now() + timedelta(minutes=30),
                )
                lead.save()
        except TariffDenied as error:
            form.add_error(None, error.message)
            return render(request, self.template_name, {"form": form}, status=403)
        except (IntegrityError, ValidationError):
            form.add_error(None, "Не удалось сохранить заявку. Проверьте контакт и повторите попытку.")
            return render(request, self.template_name, {"form": form}, status=400)
        messages.success(request, "Заявка на подбор добавлена. Первый ответ нужен в течение 30 минут.")
        return redirect("lead_detail", pk=lead.pk)


class LeadAttachPropertyView(LoginRequiredMixin, View):
    def post(self, request, pk):
        with transaction.atomic():
            lead = get_object_or_404(
                workspace_queryset(Lead.objects.select_for_update(), request.user), pk=pk,
            )
            if lead.property_id or lead.status == "archived" or lead.outcome != "open":
                raise Http404
            form = LeadPropertyForm(request.POST, lead=lead)
            if not form.is_valid():
                messages.error(request, "Выберите доступный объект из этого пространства.")
                return redirect(f"{reverse('lead_detail', args=[pk])}?attach=1#lead-property-attach")
            try:
                require_resource_action(request.user, lead, Operation.EDIT_CRM)
            except TariffDenied as error:
                messages.error(request, error.message)
                return redirect(f"{reverse('lead_detail', args=[pk])}?attach=1#lead-property-attach")
            lead.property = form.cleaned_data["property"]
            lead.save(update_fields=["property", "updated_at"])
        messages.success(request, "Объект привязан к заявке.")
        return redirect("lead_detail", pk=pk)
