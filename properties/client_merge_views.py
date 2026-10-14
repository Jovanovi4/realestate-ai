"""Review and confirm contact merges."""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from billing.enforcement import TariffDenied, require_resource_action
from billing.rules import Operation

from .agency_access import may_manage_agency, workspace_queryset
from .models import Client, ClientInteraction, ClientPropertySuggestion, ClientReminder, Deal, Lead, OwnerInquiry, PropertyOwnerLink
from .services.client_merge_service import ClientMergeConflict, merge_clients, merge_conflicts


class ClientMergeView(LoginRequiredMixin, View):
    template_name = "properties/client_merge.html"

    def contacts(self, request, pk, source_pk):
        visible = workspace_queryset(Client.objects, request.user)
        target = get_object_or_404(visible, pk=pk)
        source = get_object_or_404(visible, pk=source_pk)
        if target.agency_id and not may_manage_agency(request.user):
            return None, None
        if not target.agency_id and target.owner_id != request.user.pk:
            return None, None
        return target, source

    def get(self, request, pk, source_pk):
        target, source = self.contacts(request, pk, source_pk)
        if not target:
            return HttpResponseForbidden("Объединение контактов доступно владельцу личной базы или руководителю команды.")
        counts = {
            "Заявки": Lead.objects.filter(client=source).count(),
            "Обращения": OwnerInquiry.objects.filter(client=source).count(),
            "Задачи": ClientReminder.objects.filter(client=source).count(),
            "Сделки": Deal.objects.filter(client=source).count(),
            "Записи истории": ClientInteraction.objects.filter(client=source).count(),
            "Объекты собственника": PropertyOwnerLink.objects.filter(client=source).count(),
            "Предложения": ClientPropertySuggestion.objects.filter(client=source).count(),
        }
        return render(request, self.template_name, {
            "target": target, "source": source, "counts": counts,
            "conflicts": merge_conflicts(target, source),
        })

    def post(self, request, pk, source_pk):
        target, source = self.contacts(request, pk, source_pk)
        if not target:
            return HttpResponseForbidden("Недостаточно прав для объединения контактов.")
        if request.POST.get("confirm") != "merge":
            return redirect("client_merge", pk=pk, source_pk=source_pk)
        try:
            require_resource_action(request.user, target, Operation.EDIT_CRM)
            merge_clients(target_id=target.pk, source_id=source.pk, actor=request.user)
        except TariffDenied as error:
            messages.error(request, error.message)
            return redirect("client_merge", pk=pk, source_pk=source_pk)
        except ClientMergeConflict as error:
            messages.error(request, str(error))
            return redirect("client_merge", pk=pk, source_pk=source_pk)
        messages.success(request, "Контакты объединены. Заявки, задачи, сделки и история сохранены.")
        return redirect("client_detail", pk=target.pk)
