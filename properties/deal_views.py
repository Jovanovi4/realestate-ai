from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import F, Prefetch
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import CreateView, TemplateView, UpdateView

from .forms import DealForm
from .agency_access import workspace_queryset
from .models import ClientInteraction, ClientReminder, Deal, Lead, Showing


class DealBoardView(LoginRequiredMixin, TemplateView):
    template_name = "properties/deal_board.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        deals = list(
            workspace_queryset(Deal.objects, self.request.user)
            .select_related("client", "property", "responsible")
            .prefetch_related(
                Prefetch("showings", queryset=Showing.objects.filter(status="planned").order_by("starts_at"), to_attr="planned_showings"),
                Prefetch("tasks", queryset=ClientReminder.objects.filter(is_done=False).order_by(F("due_at").asc(nulls_last=True)), to_attr="open_tasks"),
            )
            .order_by("-updated_at")
        )
        now = timezone.now()
        for deal in deals:
            deal.next_showing = next(
                (showing for showing in deal.planned_showings if showing.starts_at >= now),
                deal.planned_showings[0] if deal.planned_showings else None,
            )
            deal.next_task = deal.open_tasks[0] if deal.open_tasks else None
        context["columns"] = [
            (stage, label, [deal for deal in deals if deal.stage == stage])
            for stage, label in Deal.STAGE_CHOICES
        ]
        context["active_count"] = sum(deal.stage in Deal.ACTIVE_STAGES for deal in deals)
        return context


class DealFormMixin(LoginRequiredMixin):
    model = Deal
    form_class = DealForm
    template_name = "properties/deal_form.html"

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "owner": self.request.user}

    def get_success_url(self):
        return reverse("deal_detail", kwargs={"pk": self.object.pk})


class DealCreateView(DealFormMixin, CreateView):
    def get_initial(self):
        initial = super().get_initial()
        lead_id = self.request.GET.get("lead", "")
        if lead_id.isdigit():
            lead = get_object_or_404(
                workspace_queryset(Lead.objects.select_related("client"), self.request.user, prefix="property__"),
                pk=lead_id,
            )
            if lead.client_id:
                initial.update({"lead": lead, "client": lead.client, "property": lead.property})
                if lead.assigned_to_id:
                    initial["responsible"] = lead.assigned_to
            return initial
        for key in ("client", "property"):
            value = self.request.GET.get(key, "")
            if value.isdigit():
                initial[key] = value
        return initial

    def form_valid(self, form):
        form.instance.owner = self.request.user
        form.instance.agency = form.cleaned_data["property"].agency
        if not form.instance.responsible_id:
            form.instance.responsible = self.request.user
        with transaction.atomic():
            response = super().form_valid(form)
            ClientInteraction.objects.create(
                client=self.object.client,
                lead=self.object.lead,
                interaction_type="status",
                text=f"Создана сделка по объекту «{self.object.property.title}».",
            )
        messages.success(self.request, "Сделка создана.")
        return response


class DealUpdateView(DealFormMixin, UpdateView):
    context_object_name = "deal"

    def get_queryset(self):
        return workspace_queryset(Deal.objects, self.request.user).select_related("client", "property", "lead", "responsible")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["deal_tasks"] = self.object.tasks.filter(is_done=False).order_by(F("due_at").asc(nulls_last=True))[:5]
        context["deal_showings"] = self.object.showings.order_by("-starts_at")[:5]
        return context

    def form_valid(self, form):
        previous_stage = Deal.objects.values_list("stage", flat=True).get(pk=self.object.pk)
        with transaction.atomic():
            response = super().form_valid(form)
            if previous_stage != self.object.stage:
                ClientInteraction.objects.create(
                    client=self.object.client,
                    lead=self.object.lead,
                    interaction_type="status",
                    text=(
                        f"Сделка по объекту «{self.object.property.title}»: "
                        f"{dict(Deal.STAGE_CHOICES)[previous_stage]} → {self.object.get_stage_display()}."
                    ),
                )
        messages.success(self.request, "Изменения в сделке сохранены.")
        return response
