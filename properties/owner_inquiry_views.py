from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

from billing.enforcement import TariffDenied, require_new_action, require_resource_action
from billing.rules import Operation

from .agency_access import agency_for, may_manage_agency, workspace_queryset
from .forms import OwnerInquiryCreateForm, OwnerInquiryProcessForm, deal_responsible_label
from .models import AgencyMembership, Client, ClientInteraction, OwnerInquiry


class OwnerInquiryListView(LoginRequiredMixin, View):
    def get(self, request):
        status = request.GET.get("status", "active")
        if status not in {"active", "completed", "all"}:
            status = "active"
        client_id = request.GET.get("client", "")
        if client_id and (not client_id.isascii() or not client_id.isdigit()):
            raise Http404
        filter_client = get_object_or_404(
            workspace_queryset(Client.objects, request.user), pk=client_id,
        ) if client_id else None
        inquiries = workspace_queryset(OwnerInquiry.objects, request.user).select_related("client", "assigned_to__realtor_profile", "property")
        if filter_client:
            inquiries = inquiries.filter(client=filter_client)
        if status == "active":
            inquiries = inquiries.exclude(result="declined").filter(property__isnull=True)
        elif status == "completed":
            inquiries = inquiries.filter(result="declined") | inquiries.filter(property__isnull=False)
        page = Paginator(inquiries.order_by("-created_at"), 20).get_page(request.GET.get("page"))
        return render(request, "properties/owner_inquiry_list.html", {
            "page_obj": page, "inquiries": page.object_list, "status": status, "filter_client": filter_client,
        })


class OwnerInquiryCreateView(LoginRequiredMixin, View):
    template_name = "properties/owner_inquiry_form.html"

    def get(self, request):
        form = OwnerInquiryCreateForm(user=request.user)
        client_id = request.GET.get("client")
        if client_id and form.fields["client"].queryset.filter(pk=client_id).exists():
            form.initial["client"] = client_id
        return render(request, self.template_name, {"form": form})

    def post(self, request):
        form = OwnerInquiryCreateForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form}, status=400)
        agency = agency_for(request.user)
        try:
            require_new_action(request.user, Operation.EDIT_CRM, agency=agency)
            with transaction.atomic():
                client = form.cleaned_data["client"]
                if not client:
                    client = Client.objects.create(
                        owner=request.user, agency=agency, name=form.cleaned_data["contact_name"].strip(),
                        phone=form.cleaned_data["contact_phone"], source=form.cleaned_data["source"], contact_role="owner",
                    )
                elif client.contact_role == "seeker":
                    Client.objects.filter(pk=client.pk, contact_role="seeker").update(contact_role="both", updated_at=timezone.now())
                inquiry = form.save(commit=False)
                inquiry.owner = request.user
                inquiry.agency = agency
                inquiry.client = client
                inquiry.next_action_due_at = form.cleaned_data["first_response_due_at"]
                inquiry.save()
        except TariffDenied as error:
            form.add_error(None, error.message)
            return render(request, self.template_name, {"form": form}, status=403)
        except (IntegrityError, ValidationError):
            form.add_error(None, "Не удалось сохранить обращение. Проверьте контакт и повторите попытку.")
            return render(request, self.template_name, {"form": form}, status=400)
        messages.success(request, "Обращение собственника добавлено.")
        return redirect("owner_inquiry_detail", pk=inquiry.pk)


class OwnerInquiryDetailView(LoginRequiredMixin, View):
    template_name = "properties/owner_inquiry_detail.html"

    def get(self, request, pk):
        inquiry = get_object_or_404(workspace_queryset(OwnerInquiry.objects, request.user).select_related(
            "client", "assigned_to__realtor_profile", "property",
        ), pk=pk)
        assignees = (User.objects.filter(pk__in=AgencyMembership.objects.filter(
            agency=inquiry.agency,
        ).values("user_id")).select_related("realtor_profile") if inquiry.agency_id and may_manage_agency(request.user) else None)
        return render(request, self.template_name, {
            "inquiry": inquiry,
            "form": OwnerInquiryProcessForm(inquiry=inquiry),
            "contact_history": ClientInteraction.objects.filter(client=inquiry.client, property__isnull=True).order_by("-created_at")[:8],
            "assignees": [(person, deal_responsible_label(person)) for person in assignees] if assignees is not None else None,
        })


class OwnerInquiryProcessView(LoginRequiredMixin, View):
    def post(self, request, pk):
        with transaction.atomic():
            inquiry = get_object_or_404(workspace_queryset(
                OwnerInquiry.objects.select_for_update().select_related("client", "property"), request.user,
            ), pk=pk)
            if inquiry.property_id:
                messages.info(request, "Объект уже создан из этого обращения.")
                return redirect("owner_inquiry_detail", pk=pk)
            form = OwnerInquiryProcessForm(request.POST, inquiry=inquiry)
            if not form.is_valid():
                return render(request, "properties/owner_inquiry_detail.html", {
                    "inquiry": inquiry, "form": form, "contact_history": [],
                }, status=400)
            try:
                require_resource_action(request.user, inquiry, Operation.EDIT_CRM)
            except TariffDenied as error:
                form.add_error(None, error.message)
                return render(request, "properties/owner_inquiry_detail.html", {
                    "inquiry": inquiry, "form": form, "contact_history": [],
                }, status=403)
            now = timezone.now()
            first_conversation = inquiry.first_contacted_at is None
            inquiry.first_contacted_at = inquiry.first_contacted_at or now
            inquiry.result = form.cleaned_data["result"]
            inquiry.result_note = form.cleaned_data["result_note"].strip()
            inquiry.next_action = form.cleaned_data.get("next_action", "").strip()
            inquiry.next_action_due_at = form.cleaned_data.get("next_action_due_at") or now
            inquiry.save()
            Client.objects.filter(pk=inquiry.client_id, first_contacted_at__isnull=True).update(first_contacted_at=now)
            ClientInteraction.objects.create(
                client=inquiry.client, interaction_type="call" if first_conversation else "note",
                text=f"Обращение #{inquiry.pk}: {inquiry.get_result_display()}. {inquiry.result_note}",
            )
        messages.success(request, "Результат разговора и следующий шаг сохранены.")
        return redirect("owner_inquiry_detail", pk=pk)


class OwnerInquiryAssignView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not may_manage_agency(request.user):
            raise Http404
        with transaction.atomic():
            inquiry = get_object_or_404(workspace_queryset(OwnerInquiry.objects.select_for_update(), request.user), pk=pk)
            if not inquiry.agency_id:
                raise Http404
            assignee_id = request.POST.get("assigned_to", "")
            if not assignee_id.isdigit():
                raise Http404
            assignee = get_object_or_404(AgencyMembership, agency_id=inquiry.agency_id, user_id=assignee_id)
            try:
                require_resource_action(request.user, inquiry, Operation.EDIT_CRM)
            except TariffDenied as error:
                messages.error(request, error.message)
                return redirect("owner_inquiry_detail", pk=pk)
            inquiry.assigned_to = assignee.user
            inquiry.save(update_fields=["assigned_to", "updated_at"])
        messages.success(request, "Ответственный назначен.")
        return redirect("owner_inquiry_detail", pk=pk)
