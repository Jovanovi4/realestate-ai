from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, TemplateView, UpdateView
from billing.enforcement import TariffDenied, require_resource_action
from billing.rules import Operation

from .forms import DealForm
from .agency_access import agency_for, may_manage_agency, workspace_queryset
from .board_pagination import paged_board_columns
from .models import ClientInteraction, ClientReminder, Deal, Lead, Showing
from .services.lead_outcome_service import convert_lead_to_deal
from .services.client_relationship_service import activate_client_relationship


class DealBoardView(LoginRequiredMixin, TemplateView):
    template_name = "properties/deal_board.html"

    def board_filters(self):
        if hasattr(self, "_board_filters"):
            return self._board_filters
        params = {}
        stage = self.request.GET.get("stage", "")
        if stage in dict(Deal.STAGE_CHOICES):
            params["stage"] = stage
        scope = self.request.GET.get("scope", "")
        agency = agency_for(self.request.user)
        if scope == "personal":
            params["scope"] = "personal"
        elif scope == "team" and agency:
            params["scope"] = "team"
        if self.request.GET.get("assigned") == "mine":
            params["assigned"] = "mine"
        responsible = self.request.GET.get("responsible", "")
        if agency and responsible.isdecimal() and may_manage_agency(self.request.user):
            if agency.memberships.filter(user_id=int(responsible)).exists():
                params["responsible"] = responsible
        self._board_filters = params
        return params

    def filtered_deals(self):
        queryset = workspace_queryset(Deal.objects, self.request.user)
        params = self.board_filters()
        agency = agency_for(self.request.user)
        if params.get("scope") == "personal":
            queryset = queryset.filter(agency__isnull=True, owner=self.request.user)
        elif params.get("scope") == "team":
            queryset = queryset.filter(agency=agency)
        if params.get("assigned") == "mine":
            queryset = queryset.filter(responsible=self.request.user)
        if params.get("responsible"):
            queryset = queryset.filter(responsible_id=int(params["responsible"]))
        if params.get("stage"):
            queryset = queryset.filter(stage=params["stage"])
        if params:
            queryset = queryset.filter(is_demo=False, property__is_demo=False, client__is_demo=False).filter(
                Q(lead__isnull=True) | Q(lead__is_demo=False),
            )
        return queryset

    def panel_context(self):
        query = self.request.GET.copy()
        query.pop("deal", None)
        context = {"deal_panel_query": query.urlencode()}
        raw_id = self.request.GET.get("deal", "")
        if not raw_id:
            return context
        if not raw_id.isdigit():
            raise Http404("Сделка не найдена")
        deal = get_object_or_404(
            workspace_queryset(
                Deal.objects.select_related("client", "property", "lead", "responsible__realtor_profile"),
                self.request.user,
            ), pk=int(raw_id),
        )
        now = timezone.now()
        next_task = deal.tasks.filter(is_done=False, cancelled_at__isnull=True).select_related(
            "owner__realtor_profile",
        ).order_by(F("due_at").asc(nulls_last=True), "pk").first()
        next_showing = deal.showings.filter(status="planned", starts_at__gte=now).order_by("starts_at", "pk").first()
        context.update({
            "selected_deal": deal,
            "deal_panel_task": next_task,
            "deal_panel_showing": next_showing,
            "deal_panel_showing_first": bool(
                next_showing and (not next_task or not next_task.due_at or next_showing.starts_at < next_task.due_at)
            ),
            "deal_panel_now": now,
        })
        newer_or_equal = Q(updated_at__lt=deal.updated_at) | Q(updated_at=deal.updated_at, pk__lt=deal.pk)
        visible_deals = self.filtered_deals().filter(stage=deal.stage)
        next_id = visible_deals.filter(newer_or_equal).order_by("-updated_at", "-pk").values_list("pk", flat=True).first()
        if next_id is None:
            next_id = visible_deals.exclude(pk=deal.pk).order_by("-updated_at", "-pk").values_list("pk", flat=True).first()
        context["next_deal_id"] = next_id
        return context

    def get(self, request, *args, **kwargs):
        if request.headers.get("X-Deal-Panel") == "1":
            return render(request, "properties/includes/deal_panel_slot.html", self.panel_context())
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.panel_context())
        deals = self.filtered_deals()
        board_filters = self.board_filters()
        context["analytics_board_filtered"] = bool(board_filters)
        context["analytics_scope_label"] = (
            "Команда" if board_filters.get("scope") == "team" else
            "Личное пространство" if board_filters.get("scope") == "personal" else
            "Мои сделки" if board_filters.get("assigned") == "mine" else
            "Выбранные сделки"
        )
        context["analytics_stage_filter"] = board_filters.get("stage")
        context["analytics_stage_label"] = dict(Deal.STAGE_CHOICES).get(context["analytics_stage_filter"], "")
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
        context["has_deals"] = bool(context.get("selected_deal")) or any(column["count"] for column in columns)
        context["active_stages"] = Deal.ACTIVE_STAGES
        context["active_count"] = (
            sum(column["count"] for column in columns)
            if context["analytics_stage_filter"] else
            sum(column["count"] for column in columns if column["key"] in Deal.ACTIVE_STAGES)
        )
        context["closed_count"] = sum(column["count"] for column in columns if column["key"] in Deal.CLOSED_STAGES)
        return context


def record_deal_stage_change(deal, previous_stage):
    if previous_stage == deal.stage:
        return
    if previous_stage in Deal.CLOSED_STAGES and deal.stage in Deal.ACTIVE_STAGES:
        activate_client_relationship(
            deal.client_id, reason=f"Возобновлена сделка по объекту «{deal.property.title}».",
        )
    ClientInteraction.objects.create(
        client=deal.client,
        lead=deal.lead,
        interaction_type="status",
        text=(
            f"Сделка по объекту «{deal.property.title}»: "
            f"{dict(Deal.STAGE_CHOICES)[previous_stage]} → {deal.get_stage_display()}."
        ),
    )


class DealStageUpdateView(LoginRequiredMixin, View):
    """Move a visible deal between board columns without editing its other fields."""

    def post(self, request, pk):
        visible = get_object_or_404(
            workspace_queryset(Deal.objects.select_related("property"), request.user), pk=pk,
        )
        target = request.POST.get("stage", "")
        expected = request.POST.get("expected_stage", "")
        stages = {key for key, _ in Deal.STAGE_CHOICES}
        if target not in stages or expected not in stages:
            return JsonResponse({"error": "Выберите этап сделки заново."}, status=400)
        outcome_note = request.POST.get("outcome_note", "").strip()
        if target == "lost" and not outcome_note:
            return JsonResponse({"error": "Укажите причину отказа."}, status=400)

        try:
            require_resource_action(request.user, visible.property, Operation.EDIT_CRM)
        except TariffDenied as error:
            return JsonResponse({"error": error.message}, status=403)

        try:
            with transaction.atomic():
                deal = get_object_or_404(
                    workspace_queryset(
                        Deal.objects.select_for_update().select_related("client", "property", "lead"), request.user,
                    ),
                    pk=pk,
                )
                if deal.stage != expected:
                    return JsonResponse({"error": "Этап сделки уже изменился. Обновите страницу и повторите перенос."}, status=409)
                if target == deal.stage:
                    return JsonResponse({"stage": deal.stage, "changed": False})
                previous_stage = deal.stage
                deal.stage = target
                update_fields = ["stage", "updated_at"]
                if target == "lost":
                    deal.outcome_note = outcome_note
                    update_fields.append("outcome_note")
                deal.save(update_fields=update_fields)
                record_deal_stage_change(deal, previous_stage)
        except (ValidationError, IntegrityError):
            return JsonResponse({"error": "Не удалось перенести сделку. Проверьте, нет ли другой активной сделки по этому клиенту и объекту."}, status=409)
        return JsonResponse({"stage": target, "changed": True})


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
                workspace_queryset(Lead.objects.select_related("client"), self.request.user),
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
                    workspace_queryset(Lead.objects.select_for_update(), self.request.user), pk=lead.pk,
                )
                if lead.status == "archived" or lead.outcome != "open" or lead.qualification != "qualified" or not lead.first_contacted_at:
                    form.add_error("lead", "Заявка уже завершена либо не прошла первый контакт и квалификацию.")
                    return self.render_to_response(self.get_context_data(form=form))
            response = super().form_valid(form)
            if self.object.client.contact_role == "owner":
                self.object.client.contact_role = "both"
                self.object.client.save(update_fields=["contact_role", "updated_at"])
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
        context["owner_links"] = self.object.property.owner_links.select_related("client").all()
        context["deal_tasks"] = list(self.object.tasks.filter(is_done=False, cancelled_at__isnull=True).select_related("owner__realtor_profile").order_by(F("due_at").asc(nulls_last=True))[:5])
        context["deal_showings"] = self.object.showings.select_related("owner__realtor_profile").order_by("-starts_at")[:5]
        next_task = context["deal_tasks"][0] if context["deal_tasks"] else None
        now = timezone.now()
        next_showing = self.object.showings.filter(status="planned", starts_at__gte=now).order_by("starts_at").first()
        if next_showing and (not next_task or not next_task.due_at or next_showing.starts_at < next_task.due_at):
            context["deal_next_showing"] = next_showing
        else:
            context["deal_next_task"] = next_task
            context["deal_next_overdue"] = bool(next_task and next_task.due_at and next_task.due_at < now)
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
                for task in self.object.tasks.filter(
                    owner_id=previous.responsible_id, is_done=False, cancelled_at__isnull=True,
                    lead__isnull=True,
                ).select_for_update():
                    task.owner_id = self.object.responsible_id
                    task.save(update_fields=["owner"])
                for showing in self.object.showings.filter(
                    owner_id=previous.responsible_id, status="planned",
                ).select_for_update():
                    showing.owner_id = self.object.responsible_id
                    showing.save(update_fields=["owner"])
                ClientInteraction.objects.create(
                    client=self.object.client,
                    lead=self.object.lead,
                    interaction_type="status",
                    text=f"Ответственный за сделку: {previous.responsible or 'Не назначен'} → {self.object.responsible}.",
                )
            if previous.stage != self.object.stage:
                record_deal_stage_change(self.object, previous.stage)
        messages.success(self.request, "Изменения в сделке сохранены.")
        return response
