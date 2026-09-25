from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import CreateView, TemplateView, UpdateView

from .forms import ShowingForm, TaskForm
from .agency_access import workspace_queryset
from .models import ClientInteraction, ClientReminder, Deal, Showing


class WorkdayView(LoginRequiredMixin, TemplateView):
    template_name = "properties/workday.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = timezone.now()
        today = timezone.localdate(now)
        today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today_start + timedelta(days=1)
        tasks = ClientReminder.objects.filter(owner=self.request.user, is_done=False).select_related("client", "deal", "deal__property")
        showings = Showing.objects.filter(owner=self.request.user).select_related("deal", "deal__client", "deal__property")
        context.update({
            "today": today,
            "overdue_tasks": tasks.filter(due_at__lt=now).order_by("due_at"),
            "today_tasks": tasks.filter(due_at__gte=now, due_at__lt=tomorrow).order_by("due_at"),
            "unscheduled_tasks": tasks.filter(due_at__isnull=True).order_by("-created_at"),
            "today_showings": showings.filter(starts_at__date=today).order_by("starts_at"),
            "overdue_showings": showings.filter(status="planned", starts_at__lt=today_start).order_by("starts_at"),
        })
        return context


class WorkdayFormMixin(LoginRequiredMixin):
    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "owner": self.request.user}

    def get_success_url(self):
        return reverse("workday")


class TaskCreateView(WorkdayFormMixin, CreateView):
    model = ClientReminder
    form_class = TaskForm
    template_name = "properties/task_form.html"

    def get_initial(self):
        initial = super().get_initial()
        initial["due_at"] = timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        for field in ("client", "deal"):
            value = self.request.GET.get(field, "")
            if value.isdigit():
                initial[field] = value
        return initial

    def form_valid(self, form):
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
        return ClientReminder.objects.filter(owner=self.request.user).select_related("client", "deal")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Задача сохранена.")
        return response


class TaskCompleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        task = get_object_or_404(ClientReminder, pk=pk, owner=request.user)
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
        form.instance.owner = self.request.user
        with transaction.atomic():
            response = super().form_valid(form)
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
        return response


class ShowingUpdateView(WorkdayFormMixin, UpdateView):
    model = Showing
    form_class = ShowingForm
    template_name = "properties/showing_form.html"
    context_object_name = "showing"

    def get_queryset(self):
        return Showing.objects.filter(owner=self.request.user).select_related("deal", "deal__client", "deal__property")

    def form_valid(self, form):
        previous = Showing.objects.values("status", "starts_at").get(pk=self.object.pk)
        with transaction.atomic():
            response = super().form_valid(form)
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
        return response
