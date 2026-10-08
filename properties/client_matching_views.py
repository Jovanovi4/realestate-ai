from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View

from billing.enforcement import TariffDenied, require_resource_action
from billing.rules import Operation

from .agency_access import workspace_queryset
from .forms import ClientPropertyReactionForm, ClientPropertySuggestionForm, ClientSearchForm
from .models import Client, ClientInteraction, ClientPropertySuggestion


class ClientMatchingView(LoginRequiredMixin, View):
    def client_record(self, request, pk):
        return get_object_or_404(workspace_queryset(Client.objects, request.user), pk=pk)

    @staticmethod
    def check_edit(user, client):
        try:
            require_resource_action(user, client, Operation.EDIT_CRM)
        except TariffDenied as error:
            raise PermissionDenied(error.message) from error

    @staticmethod
    def back_to_client(client):
        return redirect(f"{reverse('client_detail', args=[client.pk])}#client-matching")


class ClientSearchUpdateView(ClientMatchingView):
    def post(self, request, pk):
        client = self.client_record(request, pk)
        self.check_edit(request.user, client)
        form = ClientSearchForm(request.POST, instance=client)
        if form.is_valid():
            form.save()
            messages.success(request, "Запрос клиента сохранён.")
        else:
            messages.error(request, "Проверьте сегмент, тип объекта, диапазон площади и валюту бюджета.")
        return self.back_to_client(client)


class ClientPropertySuggestionCreateView(ClientMatchingView):
    def post(self, request, pk):
        client = self.client_record(request, pk)
        self.check_edit(request.user, client)
        form = ClientPropertySuggestionForm(request.POST, owner=request.user, client=client)
        if not form.is_valid():
            messages.error(request, "Не удалось добавить объект. Проверьте, что он доступен в этом пространстве.")
            return self.back_to_client(client)
        with transaction.atomic():
            _, created = ClientPropertySuggestion.objects.get_or_create(
                client=client, property=form.cleaned_data["property"], defaults={"added_by": request.user},
            )
        if created:
            messages.success(request, "Объект добавлен в подборку.")
        return self.back_to_client(client)


class ClientPropertySuggestionSendView(ClientMatchingView):
    def post(self, request, pk, suggestion_pk):
        client = self.client_record(request, pk)
        self.check_edit(request.user, client)
        with transaction.atomic():
            suggestion = get_object_or_404(
                ClientPropertySuggestion.objects.select_for_update().select_related("property"),
                pk=suggestion_pk, client=client,
            )
            if not suggestion.sent_at:
                suggestion.sent_at = timezone.now()
                suggestion.save(update_fields=["sent_at"])
                ClientInteraction.objects.create(
                    client=client, interaction_type="message",
                    text=f"Предложен объект «{suggestion.property.title}». Передача клиенту отмечена вручную.",
                )
        messages.success(request, "Передача объекта клиенту отмечена.")
        return self.back_to_client(client)


class ClientPropertySuggestionReactionView(ClientMatchingView):
    def post(self, request, pk, suggestion_pk):
        client = self.client_record(request, pk)
        self.check_edit(request.user, client)
        with transaction.atomic():
            suggestion = get_object_or_404(
                ClientPropertySuggestion.objects.select_for_update().select_related("property"),
                pk=suggestion_pk, client=client,
            )
            previous = (suggestion.reaction, suggestion.reaction_note)
            form = ClientPropertyReactionForm(request.POST, instance=suggestion, prefix=f"reaction-{suggestion.pk}")
            if not form.is_valid():
                messages.error(request, "Укажите реакцию клиента и проверьте комментарий.")
                return self.back_to_client(client)
            if previous != (form.cleaned_data["reaction"], form.cleaned_data["reaction_note"]):
                suggestion = form.save(commit=False)
                suggestion.reacted_at = timezone.now() if suggestion.reaction != "pending" else None
                suggestion.save()
                detail = f" {suggestion.reaction_note}" if suggestion.reaction_note else ""
                ClientInteraction.objects.create(
                    client=client, interaction_type="note",
                    text=f"Реакция на объект «{suggestion.property.title}»: {suggestion.get_reaction_display()}.{detail}",
                )
        messages.success(request, "Реакция клиента сохранена.")
        return self.back_to_client(client)
