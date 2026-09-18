import json
import re

from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .models import AIContent, Client, ClientInteraction, ClientReminder, Lead, Property, RealtorProfile


PHONE_MASK_ATTRS = {
    "data-phone-mask": "true",
    "placeholder": "+7 (999) 999-99-99",
    "inputmode": "tel",
    "maxlength": "18",
}


class PropertyForm(forms.ModelForm):
    landing_block_order = forms.CharField(required=False, widget=forms.HiddenInput())
    landing_enabled_blocks = forms.CharField(required=False, widget=forms.HiddenInput())

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
            "landing_enabled_blocks",
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
        enabled = self.instance.landing_enabled_blocks or {key: True for key, _ in Property.LANDING_BLOCKS}
        self.landing_blocks = [(key, block_labels[key], bool(enabled.get(key, True))) for key in order]
        self.initial["landing_enabled_blocks"] = json.dumps({key: bool(enabled.get(key, True)) for key, _ in Property.LANDING_BLOCKS})
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

    def clean_landing_enabled_blocks(self):
        try:
            enabled = json.loads(self.cleaned_data["landing_enabled_blocks"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValidationError("Не удалось прочитать настройки блоков лендинга.") from error
        valid_keys = {key for key, _ in Property.LANDING_BLOCKS}
        if not isinstance(enabled, dict) or set(enabled) != valid_keys or not all(isinstance(value, bool) for value in enabled.values()):
            raise ValidationError("Настройки блоков лендинга содержат недопустимые значения.")
        return enabled


class RealtorProfileForm(forms.ModelForm):
    benefit_1_icon = forms.CharField(label="Иконка преимущества 1", max_length=8, required=False)
    benefit_1_title = forms.CharField(label="Заголовок преимущества 1", max_length=80, required=False)
    benefit_1_description = forms.CharField(label="Описание преимущества 1", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    benefit_2_icon = forms.CharField(label="Иконка преимущества 2", max_length=8, required=False)
    benefit_2_title = forms.CharField(label="Заголовок преимущества 2", max_length=80, required=False)
    benefit_2_description = forms.CharField(label="Описание преимущества 2", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    benefit_3_icon = forms.CharField(label="Иконка преимущества 3", max_length=8, required=False)
    benefit_3_title = forms.CharField(label="Заголовок преимущества 3", max_length=80, required=False)
    benefit_3_description = forms.CharField(label="Описание преимущества 3", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    class Meta:
        model = RealtorProfile
        fields = ("display_name", "photo", "phone", "telegram_username", "email", "about")
        widgets = {
            "photo": forms.FileInput(attrs={"accept": "image/*"}),
            "about": forms.Textarea(attrs={"rows": 4, "placeholder": "Например: Помогаю подобрать недвижимость и сопровождаю сделку на каждом этапе."}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        self.fields["phone"].widget.attrs.update(PHONE_MASK_ATTRS)
        self.fields["photo"].widget.attrs.update(
            {"class": "d-none", "x-ref": "photoInput", "x-on:change": "selectPhoto($event)"}
        )
        benefits = self.instance.benefits or []
        for index in range(1, 4):
            benefit = benefits[index - 1] if len(benefits) >= index else {}
            for key in ("icon", "title", "description"):
                self.initial[f"benefit_{index}_{key}"] = benefit.get(key, "")

    def save(self, commit=True):
        profile = super().save(commit=False)
        profile.benefits = [
            {"icon": self.cleaned_data.get(f"benefit_{index}_icon") or "◆", "title": self.cleaned_data.get(f"benefit_{index}_title") or "Преимущество", "description": self.cleaned_data.get(f"benefit_{index}_description") or ""}
            for index in range(1, 4)
        ]
        if commit:
            profile.save()
        return profile


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

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = (
            "name", "phone", "preferred_contact_time", "budget", "preferred_area",
            "source", "notes", "status", "outcome_reason",
        )
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "budget": forms.NumberInput(attrs={"step": "1000", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        self.fields["phone"].widget.attrs.update(PHONE_MASK_ATTRS)

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])


class ClientDetailsForm(ClientForm):
    class Meta(ClientForm.Meta):
        fields = (
            "name", "phone", "preferred_contact_time", "budget", "preferred_area",
            "source", "notes", "outcome_reason",
        )


class ClientInteractionForm(forms.ModelForm):
    class Meta:
        model = ClientInteraction
        fields = ("interaction_type", "text")
        widgets = {"text": forms.Textarea(attrs={"rows": 3, "placeholder": "Что произошло или о чём договорились?"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["interaction_type"].widget.attrs["class"] = "form-select"
        self.fields["text"].widget.attrs["class"] = "form-control"


class ClientReminderForm(forms.ModelForm):
    class Meta:
        model = ClientReminder
        fields = ("text", "due_at")
        widgets = {"due_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class AIRequestForm(forms.Form):
    MODE_CHOICES = [
        ("single", "Один текст"),
        ("package", "Комплект объявления"),
        ("audit", "Проверить готовность"),
        ("landing", "Комплект для лендинга"),
        ("improve", "Улучшить текст"),
        ("lead_reply", "Ответ клиенту"),
    ]
    IMPROVEMENT_CHOICES = [
        ("shorter", "Сделать короче"),
        ("stronger", "Сделать убедительнее"),
        ("premium", "Сделать премиальнее"),
        ("plain", "Убрать канцелярит"),
        ("audience", "Адаптировать для аудитории"),
    ]
    mode = forms.ChoiceField(label="Задача", choices=MODE_CHOICES, initial="single")
    content_type = forms.ChoiceField(
        label="Что создать",
        choices=AIContent.CONTENT_TYPES,
    )
    tone = forms.ChoiceField(label="Тон текста", choices=AIContent.TONE_CHOICES)
    improvement = forms.ChoiceField(label="Как улучшить", choices=IMPROVEMENT_CHOICES, required=False)
    source_text = forms.CharField(label="Исходный текст или аудитория", required=False, widget=forms.Textarea(attrs={"rows": 4}))
    lead = forms.ModelChoiceField(label="Заявка клиента", queryset=Lead.objects.none(), required=False)

    def __init__(self, *args, leads=None, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        self.fields["lead"].queryset = leads if leads is not None else Lead.objects.none()
        self.fields["source_text"].widget.attrs["placeholder"] = "Вставьте текст для улучшения или опишите аудиторию для адаптации."
        self.fields["mode"].widget.attrs["x-model"] = "mode"
        self.fields["content_type"].widget.attrs["x-model"] = "contentType"
        self.fields["tone"].widget.attrs["x-model"] = "tone"


class AIContentEditForm(forms.ModelForm):
    class Meta:
        model = AIContent
        fields = ("edited_content", "apply_target")
        widgets = {"edited_content": forms.Textarea(attrs={"rows": 12})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.edited_content:
            self.initial["edited_content"] = self.instance.content
        self.fields["edited_content"].widget.attrs["class"] = "form-control"
        self.fields["edited_content"].widget.attrs["x-ref"] = "text"
        self.fields["apply_target"].widget.attrs["class"] = "form-select"

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
