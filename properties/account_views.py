import uuid

from django import forms
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import urlsafe_base64_decode
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.generic import FormView

from .forms import PHONE_MASK_ATTRS, mark_invalid_fields, normalize_phone
from .models import RealtorProfile
from .services.account_email_service import email_verification_token, enqueue_account_email, password_recovery_token


class SecurityForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        mark_invalid_fields(self)


class AccountRecoveryForm(SecurityForm):
    phone = forms.CharField(label="Телефон для входа", max_length=30, widget=forms.TelInput(attrs=PHONE_MASK_ATTRS))
    email = forms.EmailField(label="Подтверждённый email", max_length=254, widget=forms.EmailInput(attrs={"autocomplete": "email"}))

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])


class VerifyEmailForm(SecurityForm):
    current_password = forms.CharField(label="Текущий пароль", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not self.user.check_password(password):
            raise ValidationError("Проверьте текущий пароль.")
        return password


class AccountSetPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        mark_invalid_fields(self)


@method_decorator(never_cache, name="dispatch")
class AccountRecoveryView(FormView):
    template_name = "registration/account_security.html"
    form_class = AccountRecoveryForm
    success_url = reverse_lazy("password_reset_done")
    extra_context = {
        "title": "Восстановление доступа",
        "description": "Укажите телефон аккаунта и email, ранее подтверждённый в личном кабинете.",
        "submit_label": "Получить ссылку", "link_name": "login", "link_label": "Вернуться ко входу",
    }

    def form_valid(self, form):
        user = User.objects.filter(
            username=form.cleaned_data["phone"], is_active=True,
            realtor_profile__email__iexact=form.cleaned_data["email"],
            realtor_profile__email_verified_at__isnull=False,
        ).first()
        if user and not user.has_usable_password():
            user = None
        # Same queue write and response for known, unverified and unknown users.
        # SMTP runs outside the HTTP request to avoid a mail-delivery timing oracle.
        enqueue_account_email(user, "reset")
        return super().form_valid(form)


class AccountResetConfirmView(auth_views.PasswordResetConfirmView):
    form_class = AccountSetPasswordForm
    template_name = "registration/password_reset_confirm.html"
    token_generator = password_recovery_token
    title = "Новый пароль"
    extra_context = {"description": "Задайте новый пароль для входа в кабинет.", "submit_label": "Сохранить пароль"}


@method_decorator([never_cache, sensitive_post_parameters("current_password")], name="dispatch")
class VerifyEmailView(LoginRequiredMixin, FormView):
    template_name = "registration/account_security.html"
    form_class = VerifyEmailForm
    success_url = reverse_lazy("account_email_sent")
    extra_context = {
        "title": "Подтверждение email", "submit_label": "Отправить ссылку",
        "link_name": "edit_profile", "link_label": "Вернуться в профиль",
    }

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "user": self.request.user}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile, _ = RealtorProfile.objects.get_or_create(user=self.request.user)
        context["description"] = f"Отправим ссылку на {profile.email}. Подтверждение позволит восстановить доступ, если вы забудете пароль." if profile.email else "Сначала сохраните email в личном кабинете."
        return context

    def form_valid(self, form):
        with transaction.atomic():
            profile = RealtorProfile.objects.select_for_update().get(user=self.request.user)
            if not profile.email:
                form.add_error(None, "Сначала сохраните email в личном кабинете.")
                return self.form_invalid(form)
            if profile.email_verified_at:
                return redirect("edit_profile")
            profile.email_verification_nonce = uuid.uuid4()
            profile.save(update_fields=["email_verification_nonce"])
            enqueue_account_email(self.request.user, "verify")
        return super().form_valid(form)


@method_decorator(never_cache, name="dispatch")
class ConfirmEmailView(View):
    template_name = "registration/account_security.html"

    @staticmethod
    def get_user(uidb64):
        try:
            if len(uidb64) > 100:
                return None
            pk = User._meta.pk.to_python(urlsafe_base64_decode(uidb64).decode())
            return User.objects.get(pk=pk)
        except (ValueError, TypeError, OverflowError, UnicodeDecodeError, ValidationError, User.DoesNotExist):
            return None

    def get(self, request, uidb64, token):
        user = self.get_user(uidb64)
        valid = email_verification_token.check_token(user, token)
        return render(request, self.template_name, {
            "title": "Подтвердите email" if valid else "Ссылка недействительна",
            "description": "Нажмите кнопку, чтобы включить восстановление доступа через этот email." if valid else "Ссылка истекла или уже использована. Запросите новую в личном кабинете.",
            "form": forms.Form() if valid else None, "submit_label": "Подтвердить email",
            "link_name": "edit_profile" if request.user.is_authenticated else "login",
            "link_label": "Вернуться в личный кабинет" if request.user.is_authenticated else "Вернуться ко входу",
        })

    def post(self, request, uidb64, token):
        user = self.get_user(uidb64)
        if not user:
            return self.get(request, uidb64, token)
        with transaction.atomic():
            profile = RealtorProfile.objects.select_for_update().filter(user=user).first()
            if not profile or not email_verification_token.check_token(user, token):
                return self.get(request, uidb64, token)
            profile.email_verified_at = timezone.now()
            profile.email_verification_nonce = uuid.uuid4()
            profile.save(update_fields=["email_verified_at", "email_verification_nonce"])
        if request.user.is_authenticated and request.user.pk == user.pk:
            messages.success(request, "Email подтверждён. Теперь его можно использовать для восстановления доступа.")
            return redirect("edit_profile")
        return render(request, self.template_name, {
            "title": "Email подтверждён", "description": "Теперь его можно использовать для восстановления доступа. Подтверждение email не выполняет вход в аккаунт.",
            "link_name": "edit_profile" if request.user.is_authenticated else "login",
            "link_label": "Вернуться в личный кабинет" if request.user.is_authenticated else "Перейти ко входу",
        })
