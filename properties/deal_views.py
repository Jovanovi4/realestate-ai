from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import F, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import CreateView, TemplateView, UpdateView
from billing.enforcement import TariffDenied, require_resource_action
from billing.rules import Operation

from .forms import DealForm
from .agency_access import workspace_queryset
from .board_pagination import paged_board_columns
from .models import ClientInteraction, ClientReminder, Deal, Lead, Showing
from .services.lead_outcome_service import convert_lead_to_deal
from .services.client_relationship_service import activate_client_relationship


class DealBoardView(LoginRequiredMixin, TemplateView):
    template_name = "properties/deal_board.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        deals = workspace_queryset(Deal.objects, self.request.user)
        now = timezone.now()
        planned = Showing.objects.filter(deal_id=OuterRef("pk"), status="planned")
        open_tasks = ClientReminder.objects.filter(deal_id=OuterRef("pk"), is_done=False).order_by(
            F("due_at").asc(nulls_last=True), "pk",
        )

        def hydrate(ids):
            return deals.filter(pk__in=ids).select_related(
                "client", "property", "responsible__realtor_profile",
            ).annotate(
                next_showing_at=Coalesce(
                    Subquery(planned.filter(starts_at__gte=now).order_by("starts_at", "pk").values("starts_at")[:1]),
                    Subquery(planned.order_by("starts_at", "pk").values("starts_at")[:1]),
                ),
                next_task_text=Subquery(open_tasks.values("text")[:1]),
                next_task_due_at=Subquery(open_tasks.values("due_at")[:1]),
            )

        columns = paged_board_columns(
            self.request, choices=Deal.STAGE_CHOICES, queryset=deals, field="stage", hydrate=hydrate,
            anchor_prefix="deal-board",
        )
        context["columns"] = columns
        context["active_count"] = sum(column["count"] for column in columns if column["key"] in Deal.ACTIVE_STAGES)
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
        try:
            require_resource_action(self.request.user, form.cleaned_data["property"], Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        form.instance.owner = self.request.user
        form.instance.agency = form.cleaned_data["property"].agency
        if not form.instance.responsible_id:
            form.instance.responsible = self.request.user
        with transaction.atomic():
            lead = form.cleaned_data.get("lead")
            if lead:
                lead = get_object_or_404(
                    workspace_queryset(Lead.objects.select_for_update(), self.request.user, prefix="property__"), pk=lead.pk,
                )
                if lead.status == "archived" or lead.outcome != "open" or lead.qualification != "qualified" or not lead.first_contacted_at:
                    form.add_error("lead", "Заявка уже завершена либо не прошла первый контакт и квалификацию.")
                    return self.render_to_response(self.get_context_data(form=form))
            response = super().form_valid(form)
            if lead:
                convert_lead_to_deal(lead, self.object)
            else:
                if self.object.stage in Deal.ACTIVE_STAGES:
                    activate_client_relationship(
                        self.object.client_id, reason=f"Создана сделка по объекту «{self.object.property.title}».",
                    )
                ClientInteraction.objects.create(
                    client=self.object.client,
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
        context["deal_tasks"] = self.object.tasks.filter(is_done=False, cancelled_at__isnull=True).select_related("owner__realtor_profile").order_by(F("due_at").asc(nulls_last=True))[:5]
        context["deal_showings"] = self.object.showings.select_related("owner__realtor_profile").order_by("-starts_at")[:5]
        return context

    def form_valid(self, form):
        try:
            require_resource_action(self.request.user, self.object.property, Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        with transaction.atomic():
            previous = Deal.objects.select_for_update().only("stage", "responsible_id").get(pk=self.object.pk)
            response = super().form_valid(form)
            if previous.responsible_id != self.object.responsible_id and self.object.responsible_id:
                self.object.tasks.filter(
                    owner_id=previous.responsible_id, is_done=False, cancelled_at__isnull=True,
                    lead__isnull=True,
                ).update(owner_id=self.object.responsible_id)
                self.object.showings.filter(
                    owner_id=previous.responsible_id, status="planned",
                ).update(owner_id=self.object.responsible_id)
                ClientInteraction.objects.create(
                    client=self.object.client,
                    lead=self.object.lead,
                    interaction_type="status",
                    text=f"Ответственный за сделку: {previous.responsible or 'Не назначен'} → {self.object.responsible}.",
                )
            if previous.stage != self.object.stage:
                if previous.stage in Deal.CLOSED_STAGES and self.object.stage in Deal.ACTIVE_STAGES:
                    activate_client_relationship(
                        self.object.client_id, reason=f"Возобновлена сделка по объекту «{self.object.property.title}».",
                    )
                ClientInteraction.objects.create(
                    client=self.object.client,
                    lead=self.object.lead,
                    interaction_type="status",
                    text=(
                        f"Сделка по объекту «{self.object.property.title}»: "
                        f"{dict(Deal.STAGE_CHOICES)[previous.stage]} → {self.object.get_stage_display()}."
                    ),
                )
        messages.success(self.request, "Изменения в сделке сохранены.")
        return response
