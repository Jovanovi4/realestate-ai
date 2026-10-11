from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View

from billing.enforcement import TariffDenied, require_resource_action
from billing.rules import Operation

from .models import ClientInteraction, ClientReminder, PropertyOwnerLink, Showing


def showing_feedback_url(showing_id):
    return f"{reverse('showing_detail', args=[showing_id])}#owner-feedback"


class OwnerFeedbackPlanForm(forms.Form):
    due_at = forms.DateTimeField(
        label="Когда сообщить", input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control form-control-sm"},
                                   format="%Y-%m-%dT%H:%M"),
    )

    def clean_due_at(self):
        value = self.cleaned_data["due_at"]
        if value <= timezone.now():
            raise ValidationError("Выберите время в будущем.")
        return value


class OwnerFeedbackPlanView(LoginRequiredMixin, View):
    def post(self, request, pk, link_pk):
        destination = showing_feedback_url(pk)
        form = OwnerFeedbackPlanForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Укажите будущий срок сообщения собственнику.")
            return redirect(destination)
        try:
            with transaction.atomic():
                showing = get_object_or_404(
                    Showing.objects.select_for_update().select_related("deal__property"), pk=pk, owner=request.user,
                )
                if showing.status != "completed":
                    messages.error(request, "Сначала запишите итог показа.")
                    return redirect(destination)
                link = get_object_or_404(PropertyOwnerLink, pk=link_pk, property_id=showing.deal.property_id)
                require_resource_action(request.user, showing, Operation.EDIT_CRM)
                task = ClientReminder.objects.filter(owner_showing=showing, client=link.client).first()
                if task and task.is_done:
                    messages.info(request, "Собственник уже получил обратную связь по этому показу.")
                    return redirect(destination)
                if task:
                    task.cancelled_at = None
                    task.due_at = form.cleaned_data["due_at"]
                    task.save(update_fields=["cancelled_at", "due_at"])
                else:
                    ClientReminder.objects.create(
                        owner=showing.owner, agency=showing.agency, client=link.client,
                        property=showing.deal.property, owner_showing=showing,
                        text="Сообщить собственнику об итоге показа", due_at=form.cleaned_data["due_at"],
                    )
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect(destination)
        messages.success(request, "Задача «Сообщить собственнику» добавлена.")
        return redirect(destination)


class OwnerFeedbackCompleteView(LoginRequiredMixin, View):
    def post(self, request, pk, task_pk):
        destination = showing_feedback_url(pk)
        note = request.POST.get("feedback_note", "").strip()
        method = request.POST.get("method", "call")
        if not note or len(note) > 2000 or method not in {"call", "message"}:
            messages.error(request, "Запишите, что сообщили собственнику, и выберите способ связи.")
            return redirect(destination)
        try:
            with transaction.atomic():
                task = get_object_or_404(
                    ClientReminder.objects.select_for_update().select_related("owner_showing__deal__property", "client"),
                    pk=task_pk, owner=request.user, owner_showing_id=pk,
                )
                if task.is_done:
                    return redirect(destination)
                if task.cancelled_at or task.owner_showing.status != "completed":
                    messages.error(request, "Показ не завершён или задача отменена.")
                    return redirect(destination)
                require_resource_action(request.user, task, Operation.EDIT_CRM)
                task.is_done = True
                task.completed_at = timezone.now()
                task.outcome_note = note
                task.save(update_fields=["is_done", "completed_at", "outcome_note"])
                interaction = ClientInteraction.objects.create(
                    client=task.client, property=task.property, interaction_type=method,
                    text=f"Сообщён итог показа {timezone.localtime(task.owner_showing.starts_at):%d.%m.%Y}: {note}",
                )
                if not task.client.first_contacted_at:
                    task.client.first_contacted_at = interaction.created_at
                    task.client.save(update_fields=["first_contacted_at", "updated_at"])
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect(destination)
        messages.success(request, "Обратная связь собственнику записана.")
        return redirect(destination)
