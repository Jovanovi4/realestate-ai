import json
import re

from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .models import AIContent, Lead, Property, RealtorProfile


PHONE_MASK_ATTRS = {
    "data-phone-mask": "true",
    "placeholder": "+7 (999) 999-99-99",
    "inputmode": "tel",
    "maxlength": "18",
}


class PropertyForm(forms.ModelForm):
    landing_block_order = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Property
        fields = [
            "title",
            "marketing_headline",
            "property_type",
            "status",
            "avito_export",
            "avito_operation",
            "price",
            "currency",
            "address",
            "area",
            "rooms",
            "bathrooms",
            "floor",
            "floors_total",
            "land_area",
            "year_built",
            "condition",
            "amenities",
            "short_description",
            "description",
            "landing_template",
            "landing_published",
            "landing_title",
            "landing_subtitle",
            "landing_about_title",
            "landing_contact_title",
            "seo_title",
            "seo_description",
            "landing_block_order",
        ]

        widgets = {
            "short_description": forms.Textarea(attrs={"rows": 3}),
            "description": forms.Textarea(attrs={"rows": 6}),
            "landing_subtitle": forms.Textarea(attrs={"rows": 3}),
            "seo_description": forms.Textarea(attrs={"rows": 2}),
            "amenities": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        order = self.instance.landing_block_order or [key for key, _ in Property.LANDING_BLOCKS]
        valid_keys = {key for key, _ in Property.LANDING_BLOCKS}
        if set(order) != valid_keys or len(order) != len(valid_keys):
            order = [key for key, _ in Property.LANDING_BLOCKS]
        self.initial["landing_block_order"] = json.dumps(order)
        block_labels = dict(Property.LANDING_BLOCKS)
        self.landing_blocks = [(key, block_labels[key]) for key in order]
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        self.fields["property_type"].widget.attrs["x-model"] = "propertyType"

    def clean_landing_block_order(self):
        try:
            order = json.loads(self.cleaned_data["landing_block_order"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValidationError("Не удалось прочитать порядок блоков лендинга.") from error
        valid_keys = {key for key, _ in Property.LANDING_BLOCKS}
        if not isinstance(order, list) or len(order) != len(valid_keys) or set(order) != valid_keys:
            raise ValidationError("Порядок блоков лендинга содержит недопустимые значения.")
        return order


class RealtorProfileForm(forms.ModelForm):
    class Meta:
        model = RealtorProfile
        fields = ("display_name", "photo", "phone", "telegram_username", "email")
        widgets = {"photo": forms.ClearableFileInput(attrs={"accept": "image/*"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        self.fields["phone"].widget.attrs.update(PHONE_MASK_ATTRS)
        self.fields["photo"].widget.attrs["x-on:change"] = "selectPhoto($event)"


class LeadForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ("name", "phone", "message")
        widgets = {"message": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["phone"].widget.attrs.update(PHONE_MASK_ATTRS)


class LeadStatusForm(forms.ModelForm):
    class Meta:
        model = Lead
        fields = ("status",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].widget.attrs["class"] = "form-select"


class AIRequestForm(forms.Form):
    content_type = forms.ChoiceField(
        label="Что создать",
        choices=AIContent.CONTENT_TYPES[:3],
    )
    tone = forms.ChoiceField(label="Тон текста", choices=AIContent.TONE_CHOICES)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select"
        self.fields["content_type"].widget.attrs["x-model"] = "contentType"
        self.fields["tone"].widget.attrs["x-model"] = "tone"


class AIContentEditForm(forms.ModelForm):
    class Meta:
        model = AIContent
        fields = ("edited_content",)
        widgets = {"edited_content": forms.Textarea(attrs={"rows": 12})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.edited_content:
            self.initial["edited_content"] = self.instance.content
        self.fields["edited_content"].widget.attrs["class"] = "form-control"
        self.fields["edited_content"].widget.attrs["x-ref"] = "text"

def normalize_phone(value):
    phone = re.sub(r"[\s()\-]", "", value.strip())
    if phone.startswith("00"):
        phone = f"+{phone[2:]}"
    if not re.fullmatch(r"\+?\d{7,15}", phone):
        raise ValidationError("Введите корректный номер телефона.")
    return phone


class RegistrationForm(forms.Form):
    phone = forms.CharField(label="Телефон", max_length=30, widget=forms.TelInput(attrs=PHONE_MASK_ATTRS))
    password1 = forms.CharField(label="Пароль", widget=forms.PasswordInput())
    password2 = forms.CharField(label="Повторите пароль", widget=forms.PasswordInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data["phone"])
        if User.objects.filter(username=phone).exists():
            raise ValidationError("Аккаунт с этим номером уже существует.")
        return phone

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Пароли не совпадают.")
        if password1:
            try:
                validate_password(password1)
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned_data

    def save(self):
        user = User(username=self.cleaned_data["phone"])
        user.set_password(self.cleaned_data["password1"])
        user.save()
        return user


class PhoneAuthenticationForm(AuthenticationForm):
    username = forms.CharField(label="Телефон", widget=forms.TelInput(attrs={**PHONE_MASK_ATTRS, "autofocus": True}))

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean_username(self):
        return normalize_phone(self.cleaned_data["username"])


class AccountPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
