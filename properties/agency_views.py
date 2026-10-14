import csv
import json
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
import uuid
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core import signing
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from billing.models import (
    BillingAccount, BillingPeriod, FileReference, ResourceReservation,
    StoredFile, UsageEntry, UsageOperation,
)
from billing.enforcement import TariffDenied, enabled as billing_enforcement_enabled, require_allowed
from billing.rules import PolicyService
from billing.reservations import DuplicateResourceRequest, request_identity, reserved_resources
from billing.rules import Operation
from billing.services import AccountService
from billing.usage import attempt

from .agency_access import membership_for, may_manage_agency, workspace_queryset
from .models import (
    Agency, AgencyInvitation, AgencyMembership, AuditEvent, Client, ClientInteraction,
    ClientPropertySuggestion, ClientReminder, CRMChangeEvent, Deal, LandingDailyView,
    LandingStylePreset, Lead, OwnerInquiry, Property, PropertyImage, PropertyOwnerLink,
    Showing, TelegramDelivery, TelegramPublication,
    TelegramSubscriber,
)
from .services.crm_analytics_service import AnalyticsPeriod, build_report, resolve_scope, team_member_rows
from .services.agency_closure_service import close_agency, closure_snapshot


class AgencyCreateForm(forms.Form):
    name = forms.CharField(label="Название агентства", max_length=150)


class InvitationForm(forms.Form):
    role = forms.ChoiceField(label="Роль сотрудника", choices=AgencyInvitation._meta.get_field("role").choices)


def team_history_events(queryset):
    """Give short, readable labels to the already recorded team events."""
    names = {"agency membership": "Сотрудник", "agency invitation": "Приглашение"}
    for event in queryset:
        subject = names.get(event.model_name.lower(), event.model_name)
        event.summary = f"{event.get_action_display()} · {subject}"
        event.display_fields = event.changed_fields
        if subject == "Приглашение" and event.action == "updated":
            if "revoked at" in event.changed_fields:
                event.summary = "Приглашение отозвано"
                event.display_fields = []
            elif "accepted by" in event.changed_fields:
                event.summary = "Приглашение принято"
                event.display_fields = []
        elif subject == "Сотрудник" and event.action == "updated" and "role" in event.changed_fields:
            event.summary = "Роль сотрудника изменена"
            event.display_fields = []
        elif subject == "Команда" and event.action == "updated" and "Название" in event.changed_fields:
            event.summary = "Команда переименована"
            event.display_fields = []
        elif subject == "Команда" and event.action == "updated" and "Закрыта" in event.changed_fields:
            event.summary = "Команда закрыта"
            event.display_fields = []
    return queryset


class AgencyDashboardView(LoginRequiredMixin, View):
    def get(self, request):
        membership = membership_for(request.user)
        if not membership:
            closed_agencies = Agency.objects.filter(owner=request.user, closed_at__isnull=False).order_by("-closed_at")
            return render(request, "properties/agency_dashboard.html", {
                "create_form": AgencyCreateForm(), "closed_agencies": closed_agencies,
            })
        agency = membership.agency
        active_tab = "journal" if request.GET.get("tab") == "journal" else "team"
        can_manage = membership.role in {"owner", "manager"}
        members = list(agency.memberships.select_related("user", "user__realtor_profile").order_by("joined_at"))
        work_overview = None
        work_members = []
        if can_manage:
            period = AnalyticsPeriod.ending_now()
            scope = resolve_scope(request.user, requested_scope="team", membership=membership)
            current = build_report(scope, period)["now"]
            rows = team_member_rows(scope, period)
            unassigned = next((row["open_leads"] for row in rows if row["user_id"] is None), 0)

            def work_url(state, *, member=None):
                params = {"state": state}
                if member is not None:
                    params["member"] = member
                else:
                    params["kind"] = "lead"
                return f"{reverse('agency_workload')}?{urlencode(params)}"

            work_overview = (
                {"label": "Без первого ответа", "mobile_label": "Без ответа", "count": current["unanswered"], "detail": "открытых заявок", "url": work_url("unanswered")},
                {"label": "Просрочено", "mobile_label": "Просрочено", "count": current["overdue_leads"], "detail": "заявок", "url": work_url("overdue"), "urgent": True},
                {"label": "Без ответственного", "mobile_label": "Не назначено", "count": unassigned, "detail": "открытых заявок", "url": work_url("unassigned")},
            )
            member_by_user = {member.user_id: member for member in members}
            for row in rows:
                if row["user_id"] is None:
                    continue
                row["membership"] = member_by_user[row["user_id"]]
                row["url"] = work_url("overdue" if row["urgent_work"] else "all", member=row["user_id"])
                row["next_due_overdue"] = bool(row["next_due_at"] and row["next_due_at"] < period.now)
                work_members.append(row)
        now = timezone.now()
        invitations = []
        pending_count = 0
        if can_manage:
            pending = list(agency.invitations.filter(
                accepted_by__isnull=True, revoked_at__isnull=True, expires_at__gt=now,
            ).select_related("created_by").order_by("expires_at", "pk"))
            pending_count = len(pending)
            previous = list(agency.invitations.exclude(pk__in=[invite.pk for invite in pending])
                            .select_related("created_by").order_by("-created_at", "-pk")[:3])
            invitations = pending + previous
            for invite in invitations:
                invite.display_status = ("Принято" if invite.accepted_by_id else "Отозвано" if invite.revoked_at
                                         else "Истекло" if invite.expires_at <= now else "Ожидает")
                invite.can_revoke = invite.display_status == "Ожидает" and (membership.role == "owner" or invite.created_by_id == request.user.pk)
                invite.copy_url = request.build_absolute_uri(reverse("agency_join", args=[invite.code])) if invite.display_status == "Ожидает" else ""
        audit_page = None
        if active_tab == "journal":
            events = agency.audit_events.select_related("actor").order_by("-created_at", "-pk")
            audit_page = Paginator(events, 20).get_page(request.GET.get("page"))
            team_history_events(audit_page.object_list)
        return render(request, "properties/agency_dashboard.html", {
            "active_tab": active_tab,
            "membership": membership,
            "agency": agency,
            "members": members,
            "invitations": invitations,
            "occupied_seats": len(members),
            "pending_seats": pending_count,
            "invitation_form": InvitationForm(),
            "audit_page": audit_page,
            "can_manage": can_manage,
            "is_owner": membership.role == "owner",
            "work_overview": work_overview,
            "work_members": work_members,
            "resource_request_key": str(uuid.uuid4()),
        })


class AgencyHistoryView(LoginRequiredMixin, View):
    def get(self, request):
        archive_id = request.GET.get("archive")
        if archive_id:
            agency = get_object_or_404(Agency, pk=archive_id, owner=request.user, closed_at__isnull=False)
            back_url = reverse("agency_archive", args=[agency.pk])
        else:
            membership = membership_for(request.user)
            if not membership:
                raise Http404
            agency = membership.agency
            back_url = f"{reverse('agency_dashboard')}?tab=journal"
        events = agency.audit_events.select_related("actor").order_by("-created_at", "-pk")
        page = Paginator(events, 25).get_page(request.GET.get("page"))
        team_history_events(page.object_list)
        return render(request, "properties/agency_history.html", {
            "agency": agency, "page": page, "back_url": back_url,
            "archive_id": agency.pk if archive_id else None,
        })


class AgencyCloseView(LoginRequiredMixin, View):
    template_name = "properties/agency_close.html"
    token_salt = "agency-closure-preview"

    def _agency(self, request):
        membership = membership_for(request.user)
        if not membership or membership.role != "owner":
            raise Http404
        return membership.agency

    def _preview(self, request, agency, *, error="", status=200):
        snapshot = closure_snapshot(agency)
        token = signing.dumps({"agency": agency.pk, "snapshot": snapshot}, salt=self.token_salt)
        return render(request, self.template_name, {
            "agency": agency, "snapshot": snapshot, "preview_token": token, "error": error,
        }, status=status)

    def get(self, request):
        return self._preview(request, self._agency(request))

    def post(self, request):
        agency = self._agency(request)
        try:
            signed = signing.loads(request.POST.get("preview_token", ""), salt=self.token_salt, max_age=900)
        except signing.BadSignature:
            return self._preview(request, agency, error="Предпросмотр устарел. Проверьте состав команды ещё раз.", status=400)
        if signed.get("agency") != agency.pk or request.POST.get("confirm_name", "").strip() != agency.name:
            return self._preview(request, agency, error="Введите точное название команды для подтверждения.", status=400)
        with transaction.atomic():
            locked = get_object_or_404(Agency.objects.select_for_update(), pk=agency.pk, owner=request.user, closed_at__isnull=True)
            current = closure_snapshot(locked)
            if current != signed.get("snapshot"):
                return self._preview(request, locked, error="Данные команды изменились. Проверьте последствия и подтвердите ещё раз.", status=409)
            close_agency(locked, request.user, current)
        messages.success(request, "Команда закрыта. Общая база и тарифная история сохранены в архиве.")
        return redirect("agency_archive", pk=agency.pk)


ARCHIVE_EXPORTS = {
    "properties": (Property, lambda agency: Property.objects.filter(agency=agency)),
    "clients": (Client, lambda agency: Client.objects.filter(agency=agency)),
    "leads": (Lead, lambda agency: Lead.objects.filter(agency=agency)),
    "deals": (Deal, lambda agency: Deal.objects.filter(agency=agency)),
    "owner_inquiries": (OwnerInquiry, lambda agency: OwnerInquiry.objects.filter(agency=agency)),
    "tasks": (ClientReminder, lambda agency: ClientReminder.objects.filter(agency=agency)),
    "showings": (Showing, lambda agency: Showing.objects.filter(agency=agency)),
    "audit": (AuditEvent, lambda agency: AuditEvent.objects.filter(agency=agency)),
    "files": (StoredFile, lambda agency: StoredFile.objects.filter(account__agency=agency)),
    "interactions": (ClientInteraction, lambda agency: ClientInteraction.objects.filter(client__agency=agency)),
    "suggestions": (ClientPropertySuggestion, lambda agency: ClientPropertySuggestion.objects.filter(client__agency=agency)),
    "owner_links": (PropertyOwnerLink, lambda agency: PropertyOwnerLink.objects.filter(property__agency=agency)),
    "images": (PropertyImage, lambda agency: PropertyImage.objects.filter(property__agency=agency)),
    "invitations": (AgencyInvitation, lambda agency: AgencyInvitation.objects.filter(agency=agency)),
    "telegram_subscribers": (TelegramSubscriber, lambda agency: TelegramSubscriber.objects.filter(feed__agency=agency)),
    "telegram_publications": (TelegramPublication, lambda agency: TelegramPublication.objects.filter(feed__agency=agency)),
    "telegram_deliveries": (TelegramDelivery, lambda agency: TelegramDelivery.objects.filter(publication__feed__agency=agency)),
    "billing_account": (BillingAccount, lambda agency: BillingAccount.objects.filter(agency=agency)),
    "billing_periods": (BillingPeriod, lambda agency: BillingPeriod.objects.filter(subscription__account__agency=agency)),
    "billing_operations": (UsageOperation, lambda agency: UsageOperation.objects.filter(account__agency=agency)),
    "billing_entries": (UsageEntry, lambda agency: UsageEntry.objects.filter(account__agency=agency)),
    "billing_reservations": (ResourceReservation, lambda agency: ResourceReservation.objects.filter(account__agency=agency)),
    "file_references": (FileReference, lambda agency: FileReference.objects.filter(file__account__agency=agency)),
    "crm_changes": (CRMChangeEvent, lambda agency: CRMChangeEvent.objects.filter(agency_id=agency.pk)),
    "landing_views": (LandingDailyView, lambda agency: LandingDailyView.objects.filter(property__agency=agency)),
    "landing_presets": (LandingStylePreset, lambda agency: LandingStylePreset.objects.filter(agency=agency)),
}

ARCHIVE_MAIN_KINDS = ("properties", "clients", "leads", "deals", "owner_inquiries", "tasks", "showings", "audit", "files")
ARCHIVE_MAIN_LABELS = {
    "properties": "Объекты", "clients": "Клиенты и собственники", "leads": "Заявки",
    "deals": "Сделки", "owner_inquiries": "Обращения собственников", "tasks": "Задачи",
    "showings": "Показы", "audit": "История изменений", "files": "Файлы",
}
ARCHIVE_EXTRA_LABELS = {
    "interactions": "История контактов", "suggestions": "Подбор объектов", "owner_links": "Связи с собственниками",
    "images": "Фотографии", "invitations": "Приглашения", "telegram_subscribers": "Telegram-подписки",
    "telegram_publications": "Telegram-публикации", "telegram_deliveries": "Доставки Telegram",
    "billing_account": "Тарифный счёт", "billing_periods": "Расчётные периоды",
    "billing_operations": "Операции расхода", "billing_entries": "Записи расхода",
    "billing_reservations": "Резервы", "file_references": "Привязки файлов",
    "crm_changes": "Изменения CRM", "landing_views": "Просмотры лендингов",
    "landing_presets": "Оформление лендингов",
}


def _archive_agency(request, pk):
    return get_object_or_404(Agency, pk=pk, owner=request.user, closed_at__isnull=False)


class AgencyArchiveView(LoginRequiredMixin, View):
    def get(self, request, pk):
        agency = _archive_agency(request, pk)
        sections = []
        for kind in ARCHIVE_MAIN_KINDS:
            model, queryset = ARCHIVE_EXPORTS[kind]
            qs = queryset(agency)
            sections.append({"kind": kind, "label": ARCHIVE_MAIN_LABELS[kind], "count": qs.count(), "sample": list(qs.order_by("pk")[:3])})
        extras = [{"kind": kind, "label": label, "count": ARCHIVE_EXPORTS[kind][1](agency).count()}
                  for kind, label in ARCHIVE_EXTRA_LABELS.items()]
        return render(request, "properties/agency_archive.html", {
            "agency": agency, "snapshot": agency.closure_snapshot, "sections": sections, "extras": extras,
        })


class AgencyArchiveRecordsView(LoginRequiredMixin, View):
    def get(self, request, pk, kind):
        agency = _archive_agency(request, pk)
        if kind not in ARCHIVE_MAIN_KINDS or kind in {"audit", "files"}:
            raise Http404
        model, queryset = ARCHIVE_EXPORTS[kind]
        page = Paginator(queryset(agency).order_by("pk"), 25).get_page(request.GET.get("page"))
        fields = list(model._meta.concrete_fields)
        rows = []
        for record in page:
            values = []
            for field in fields:
                value = getattr(record, field.attname)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                elif isinstance(value, (datetime, date)):
                    if isinstance(value, datetime) and timezone.is_aware(value):
                        value = timezone.localtime(value)
                    value = value.strftime("%d.%m.%Y %H:%M") if isinstance(value, datetime) else value.strftime("%d.%m.%Y")
                elif isinstance(value, bool):
                    value = "Да" if value else "Нет"
                elif value is None or value == "":
                    value = "—"
                values.append((str(field.verbose_name), value))
            rows.append({"title": str(record), "fields": values})
        return render(request, "properties/agency_archive_records.html", {
            "agency": agency, "kind": kind, "label": ARCHIVE_MAIN_LABELS[kind],
            "rows": rows, "page": page,
        })


def _csv_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, (datetime, date)):
        value = value.isoformat()
    elif value is None:
        value = ""
    else:
        value = str(value)
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


class AgencyArchiveExportView(LoginRequiredMixin, View):
    def get(self, request, pk, kind):
        agency = _archive_agency(request, pk)
        spec = ARCHIVE_EXPORTS.get(kind)
        if not spec:
            raise Http404
        model, queryset = spec
        fields = list(model._meta.concrete_fields)
        names = [field.attname for field in fields]

        class Echo:
            def write(self, value):
                return value

        writer = csv.writer(Echo())

        def rows():
            yield "\ufeff"
            yield writer.writerow([str(field.verbose_name) for field in fields])
            for values in queryset(agency).order_by("pk").values_list(*names).iterator(chunk_size=500):
                yield writer.writerow([_csv_cell(value) for value in values])

        response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="reaicrm-team-{agency.pk}-{kind}.csv"'
        return response


class AgencyArchiveFileView(LoginRequiredMixin, View):
    def get(self, request, pk, file_pk):
        agency = _archive_agency(request, pk)
        stored = get_object_or_404(StoredFile, pk=file_pk, account__agency=agency)
        try:
            source = default_storage.open(stored.name, "rb")
        except (OSError, ValueError):
            raise Http404 from None
        return FileResponse(source, as_attachment=True, filename=PurePosixPath(stored.name).name)


class AgencyArchiveFilesView(LoginRequiredMixin, View):
    def get(self, request, pk):
        agency = _archive_agency(request, pk)
        files = StoredFile.objects.filter(account__agency=agency).order_by("pk")
        page = Paginator(files, 30).get_page(request.GET.get("page"))
        return render(request, "properties/agency_archive_files.html", {"agency": agency, "page": page})


class AgencyRenameView(LoginRequiredMixin, View):
    def post(self, request):
        membership = membership_for(request.user)
        if not membership or membership.role != "owner":
            raise Http404
        form = AgencyCreateForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Укажите название команды до 150 символов.")
            return redirect("agency_settings")
        name = form.cleaned_data["name"].strip()
        account = AccountService.ensure_agency(membership.agency)
        try:
            require_allowed(PolicyService.evaluate(account, request.user, Operation.EDIT_CRM))
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("agency_settings")
        if membership.agency.name != name:
            membership.agency.name = name
            membership.agency.save(update_fields=["name"])
            AuditEvent.objects.create(
                agency=membership.agency, actor=request.user, model_name="Команда",
                object_pk=str(membership.agency_id), action="updated", changed_fields=["Название"],
            )
            messages.success(request, "Название команды обновлено.")
        return redirect("agency_settings")


class AgencyCreateView(LoginRequiredMixin, View):
    def post(self, request):
        if membership_for(request.user):
            return redirect("agency_dashboard")
        form = AgencyCreateForm(request.POST)
        if not form.is_valid():
            return render(request, "properties/agency_dashboard.html", {"create_form": form}, status=400)
        try:
            with transaction.atomic():
                personal = AccountService.ensure_personal(request.user)
                BillingAccount.objects.select_for_update().get(pk=personal.pk)
                require_allowed(PolicyService.evaluate(personal, request.user, Operation.CREATE_AGENCY))
                agency = Agency.objects.create(owner=request.user, name=form.cleaned_data["name"])
                if billing_enforcement_enabled():
                    AccountService.convert_founder_account(agency, request.user)
                else:
                    attempt(lambda: AccountService.convert_founder_account(agency, request.user))
                AgencyMembership.objects.get_or_create(agency=agency, user=request.user, defaults={"role": "owner"})
                # The founder explicitly converts their existing personal database into shared agency data.
                # Move referenced records first so validation of owner inquiries
                # and showing feedback tasks sees one consistent team scope.
                for model in (Property, Client, Lead, Deal, Showing, ClientReminder, OwnerInquiry):
                    for record in model.objects.filter(owner=request.user, agency__isnull=True).iterator():
                        record.agency = agency
                        record.save(update_fields=["agency"])
        except TariffDenied as error:
            form.add_error(None, error.message)
            return render(request, "properties/agency_dashboard.html", {"create_form": form}, status=403)
        messages.success(request, "Команда создана. Ваши объекты, контакты, обращения собственников и сделки стали общей базой команды.")
        return redirect("agency_dashboard")


class AgencyInviteView(LoginRequiredMixin, View):
    def post(self, request):
        membership = membership_for(request.user)
        if not membership or membership.role not in {"owner", "manager"}:
            raise Http404
        form = InvitationForm(request.POST)
        if form.is_valid() and (membership.role == "owner" or form.cleaned_data["role"] == "agent"):
            account = AccountService.ensure_agency(membership.agency)
            try:
                with reserved_resources(account=account, actor=request.user, action=Operation.INVITE_MEMBER,
                        amounts={"invited_seats": 1}, invited_role=form.cleaned_data["role"],
                        **request_identity(request)) as reservation:
                    invite = AgencyInvitation.objects.create(
                        agency=membership.agency, role=form.cleaned_data["role"], created_by=request.user,
                        expires_at=timezone.now() + timedelta(days=7),
                    )
                    reservation.result_id = str(invite.pk)
            except DuplicateResourceRequest as duplicate:
                invite = AgencyInvitation.objects.filter(pk=duplicate.reservation.result_id, agency=membership.agency).first()
                if invite is None:
                    messages.info(request, "Этот запрос уже обрабатывается или приглашение отозвано.")
                    return redirect("agency_dashboard")
            except ValidationError:
                messages.error(request, "Ключ запроса уже использован для другого приглашения. Обновите страницу и повторите действие.")
                return redirect("agency_dashboard")
            except TariffDenied as error:
                messages.error(request, error.message)
                return redirect("agency_dashboard")
            messages.success(request, "Приглашение создано. Скопируйте ссылку в разделе «Приглашения».")
        else:
            messages.error(request, "Выберите роль сотрудника.")
        return redirect("agency_dashboard")


class AgencyJoinView(LoginRequiredMixin, View):
    def get(self, request, code):
        invite = get_object_or_404(AgencyInvitation.objects.select_related("agency"), code=code, agency__closed_at__isnull=True, accepted_by__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())
        if membership_for(request.user):
            messages.error(request, "Вы уже состоите в агентстве.")
            return redirect("agency_dashboard")
        return render(request, "properties/agency_join.html", {"invite": invite})

    def post(self, request, code):
        visible = get_object_or_404(AgencyInvitation.objects.select_related("agency"), code=code,
            agency__closed_at__isnull=True, accepted_by__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())
        account = AccountService.ensure_agency(visible.agency)
        try:
            with transaction.atomic():
                if Agency.objects.select_for_update().get(pk=visible.agency_id).closed_at:
                    raise Http404
                BillingAccount.objects.select_for_update().get(pk=account.pk)
                invite = get_object_or_404(AgencyInvitation.objects.select_for_update().select_related("agency"), code=code, agency__closed_at__isnull=True, accepted_by__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())
                if membership_for(request.user):
                    messages.error(request, "Вы уже состоите в агентстве.")
                    return redirect("agency_dashboard")
                require_allowed(PolicyService.evaluate(account, request.user, Operation.ACCEPT_INVITE, resource=invite))
                AgencyMembership.objects.create(agency=invite.agency, user=request.user, role=invite.role)
                invite.accepted_by = request.user
                invite.save(update_fields=["accepted_by"])
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("agency_join", code=code)
        messages.success(request, f"Вы присоединились к агентству «{invite.agency.name}».")
        return redirect("agency_dashboard")


class AgencyInviteRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        membership = membership_for(request.user)
        if not membership or membership.role not in {"owner", "manager"}:
            raise Http404
        try:
            with transaction.atomic():
                invite = get_object_or_404(AgencyInvitation.objects.select_for_update(), pk=pk, agency=membership.agency,
                                           accepted_by__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())
                if membership.role == "manager" and invite.created_by_id != request.user.pk:
                    raise Http404
                account = AccountService.ensure_agency(membership.agency)
                require_allowed(PolicyService.evaluate(account, request.user, Operation.EDIT_CRM))
                invite.revoked_at = timezone.now()
                invite.save(update_fields=["revoked_at"])
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("agency_dashboard")
        messages.success(request, "Приглашение отозвано.")
        return redirect("agency_dashboard")


class AgencyMemberRoleView(LoginRequiredMixin, View):
    def post(self, request, pk):
        membership = membership_for(request.user)
        if not membership or membership.role != "owner":
            raise Http404
        target = get_object_or_404(AgencyMembership, pk=pk, agency=membership.agency)
        role = request.POST.get("role")
        if target.role == "owner" or role not in {"manager", "agent"}:
            raise Http404
        try:
            account = AccountService.ensure_agency(membership.agency)
            require_allowed(PolicyService.evaluate(account, request.user, Operation.CHANGE_MEMBER_ROLE,
                resource=target, invited_role=role))
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("agency_dashboard")
        target.role = role
        target.save(update_fields=["role"])
        messages.success(request, "Роль сотрудника обновлена.")
        return redirect("agency_dashboard")


def member_transfer_snapshot(agency, user):
    """Count every record that will change owner, plus active work for the preview."""
    leads = Lead.objects.filter(agency=agency, assigned_to=user)
    tasks = ClientReminder.objects.filter(agency=agency, owner=user)
    deals = Deal.objects.filter(agency=agency)
    inquiries = OwnerInquiry.objects.filter(agency=agency)
    showings = Showing.objects.filter(agency=agency, owner=user)
    return {
        "active_leads": leads.filter(outcome="open").exclude(status="archived").count(),
        "open_tasks": tasks.filter(is_done=False, cancelled_at__isnull=True).count(),
        "active_deals": deals.filter(responsible=user, stage__in=Deal.ACTIVE_STAGES).count(),
        "open_inquiries": inquiries.filter(assigned_to=user, property__isnull=True).exclude(result="declined").count(),
        "planned_showings": showings.filter(status="planned").count(),
        "unassigned_team_leads": Lead.objects.filter(
            agency=agency, assigned_to__isnull=True, outcome="open",
        ).exclude(status="archived").count(),
        "all_leads": Lead.objects.filter(agency=agency).filter(Q(owner=user) | Q(assigned_to=user)).count(),
        "all_tasks": tasks.count(),
        "all_properties": Property.objects.filter(agency=agency, owner=user).count(),
        "all_clients": Client.objects.filter(agency=agency, owner=user).count(),
        "all_deals_owned": deals.filter(owner=user).count(),
        "all_deals_responsible": deals.filter(responsible=user).count(),
        "all_showings": showings.count(),
        "all_inquiries_owned": inquiries.filter(owner=user).count(),
        "all_inquiries_assigned": inquiries.filter(assigned_to=user).count(),
    }


class AgencyMemberRemoveView(LoginRequiredMixin, View):
    template_name = "properties/agency_member_remove.html"
    token_salt = "agency-member-transfer-preview"

    def target_for(self, request, pk):
        membership = membership_for(request.user)
        if not membership or membership.role != "owner":
            raise Http404
        target = get_object_or_404(AgencyMembership.objects.select_related("user", "user__realtor_profile"),
                                   pk=pk, agency=membership.agency)
        if target.role == "owner":
            raise Http404
        return membership, target

    def preview(self, request, membership, target, *, error="", status=200, selected_replacement=""):
        counts = member_transfer_snapshot(membership.agency, target.user)
        token = signing.dumps({"agency": membership.agency_id, "member": target.pk, "counts": counts}, salt=self.token_salt)
        replacements = (AgencyMembership.objects.filter(agency=membership.agency)
                        .exclude(pk=target.pk).select_related("user", "user__realtor_profile").order_by("user_id"))
        return render(request, self.template_name, {
            "target": target, "counts": counts, "preview_token": token,
            "replacements": replacements, "error": error, "selected_replacement": str(selected_replacement),
        }, status=status)

    def get(self, request, pk):
        membership, target = self.target_for(request, pk)
        return self.preview(request, membership, target)

    def post(self, request, pk):
        membership, target = self.target_for(request, pk)
        replacement_id = request.POST.get("replacement", "")
        if request.POST.get("confirm") != "transfer_and_remove" or not replacement_id.isdecimal():
            return self.preview(request, membership, target, error="Выберите получателя работы.", status=400)
        try:
            preview = signing.loads(request.POST.get("preview_token", ""), salt=self.token_salt, max_age=3600)
        except signing.BadSignature:
            return self.preview(request, membership, target, error="Предварительный просмотр устарел. Проверьте нагрузку ещё раз.", status=400)
        if preview.get("agency") != membership.agency_id or preview.get("member") != target.pk:
            raise Http404
        account = AccountService.ensure_agency(membership.agency)
        try:
            with transaction.atomic():
                BillingAccount.objects.select_for_update().get(pk=account.pk)
                target = get_object_or_404(AgencyMembership.objects.select_for_update(), pk=target.pk, agency=membership.agency)
                replacement_member = get_object_or_404(AgencyMembership.objects.select_for_update().select_related("user"),
                    agency=membership.agency, user_id=int(replacement_id))
                if replacement_member.pk == target.pk:
                    return self.preview(request, membership, target, error="Выберите другого сотрудника.", status=400)
                current = member_transfer_snapshot(membership.agency, target.user)
                if current != preview.get("counts"):
                    return self.preview(request, membership, target, error="Нагрузка изменилась. Проверьте обновлённые числа и подтвердите ещё раз.", status=409, selected_replacement=replacement_id)
                require_allowed(PolicyService.evaluate(account, request.user, Operation.REMOVE_MEMBER, resource=target))
                recipient = replacement_member.user
                for model in (Property, Client, Lead, Deal, Showing, OwnerInquiry):
                    for record in model.objects.select_for_update().filter(agency=membership.agency, owner=target.user).iterator():
                        record.owner = recipient
                        record.save(update_fields=["owner"])
                for lead in Lead.objects.select_for_update().filter(agency=membership.agency, assigned_to=target.user).iterator():
                    lead.assigned_to = recipient
                    lead.save(update_fields=["assigned_to", "updated_at"])
                for lead in Lead.objects.select_for_update().filter(
                    agency=membership.agency, assigned_to__isnull=True, outcome="open",
                ).exclude(status="archived").iterator():
                    lead.assigned_to = recipient
                    lead.save(update_fields=["assigned_to", "updated_at"])
                for inquiry in OwnerInquiry.objects.select_for_update().filter(agency=membership.agency, assigned_to=target.user).iterator():
                    inquiry.assigned_to = recipient
                    inquiry.save(update_fields=["assigned_to", "updated_at"])
                for deal in Deal.objects.select_for_update().filter(agency=membership.agency, responsible=target.user).iterator():
                    deal.responsible = recipient
                    deal.save(update_fields=["responsible"])
                for task in ClientReminder.objects.select_for_update(of=("self",)).select_related("lead").filter(
                    agency=membership.agency, owner=target.user).iterator():
                    task.owner = (
                        task.lead.assigned_to if task.lead_id and task.lead.assigned_to_id
                        and not task.is_done and not task.cancelled_at else recipient
                    )
                    task.save(update_fields=["owner"])
                target.delete()
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("agency_dashboard")
        except ValidationError:
            return self.preview(request, membership, target, error="Передача не выполнена: проверьте связанные записи.", status=409)
        messages.success(request, "Сотрудник исключён. Его командная работа передана выбранному сотруднику.")
        return redirect("agency_dashboard")


class LeadAssignView(LoginRequiredMixin, View):
    def post(self, request, pk):
        from .views import is_lead_panel_request, lead_panel_feedback
        membership = membership_for(request.user)
        if not membership or membership.role not in {"owner", "manager"}:
            raise Http404
        lead = get_object_or_404(
            workspace_queryset(Lead.objects, request.user)
            .select_related("property", "client", "assigned_to__realtor_profile"),
            pk=pk, agency=membership.agency,
        )
        assignee_id = request.POST.get("assigned_to", "")
        if not assignee_id.isdigit():
            if is_lead_panel_request(request):
                return lead_panel_feedback(request, lead, "Выберите сотрудника команды.", level="error")
            raise Http404
        assignee = AgencyMembership.objects.filter(agency=membership.agency, user_id=assignee_id).first()
        if not assignee:
            if is_lead_panel_request(request):
                return lead_panel_feedback(request, lead, "Выберите сотрудника команды.", level="error")
            raise Http404
        try:
            account = AccountService.ensure_agency(membership.agency)
            require_allowed(PolicyService.evaluate(account, request.user, Operation.EDIT_CRM, resource=lead))
        except TariffDenied as error:
            if is_lead_panel_request(request):
                return lead_panel_feedback(request, lead, error.message, level="error")
            messages.error(request, error.message)
            return redirect(f"{reverse('lead_detail', args=[lead.pk])}#lead-about")
        lead.assigned_to = assignee.user
        lead.save(update_fields=["assigned_to", "updated_at"])
        if is_lead_panel_request(request):
            return lead_panel_feedback(request, lead, "Ответственный за заявку назначен.")
        messages.success(request, "Ответственный за заявку назначен.")
        return redirect(f"{reverse('lead_detail', args=[lead.pk])}#lead-about")
