from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, CharField, Count, DateTimeField, ExpressionWrapper, F, IntegerField, Q, Value, When
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import CreateView, TemplateView, UpdateView
from billing.enforcement import TariffDenied, require_new_action, require_resource_action
from billing.rules import Operation

from .forms import ShowingForm, TaskForm
from .agency_access import membership_for, workspace_queryset
from .models import ClientInteraction, ClientReminder, Deal, Lead, OwnerInquiry, PropertyOwnerLink, Showing


def sync_showing_follow_up(showing, form):
    """Keep a completed showing and its next CRM task in the same transaction."""
    task = showing.follow_up_task
    if showing.status != "completed":
        if task and not task.is_done and not task.cancelled_at:
            task.cancelled_at = timezone.now()
            task.save(update_fields=["cancelled_at"])
        if task:
            showing.follow_up_task = None
            showing.save(update_fields=["follow_up_task"])
        return

    action = (form.cleaned_data.get("next_action") or "").strip()
    due_at = form.cleaned_data.get("next_action_due_at")
    if not action or not due_at:
        return
    if task and not task.is_done and not task.cancelled_at:
        task.text = action
        task.due_at = due_at
        task.save(update_fields=["text", "due_at"])
    else:
        task = ClientReminder.objects.create(
            owner=showing.deal.responsible or showing.owner,
            agency=showing.agency,
            client=showing.deal.client,
            deal=showing.deal,
            text=action,
            due_at=due_at,
        )
        showing.follow_up_task = task
        showing.save(update_fields=["follow_up_task"])

class WorkItemsView(LoginRequiredMixin, TemplateView):
    template_name = "properties/work_items.html"
    page_size = 25
    default_period = "all"
    kinds = ("all", "task", "lead", "owner", "showing")
    periods = {"today": (0, 1), "overdue": (0,), "future": (2,), "unscheduled": (3,), "all": (0, 1, 2, 3)}

    def get_template_names(self):
        if self.request.headers.get("HX-Request") == "true":
            return ["properties/includes/work_items_workspace.html"]
        return [self.template_name]

    def render_to_response(self, context, **kwargs):
        response = super().render_to_response(context, **kwargs)
        response.headers["Vary"] = "HX-Request"
        return response

    @staticmethod
    def item_rows(queryset, *, kind, due_at, now, tomorrow):
        return queryset.order_by().annotate(
            item_kind=Value(kind, output_field=CharField()),
            item_due_at=due_at,
        ).annotate(sort_group=Case(
            When(item_due_at__isnull=True, then=Value(3)),
            When(item_due_at__lt=now, then=Value(0)),
            When(item_due_at__lt=tomorrow, then=Value(1)),
            default=Value(2), output_field=IntegerField(),
        )).values("item_kind", "pk", "item_due_at", "sort_group")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        tomorrow = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        selected_kind = self.request.GET.get("kind", "all")
        if selected_kind not in self.kinds:
            selected_kind = "all"

        selected_period = self.request.GET.get("period", self.default_period)
        if selected_period not in self.periods:
            selected_period = self.default_period
        query = self.request.GET.get("q", "").strip()[:200]
        counts = {index: 0 for index in range(4)}
        sources = []

        def add_source(queryset, kind, due_at):
            rows = self.item_rows(queryset, kind=kind, due_at=due_at, now=now, tomorrow=tomorrow)
            for group in rows.values("sort_group").annotate(total=Count("pk")).order_by():
                counts[group["sort_group"]] += group["total"]
            sources.append(rows.filter(sort_group__in=self.periods[selected_period]))

        if selected_kind in {"all", "task"}:
            tasks = ClientReminder.objects.filter(
                owner=self.request.user, is_done=False, cancelled_at__isnull=True, lead__isnull=True,
            )
            if query:
                tasks = tasks.filter(Q(text__icontains=query) | Q(client__name__icontains=query) | Q(client__phone__icontains=query)
                    | Q(property__title__icontains=query) | Q(property__address__icontains=query)
                    | Q(deal__property__title__icontains=query) | Q(deal__property__address__icontains=query))
            add_source(tasks, "task", F("due_at"))

        if selected_kind in {"all", "lead"}:
            leads = workspace_queryset(Lead.objects, self.request.user).filter(
                assigned_to=self.request.user, outcome="open",
            ).exclude(status="archived")
            first_response_due = ExpressionWrapper(F("created_at") + timedelta(minutes=30), output_field=DateTimeField())
            lead_due = Case(
                When(first_contacted_at__isnull=True, next_action_due_at__gte=first_response_due, then=first_response_due),
                default=F("next_action_due_at"), output_field=DateTimeField(),
            )
            if query:
                leads = leads.filter(Q(next_action__icontains=query) | Q(name__icontains=query) | Q(phone__icontains=query)
                    | Q(property__title__icontains=query) | Q(property__address__icontains=query))
            add_source(leads, "lead", lead_due)

        if selected_kind in {"all", "owner"}:
            inquiries = workspace_queryset(OwnerInquiry.objects, self.request.user).filter(
                assigned_to=self.request.user, property__isnull=True,
            ).exclude(result="declined").exclude(status="archived")
            inquiry_due = Case(
                When(first_contacted_at__isnull=True, next_action_due_at__gte=F("first_response_due_at"), then=F("first_response_due_at")),
                default=F("next_action_due_at"), output_field=DateTimeField(),
            )
            if query:
                inquiries = inquiries.filter(Q(next_action__icontains=query) | Q(client__name__icontains=query)
                    | Q(client__phone__icontains=query) | Q(address__icontains=query) | Q(description__icontains=query))
            add_source(inquiries, "owner", inquiry_due)

        if selected_kind in {"all", "showing"}:
            showings = Showing.objects.filter(owner=self.request.user, status="planned")
            if query:
                showings = showings.filter(Q(location__icontains=query) | Q(deal__client__name__icontains=query)
                    | Q(deal__client__phone__icontains=query) | Q(deal__property__title__icontains=query)
                    | Q(deal__property__address__icontains=query))
            add_source(showings, "showing", F("starts_at"))

        rows = sources[0].union(*sources[1:], all=True).order_by("sort_group", "item_due_at", "item_kind", "pk")
        page = Paginator(rows, self.page_size).get_page(self.request.GET.get("page"))
        visible = list(page.object_list)
        page.object_list = visible
        ids = {kind: [row["pk"] for row in visible if row["item_kind"] == kind] for kind in self.kinds[1:]}
        task_map = {
            task.pk: task for task in ClientReminder.objects.filter(pk__in=ids["task"], owner=self.request.user).select_related(
                "client", "deal__property", "property",
            )
        }
        lead_map = {
            lead.pk: lead for lead in workspace_queryset(
                Lead.objects.filter(pk__in=ids["lead"], assigned_to=self.request.user, outcome="open"),
                self.request.user,
            ).select_related("property", "client")
        }
        owner_map = {
            inquiry.pk: inquiry for inquiry in workspace_queryset(
                OwnerInquiry.objects.filter(pk__in=ids["owner"], assigned_to=self.request.user, property__isnull=True),
                self.request.user,
            ).select_related("client")
        }
        showing_map = {
            showing.pk: showing for showing in Showing.objects.filter(
                pk__in=ids["showing"], owner=self.request.user, status="planned",
            ).select_related("deal__client", "deal__property")
        }

        params = {"period": selected_period}
        if selected_kind != "all":
            params["kind"] = selected_kind
        if query:
            params["q"] = query

        def list_url(**changes):
            values = {**params, **changes}
            return self.request.path + "?" + urlencode({key: value for key, value in values.items() if value is not None})

        return_url = list_url(page=page.number)
        group_titles = ("Просрочено", "Сегодня", "Позже", "Без срока")
        groups = []
        for row in visible:
            kind, pk, due_at = row["item_kind"], row["pk"], row["item_due_at"]
            if kind == "task":
                task = task_map.get(pk)
                if not task:
                    continue
                details = task.client.name if task.client_id else "Общая задача"
                if task.deal_id:
                    details = f"{task.deal.property.title} · {details}"
                elif task.property_id:
                    details = f"{task.property.title} · {details}"
                item = {"kind": kind, "kind_label": "Задача", "pk": pk, "title": task.text,
                        "details": details, "due_at": due_at, "url": reverse("task_detail", args=[pk])}
            elif kind == "lead":
                lead = lead_map.get(pk)
                if not lead:
                    continue
                needs_first_response = not lead.first_contacted_at and lead.first_response_due_at <= lead.next_action_due_at
                item = {"kind": kind, "kind_label": "Заявка", "pk": pk,
                        "title": "Ответить клиенту" if needs_first_response else lead.next_action,
                        "details": f"{lead.name} · {lead.property.title if lead.property_id else 'Подбор недвижимости'}", "due_at": due_at,
                        "url": reverse("lead_detail", args=[pk])}
            elif kind == "owner":
                inquiry = owner_map.get(pk)
                if not inquiry:
                    continue
                item = {"kind": kind, "kind_label": "Собственник", "pk": pk,
                        "title": "Ответить собственнику" if not inquiry.first_contacted_at and inquiry.first_response_due_at <= inquiry.next_action_due_at else inquiry.next_action,
                        "details": f"{inquiry.client.name} · {inquiry.address or inquiry.description[:70]}", "due_at": due_at,
                        "url": reverse("owner_inquiry_detail", args=[pk])}
            else:
                showing = showing_map.get(pk)
                if not showing:
                    continue
                item = {"kind": kind, "kind_label": "Показ", "pk": pk,
                        "title": showing.deal.property.title, "details": showing.deal.client.name,
                        "due_at": due_at, "url": reverse("showing_detail", args=[pk])}
            item["key"] = f"{kind}:{pk}"
            item["is_overdue"] = row["sort_group"] == 0
            item["panel_url"] = list_url(page=page.number, item=item["key"])
            item["detail_url"] = item["url"]
            item["can_complete"] = False
            item["description"] = ""
            item["contact_url"] = ""
            item["property_url"] = ""
            item["deal_url"] = ""
            item["phone"] = ""
            if kind == "task":
                contact = task.client
                prop = task.deal.property if task.deal_id else task.property
                item["contact_name"] = contact.name if contact else "Общая задача"
                item["property_title"] = prop.title if prop else ""
                item["can_complete"] = not task.owner_showing_id
                item["description"] = task.outcome_note
                item["detail_url"] += "?" + urlencode({"next": return_url})
                item["action_label"] = "Записать итог" if task.owner_showing_id else "Изменить задачу"
                item["action_icon"] = "edit"
                if task.deal_id:
                    item["deal_url"] = reverse("deal_detail", args=[task.deal_id])
            elif kind == "lead":
                contact = lead.client
                prop = lead.property
                item["contact_name"] = lead.name
                item["phone"] = lead.phone
                item["property_title"] = prop.title if prop else "Подбор недвижимости"
                item["description"] = lead.message
                item["action_label"] = "Записать итог" if lead.first_contacted_at else "Записать контакт"
                item["action_icon"] = "edit"
                item["action_url"] = item["url"] + ("?complete=1#lead-action" if lead.first_contacted_at else "?contact=1#lead-edit")
            elif kind == "owner":
                contact, prop = inquiry.client, None
                item["contact_name"] = contact.name
                item["property_title"] = inquiry.address
                item["description"] = inquiry.description
                item["action_label"], item["action_icon"] = "Записать итог", "edit"
                item["action_url"] = item["url"] + "#owner-inquiry-process"
            else:
                contact, prop = showing.deal.client, showing.deal.property
                item["title"] = "Записать итог показа" if item["is_overdue"] else "Провести показ"
                item["contact_name"], item["property_title"] = contact.name, prop.title
                item["description"] = showing.location
                item["detail_url"] += "?" + urlencode({"next": return_url})
                item["action_label"], item["action_icon"] = "Открыть показ", "chevron-right"
                item["deal_url"] = reverse("deal_detail", args=[showing.deal_id])
            if contact:
                item["contact_url"] = reverse("client_detail", args=[contact.pk])
                item["phone"] = item["phone"] or contact.phone
            if prop:
                item["property_url"] = reverse("property_detail", args=[prop.pk])
            item.setdefault("action_url", item["detail_url"])
            index = row["sort_group"]
            if not groups or groups[-1]["index"] != index:
                groups.append({"index": index, "title": group_titles[index], "items": []})
            groups[-1]["items"].append(item)

        items = [item for group in groups for item in group["items"]]
        selected_item = next((item for item in items if item["key"] == self.request.GET.get("item")), None)
        next_item = None
        if selected_item:
            index = items.index(selected_item)
            if index + 1 < len(items):
                next_item = items[index + 1]
        period_labels = (("today", "Сегодня"), ("overdue", "Просрочено"), ("future", "Предстоящие"), ("unscheduled", "Без срока"), ("all", "Все открытые"))
        context.update({
            "work_items": items,
            "selected_item": selected_item,
            "next_work_item": next_item,
            "work_list_url": return_url,
            "work_reset_url": self.request.path,
            "work_query": query,
            "work_period": selected_period,
            "work_scope": urlencode({**params, "page": page.number}),
            "work_filter_count": int(selected_kind != "all"),
            "work_period_tabs": [{"value": value, "label": label, "url": list_url(period=value),
                "count": sum(counts[index] for index in self.periods[value])} for value, label in period_labels],
            "work_kind_choices": (("all", "Все дела"), ("task", "Задачи"), ("lead", "Заявки"), ("owner", "Собственники"), ("showing", "Показы")),
            "work_previous_url": list_url(page=page.previous_page_number()) if page.has_previous() else "",
            "work_next_url": list_url(page=page.next_page_number()) if page.has_next() else "",
            "selected_kind": selected_kind,
            "page_obj": page,
            "groups": groups,
        })
        return context


class WorkdayView(WorkItemsView):
    default_period = "today"
    template_name = "properties/workday.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        today = timezone.localdate(now)
        today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today_start + timedelta(days=1)
        tasks = ClientReminder.objects.filter(owner=self.request.user, is_done=False, cancelled_at__isnull=True, lead__isnull=True).select_related("client", "deal", "deal__property", "property")
        showings = Showing.objects.filter(owner=self.request.user).select_related("deal", "deal__client", "deal__property")
        leads = workspace_queryset(Lead.objects, self.request.user).filter(assigned_to=self.request.user, outcome="open").exclude(status="archived").select_related("property")
        owner_inquiries = workspace_queryset(OwnerInquiry.objects, self.request.user).filter(
            assigned_to=self.request.user, property__isnull=True,
        ).exclude(result="declined").exclude(status="archived").select_related("client")
        context.update({
            "today": today,
            "overdue_tasks": tasks.filter(due_at__lt=now).order_by("due_at"),
            "today_tasks": tasks.filter(due_at__gte=now, due_at__lt=tomorrow).order_by("due_at"),
            "unscheduled_tasks": tasks.filter(due_at__isnull=True).order_by("-created_at"),
            "today_showings": showings.filter(status="planned", starts_at__gte=now, starts_at__lt=tomorrow).order_by("starts_at"),
            "today_other_showings": showings.filter(status__in=("completed", "cancelled"), starts_at__date=today).order_by("starts_at"),
            "overdue_showings": showings.filter(status="planned", starts_at__lt=now).order_by("starts_at"),
            "overdue_leads": leads.filter(Lead.overdue_filter(now)).order_by("next_action_due_at"),
            "today_leads": leads.exclude(Lead.overdue_filter(now)).filter(
                Q(next_action_due_at__gte=now, next_action_due_at__lt=tomorrow)
                | Q(first_contacted_at__isnull=True, created_at__gte=today_start - timedelta(minutes=30), created_at__lt=tomorrow - timedelta(minutes=30))
            ).order_by("next_action_due_at"),
            "overdue_owner_inquiries": [item for item in owner_inquiries if item.attention_due_at < now],
            "today_owner_inquiries": [item for item in owner_inquiries if now <= item.attention_due_at < tomorrow],
        })
        return context


class WorkdayFormMixin(LoginRequiredMixin):
    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "owner": self.request.user}

    def get_return_url(self):
        next_url = self.request.POST.get("next", "") or self.request.GET.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(next_url, {self.request.get_host()}):
            return next_url
        return reverse("workday")

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "return_url": self.get_return_url()}

    def get_success_url(self):
        return self.get_return_url()

class TaskCreateView(WorkdayFormMixin, CreateView):
    model = ClientReminder
    form_class = TaskForm
    template_name = "properties/task_form.html"

    def get_initial(self):
        initial = super().get_initial()
        initial["due_at"] = timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        for field in ("client", "deal", "property"):
            value = self.request.GET.get(field, "")
            if value.isdigit():
                initial[field] = value
        return initial

    def form_valid(self, form):
        target = form.cleaned_data.get("deal") or form.cleaned_data.get("property") or form.cleaned_data.get("client")
        try:
            if target:
                require_resource_action(self.request.user, target, Operation.EDIT_CRM)
            else:
                require_new_action(self.request.user, Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        form.instance.owner = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, "Задача добавлена.")
        return response

class TaskUpdateView(WorkdayFormMixin, UpdateView):
    model = ClientReminder
    form_class = TaskForm
    template_name = "properties/task_form.html"
    context_object_name = "task"

    def get_queryset(self):
        membership = membership_for(self.request.user)
        visible = Q(owner=self.request.user)
        if membership and membership.role in {"owner", "manager"}:
            visible |= Q(agency=membership.agency)
        return ClientReminder.objects.filter(visible, lead__isnull=True).select_related("client", "deal", "property")

    def dispatch(self, request, *args, **kwargs):
        feedback_task = self.get_queryset().filter(pk=kwargs["pk"], owner_showing__isnull=False).values_list("owner_showing_id", flat=True).first()
        if feedback_task:
            from .owner_feedback_views import showing_feedback_url
            destination = showing_feedback_url(feedback_task)
            return_url = self.get_return_url()
            if return_url != reverse("workday"):
                destination = f"{reverse('showing_detail', args=[feedback_task])}?{urlencode({'next': return_url})}#owner-feedback"
            return redirect(destination)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        try:
            require_resource_action(self.request.user, self.object, Operation.EDIT_CRM)
            target = form.cleaned_data.get("deal") or form.cleaned_data.get("property") or form.cleaned_data.get("client")
            if target:
                require_resource_action(self.request.user, target, Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        response = super().form_valid(form)
        messages.success(self.request, "Задача сохранена.")
        return response

class TaskCompleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        task = get_object_or_404(ClientReminder, pk=pk, owner=request.user)
        if task.lead_id:
            messages.info(request, "Запишите итог и назначьте следующий шаг в карточке заявки.")
            return redirect(f"{reverse('lead_detail', kwargs={'pk': task.lead_id})}?complete=1#lead-action")
        if task.owner_showing_id:
            from .owner_feedback_views import showing_feedback_url
            messages.info(request, "Запишите, что сообщили собственнику, в карточке показа.")
            return redirect(showing_feedback_url(task.owner_showing_id))
        try:
            require_resource_action(request.user, task, Operation.EDIT_CRM)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("workday")
        if not task.is_done:
            task.is_done = True
            task.completed_at = timezone.now()
            task.save(update_fields=["is_done", "completed_at"])
        next_url = request.POST.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
            return redirect(next_url)
        return redirect("workday")

class ShowingCreateView(WorkdayFormMixin, CreateView):
    model = Showing
    form_class = ShowingForm
    template_name = "properties/showing_form.html"

    def get_initial(self):
        initial = super().get_initial()
        deal_id = self.request.GET.get("deal", "")
        if deal_id.isdigit():
            deal = get_object_or_404(workspace_queryset(Deal.objects, self.request.user), pk=deal_id)
            initial["deal"] = deal
            initial["location"] = deal.property.address
        return initial

    def form_valid(self, form):
        try:
            require_resource_action(self.request.user, form.cleaned_data["deal"], Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        form.instance.owner = self.request.user
        with transaction.atomic():
            response = super().form_valid(form)
            sync_showing_follow_up(self.object, form)
            ClientInteraction.objects.create(
                client=self.object.deal.client,
                lead=self.object.deal.lead,
                interaction_type="viewing",
                text=(
                    f"Показ объекта «{self.object.deal.property.title}»: "
                    f"{timezone.localtime(self.object.starts_at):%d.%m.%Y в %H:%M} · {self.object.get_status_display().lower()}."
                ),
            )
        messages.success(self.request, "Показ сохранён." if self.object.status != "planned" else "Показ запланирован.")
        if self.object.status == "completed" and self.object.deal.property.owner_links.exists():
            return redirect(f"{reverse('showing_detail', args=[self.object.pk])}#owner-feedback")
        return response

class ShowingUpdateView(WorkdayFormMixin, UpdateView):
    model = Showing
    form_class = ShowingForm
    template_name = "properties/showing_form.html"
    context_object_name = "showing"

    def get_queryset(self):
        membership = membership_for(self.request.user)
        visible = Q(owner=self.request.user)
        if membership and membership.role in {"owner", "manager"}:
            visible |= Q(agency=membership.agency)
        elif membership:
            visible |= Q(agency=membership.agency, owner_feedback_tasks__owner=self.request.user)
        return Showing.objects.filter(visible).distinct().select_related("deal", "deal__client", "deal__property")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        membership = membership_for(self.request.user)
        context["can_edit_showing"] = self.object.owner_id == self.request.user.pk or bool(
            membership and membership.role in {"owner", "manager"} and self.object.agency_id == membership.agency_id
        )
        context["can_plan_feedback"] = self.object.owner_id == self.request.user.pk
        if self.object.status == "completed" and Showing.objects.filter(pk=self.object.pk, status="completed").exists():
            context["show_owner_feedback"] = True
            links = list(PropertyOwnerLink.objects.filter(property=self.object.deal.property).select_related("client"))
            tasks = {
                task.client_id: task for task in self.object.owner_feedback_tasks.filter(client_id__in=[link.client_id for link in links])
            }
            for link in links:
                link.feedback_task = tasks.get(link.client_id)
                link.feedback_can_complete = bool(link.feedback_task and link.feedback_task.owner_id == self.request.user.pk)
            context["owner_feedback_links"] = links
            context["owner_feedback_pending"] = any(
                not link.feedback_task or link.feedback_task.cancelled_at or not link.feedback_task.is_done
                for link in links
            )
            context["owner_feedback_due"] = timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        return context

    def form_valid(self, form):
        membership = membership_for(self.request.user)
        if self.object.owner_id != self.request.user.pk and not (
                membership and membership.role in {"owner", "manager"} and self.object.agency_id == membership.agency_id):
            raise Http404
        try:
            require_resource_action(self.request.user, self.object, Operation.EDIT_CRM)
        except TariffDenied as error:
            form.add_error(None, error.message)
            return self.render_to_response(self.get_context_data(form=form), status=403)
        previous = Showing.objects.values("status", "starts_at").get(pk=self.object.pk)
        with transaction.atomic():
            locked = Showing.objects.select_for_update().get(pk=self.object.pk)
            form.instance.follow_up_task = locked.follow_up_task
            response = super().form_valid(form)
            sync_showing_follow_up(self.object, form)
            if self.object.status != "completed":
                for task in self.object.owner_feedback_tasks.filter(is_done=False, cancelled_at__isnull=True):
                    task.cancelled_at = timezone.now()
                    task.save(update_fields=["cancelled_at"])
            if previous["status"] != self.object.status or previous["starts_at"] != self.object.starts_at:
                detail = f" Итог: {self.object.outcome_note.strip()}" if self.object.status == "completed" else ""
                ClientInteraction.objects.create(
                    client=self.object.deal.client,
                    lead=self.object.deal.lead,
                    interaction_type="viewing",
                    text=(
                        f"Показ объекта «{self.object.deal.property.title}»: "
                        f"{timezone.localtime(self.object.starts_at):%d.%m.%Y в %H:%M} · "
                        f"{self.object.get_status_display().lower()}.{detail}"
                    ),
                )
        messages.success(self.request, "Показ сохранён.")
        if self.object.status == "completed" and self.object.deal.property.owner_links.exists():
            return redirect(f"{reverse('showing_detail', args=[self.object.pk])}#owner-feedback")
        return response
