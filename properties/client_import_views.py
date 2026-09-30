from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.views import View

from billing.enforcement import TariffDenied, require_new_action
from billing.rules import Operation

from .agency_access import agency_for
from .forms import ClientCsvImportForm
from .services.client_import_service import ClientImportError, import_clients


class ClientImportView(LoginRequiredMixin, View):
    template_name = "properties/client_import.html"

    def get(self, request):
        return render(request, self.template_name, {"form": ClientCsvImportForm(), "agency": agency_for(request.user)})

    def post(self, request):
        form = ClientCsvImportForm(request.POST, request.FILES)
        row_errors = ()
        status = 200
        if form.is_valid():
            try:
                require_new_action(request.user, Operation.EDIT_CRM, agency=agency_for(request.user))
            except TariffDenied as error:
                form.add_error(None, error.message)
                status = 403
            else:
                try:
                    result = import_clients(form.cleaned_data["file"], request.user)
                except ClientImportError as error:
                    form.add_error(None, str(error))
                    row_errors = error.row_errors
                else:
                    message = f"Импорт завершён: добавлено {result.created}, пропущено совпадений {result.skipped}."
                    if result.created:
                        messages.success(request, message)
                    else:
                        messages.info(request, message)
                    return redirect("client_list")
        return render(request, self.template_name, {
            "form": form, "row_errors": row_errors, "agency": agency_for(request.user),
        }, status=status)
