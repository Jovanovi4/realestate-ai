import json
import re
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone
from .agency_access import agency_for, may_manage_agency, workspace_queryset
from .models import AIContent, Client, ClientInteraction, ClientPropertySuggestion, ClientReminder, Deal, LandingStylePreset, Lead, OwnerInquiry, Property, PropertyOwnerLink, RealtorProfile, Showing, same_workspace
from .services.image_upload_service import prepare_image_upload
from .services.staff_display import staff_display_name
from .landing_colors import normalize_hex


PHONE_MASK_ATTRS = {
    "data-phone-mask": "true",
    "placeholder": "+7 (999) 999-99-99",
    "inputmode": "tel",
    "autocomplete": "tel",
    "maxlength": "18",
}


def mark_invalid_fields(form):
    """Expose server-side validation state to the shared form styles and assistive tech."""
    if not form.is_bound:
        return
    for name in form.errors:
        if name not in form.fields:
            continue
        attrs = form.fields[name].widget.attrs
        attrs["aria-invalid"] = "true"
        classes = attrs.get("class", "").split()
        if "is-invalid" not in classes:
            attrs["class"] = " ".join([*classes, "is-invalid"])


class SafeImageField(forms.FileField):
    """Check size before Pillow decoding, then replace input with sanitized pixels."""

    def __init__(self, *args, purpose, **kwargs):
        self.purpose = purpose
        super().__init__(*args, **kwargs)
        self.help_text = f"JPG, PNG или WEBP · до {settings.PROFILE_IMAGE_MAX_MB} МБ."

    def to_python(self, data):
        uploaded_file = super().to_python(data)
        return prepare_image_upload(uploaded_file, purpose=self.purpose) if uploaded_file else None


class PropertyForm(forms.ModelForm):
    landing_logo = SafeImageField(purpose="logo", required=False, label="Логотип в шапке лендинга", widget=forms.ClearableFileInput)
    landing_block_order = forms.CharField(required=False, widget=forms.HiddenInput())
    landing_enabled_blocks = forms.CharField(required=False, widget=forms.HiddenInput())
    landing_benefit_1_icon = forms.CharField(label="Иконка", max_length=8, required=False)
    landing_benefit_1_title = forms.CharField(label="Заголовок", max_length=80, required=False)
    landing_benefit_1_description = forms.CharField(label="Описание", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    landing_benefit_2_icon = forms.CharField(label="Иконка", max_length=8, required=False)
    landing_benefit_2_title = forms.CharField(label="Заголовок", max_length=80, required=False)
    landing_benefit_2_description = forms.CharField(label="Описание", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    landing_benefit_3_icon = forms.CharField(label="Иконка", max_length=8, required=False)
    landing_benefit_3_title = forms.CharField(label="Заголовок", max_length=80, required=False)
    landing_benefit_3_description = forms.CharField(label="Описание", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    class Meta:
        model = Property
        fields = [
            "title",
            "marketing_headline",
            "property_type",
            "deal_type",
            "status",
            "avito_export",
            "cian_export",
            "avito_operation",
            "price",
            "currency",
            "address",
            "area",
            "commercial_purpose",
            "ceiling_height_m",
            "power_kw",
            "entrance_type",
            "parking_type",
            "access_mode",
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
            "rent_deposit",
            "rent_commission",
            "utilities_terms",
            "min_lease_months",
            "available_from",
            "furnished",
            "pets_allowed",
            "children_allowed",
            "landing_template",
            "landing_accent",
            "landing_custom_color",
            "landing_button_style",
            "landing_hero_layout",
            "landing_gallery_style",
            "landing_published",
            "landing_title",
            "landing_subtitle",
            "landing_about_title",
            "landing_contact_title",
            "landing_trust_about",
            "landing_logo",
            "seo_title",
            "seo_description",
            "landing_block_order",
            "landing_enabled_blocks",
        ]

        widgets = {
            "short_description": forms.Textarea(attrs={"rows": 3}),
            "description": forms.Textarea(attrs={"rows": 6}),
            "landing_subtitle": forms.Textarea(attrs={"rows": 3}),
            "landing_trust_about": forms.Textarea(attrs={"rows": 3, "placeholder": "Например: Сопровождаем сделку от первого просмотра до передачи ключей."}),
            "seo_description": forms.Textarea(attrs={"rows": 2}),
            "amenities": forms.Textarea(attrs={"rows": 3}),
            "available_from": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        order = self.instance.landing_block_order or [key for key, _ in Property.LANDING_BLOCKS]
        valid_keys = {key for key, _ in Property.LANDING_BLOCKS}
        if set(order) != valid_keys or len(order) != len(valid_keys):
            order = [key for key, _ in Property.LANDING_BLOCKS]
        self._default_landing_block_order = order
        self.initial["landing_block_order"] = json.dumps(order)
        block_labels = dict(Property.LANDING_BLOCKS)
        enabled = self.instance.landing_enabled_blocks or {key: True for key, _ in Property.LANDING_BLOCKS}
        self._default_landing_enabled_blocks = {
            key: bool(enabled.get(key, True)) for key, _ in Property.LANDING_BLOCKS
        }
        self.landing_blocks = [(key, block_labels[key], bool(enabled.get(key, True))) for key in order]
        self.initial["landing_enabled_blocks"] = json.dumps(self._default_landing_enabled_blocks)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        self.fields["property_type"].widget.attrs["x-model"] = "propertyType"
        self.fields["deal_type"].widget.attrs["x-model"] = "dealType"
        selected_type = self.data.get(self.add_prefix("property_type")) if self.is_bound else self.initial.get("property_type", self.instance.property_type)
        self.property_group = "commercial" if selected_type in Property.COMMERCIAL_TYPES else "residential"
        self.residential_types = [(code, label) for code, label in Property.PROPERTY_TYPES if code in Property.RESIDENTIAL_TYPES]
        self.commercial_types = [(code, label) for code, label in Property.PROPERTY_TYPES if code in Property.COMMERCIAL_TYPES]
        for name in ("area", "ceiling_height_m", "power_kw"):
            self.fields[name].widget.attrs.update({"step": "0.01", "min": "0", "inputmode": "decimal"})
        self.fields["landing_logo"].widget.attrs.update({
            "x-ref": "logoInput",
            "@change": "selectLogo($event)",
            "accept": "image/png,image/jpeg,image/webp",
        })
        self._visual_landing_defaults = {
            "landing_accent": "template",
            "landing_button_style": "template",
            "landing_hero_layout": "split",
            "landing_gallery_style": "large",
        }
        for field_name in self._visual_landing_defaults:
            self.fields[field_name].required = False
        self.fields["landing_custom_color"].widget.attrs.update({
            "placeholder": "#2563EB", "maxlength": "7", "spellcheck": "false", "autocomplete": "off",
            "inputmode": "text", "aria-label": "Шестизначный код цвета",
        })
        benefits = self.instance.landing_benefits or []
        for index in range(1, 4):
            benefit = benefits[index - 1] if len(benefits) >= index else {}
            for key in ("icon", "title", "description"):
                self.initial[f"landing_benefit_{index}_{key}"] = benefit.get(key, "")
        mark_invalid_fields(self)

    def clean_status(self):
        status = self.cleaned_data["status"]
        old_status = Property.objects.filter(pk=self.instance.pk).values_list("status", flat=True).first() if self.instance.pk else None
        if old_status == "archived" and status != "archived":
            raise ValidationError("Сначала восстановите объект отдельной кнопкой в его карточке.")
        agency = self.instance.agency if self.instance.agency_id else (agency_for(self.user) if self.user and not self.instance.pk else None)
        if self.user and agency:
            if (status == "archived" or old_status == "archived") and status != old_status and not may_manage_agency(self.user):
                raise ValidationError("Архивировать и восстанавливать общие объекты может руководитель или менеджер команды.")
        return status

    def clean_landing_published(self):
        published = self.cleaned_data.get("landing_published", False)
        if published and self.cleaned_data.get("status") == "archived":
            raise ValidationError("Сначала восстановите объект из архива, затем опубликуйте лендинг.")
        return published

    def clean_landing_custom_color(self):
        raw = self.cleaned_data.get("landing_custom_color", "")
        if not raw:
            return ""
        color = normalize_hex(raw)
        if not color:
            raise ValidationError("Укажите цвет в формате #RRGGBB.")
        return color

    def save(self, commit=True):
        property = super().save(commit=False)
        property.landing_benefits = [
            {
                "icon": self.cleaned_data.get(f"landing_benefit_{index}_icon") or "◆",
                "title": self.cleaned_data.get(f"landing_benefit_{index}_title") or "Преимущество",
                "description": self.cleaned_data.get(f"landing_benefit_{index}_description") or "",
            }
            for index in range(1, 4)
        ]
        if commit:
            property.save()
            self.save_m2m()
        return property

    def clean(self):
        cleaned_data = super().clean()
        old_type = Property.objects.filter(pk=self.instance.pk).values_list("property_type", flat=True).first() if self.instance.pk else None
        if cleaned_data.get("property_type") in Property.COMMERCIAL_TYPES:
            cleaned_data["avito_export"] = False
            cleaned_data["cian_export"] = False
            if old_type in Property.COMMERCIAL_TYPES:
                for name in ("rooms", "bathrooms", "land_area", "furnished", "pets_allowed", "children_allowed"):
                    cleaned_data[name] = getattr(self.instance, name)
            else:
                for name in ("rooms", "bathrooms", "land_area"):
                    cleaned_data[name] = None
                for name in ("furnished", "pets_allowed", "children_allowed"):
                    cleaned_data[name] = False
        elif cleaned_data.get("property_type") in Property.RESIDENTIAL_TYPES:
            for name in ("commercial_purpose", "entrance_type", "parking_type", "access_mode"):
                cleaned_data[name] = ""
            for name in ("ceiling_height_m", "power_kw"):
                cleaned_data[name] = None
        for field_name, default in self._visual_landing_defaults.items():
            if not cleaned_data.get(field_name):
                cleaned_data[field_name] = getattr(self.instance, field_name, None) or default
        if cleaned_data.get("landing_accent") == "custom" and not cleaned_data.get("landing_custom_color"):
            self.add_error("landing_custom_color", "Выберите цвет или укажите код #RRGGBB.")
        if cleaned_data.get("deal_type") == "rent":
            if cleaned_data.get("status") == "sold":
                self.add_error("status", "Для аренды используйте статусы «Свободен», «Бронь» или «Сдан».")
            if not cleaned_data.get("utilities_terms"):
                self.add_error("utilities_terms", "Укажите условия оплаты коммунальных услуг.")
        return cleaned_data

    def clean_landing_block_order(self):
        raw_order = self.cleaned_data["landing_block_order"]
        if not raw_order:
            return self._default_landing_block_order
        try:
            order = json.loads(raw_order)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValidationError("Не удалось прочитать порядок блоков лендинга.") from error
        valid_keys = {key for key, _ in Property.LANDING_BLOCKS}
        if not isinstance(order, list) or len(order) != len(valid_keys) or set(order) != valid_keys:
            raise ValidationError("Порядок блоков лендинга содержит недопустимые значения.")
        return order

    def clean_landing_enabled_blocks(self):
        raw_enabled = self.cleaned_data["landing_enabled_blocks"]
        if not raw_enabled:
            return self._default_landing_enabled_blocks
        try:
            enabled = json.loads(raw_enabled)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValidationError("Не удалось прочитать настройки блоков лендинга.") from error
        valid_keys = {key for key, _ in Property.LANDING_BLOCKS}
        if not isinstance(enabled, dict) or set(enabled) != valid_keys or not all(isinstance(value, bool) for value in enabled.values()):
            raise ValidationError("Настройки блоков лендинга содержат недопустимые значения.")
        return enabled


class LandingStylePresetForm(forms.ModelForm):
    class Meta:
        model = LandingStylePreset
        fields = (
            "name", "landing_template", "landing_accent", "landing_custom_color",
            "landing_button_style", "landing_hero_layout", "landing_gallery_style",
        )

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise ValidationError("Назовите пресет.")
        return name

    def clean_landing_custom_color(self):
        raw = self.cleaned_data.get("landing_custom_color", "")
        if not raw:
            return ""
        color = normalize_hex(raw)
        if not color:
            raise ValidationError("Укажите цвет в формате #RRGGBB.")
        return color

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("landing_accent") == "custom" and not cleaned.get("landing_custom_color"):
            self.add_error("landing_custom_color", "Выберите цвет для пресета.")
        return cleaned


class RealtorProfileForm(forms.ModelForm):
    photo = SafeImageField(purpose="profile", required=False, label="Фото", widget=forms.FileInput(attrs={"accept": "image/png,image/jpeg,image/webp"}))

    class Meta:
        model = RealtorProfile
        fields = ("display_name", "photo", "phone", "telegram_username", "email", "telegram_chat_id")
        widgets = {
            "photo": forms.FileInput(attrs={"accept": "image/png,image/jpeg,image/webp"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        self.fields["phone"].widget.attrs.update(PHONE_MASK_ATTRS)
        self.fields["photo"].widget.attrs.update(
            {"class": "d-none", "x-ref": "photoInput", "x-on:change": "selectPhoto($event)"}
        )
        mark_invalid_fields(self)


class LeadForm(forms.ModelForm):
    website = forms.CharField(required=False, widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}))
    personal_data_consent = forms.BooleanField(
        required=True,
        label="Даю согласие на обработку персональных данных",
        error_messages={"required": "Для отправки заявки необходимо согласие на обработку персональных данных."},
    )

    class Meta:
        model = Lead
        fields = ("contact_purpose", "name", "phone", "message")
        widgets = {
            "contact_purpose": forms.Select(),
            "message": forms.Textarea(attrs={"rows": 3, "placeholder": "Необязательно: удобное время, вопрос или пожелание"}),
        }

    def __init__(self, *args, property=None, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"
        self.fields["phone"].widget.attrs.update(PHONE_MASK_ATTRS)
        if property and property.deal_type == "rent":
            self.initial.setdefault("contact_purpose", "rent_terms")

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])


class LeadProcessingForm(forms.Form):
    source = forms.ChoiceField(label="Источник", choices=Lead.SOURCE_CHOICES)
    qualification = forms.ChoiceField(label="Результат квалификации", choices=Lead.QUALIFICATION_CHOICES)
    next_action = forms.CharField(label="Следующее действие", max_length=255)
    next_action_due_at = forms.DateTimeField(
        label="Когда выполнить",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    contact_method = forms.ChoiceField(
        label="Способ связи", required=False,
        choices=[("", "Выберите способ"), ("call", "Звонок"), ("message", "Сообщение")],
    )
    contact_note = forms.CharField(label="Итог разговора", required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, lead=None, **kwargs):
        if lead is not None and not args and "initial" not in kwargs:
            kwargs["initial"] = {
                "source": lead.source,
                "qualification": lead.qualification,
                "next_action": lead.next_action,
                "next_action_due_at": lead.next_action_due_at,
            }
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs["id"] = f"id_lead_{name}"
        mark_invalid_fields(self)

    def clean_next_action(self):
        value = self.cleaned_data["next_action"].strip()
        if not value:
            raise forms.ValidationError("Укажите следующее действие.")
        return value

    def clean_next_action_due_at(self):
        value = self.cleaned_data["next_action_due_at"]
        from django.utils import timezone
        if value <= timezone.now():
            raise forms.ValidationError("Укажите время в будущем.")
        return value


class LeadActionCompleteForm(forms.Form):
    task_id = forms.IntegerField(widget=forms.HiddenInput)
    outcome_note = forms.CharField(label="Итог действия", max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))
    next_action = forms.CharField(label="Следующее действие", max_length=255)
    next_action_due_at = forms.DateTimeField(
        label="Когда выполнить",
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["id"] = f"id_complete_{name}"
            if not isinstance(field.widget, forms.HiddenInput):
                field.widget.attrs["class"] = "form-control"
        mark_invalid_fields(self)

    def clean_outcome_note(self):
        return self.cleaned_data["outcome_note"].strip()

    def clean_next_action(self):
        return self.cleaned_data["next_action"].strip()

    def clean_next_action_due_at(self):
        from django.utils import timezone
        value = self.cleaned_data["next_action_due_at"]
        if value <= timezone.now():
            raise forms.ValidationError("Укажите время в будущем.")
        return value


class LeadDeclineForm(forms.Form):
    reason = forms.CharField(
        label="Причина закрытия", max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-control", "id": "id_lead_decline_reason", "rows": 3, "placeholder": "Почему работа по заявке завершена?"}),
    )

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if not reason:
            raise forms.ValidationError("Укажите причину закрытия заявки.")
        return reason


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
        mark_invalid_fields(self)

    def clean_phone(self):
        return normalize_phone(self.cleaned_data["phone"])


class ClientDetailsForm(ClientForm):
    class Meta(ClientForm.Meta):
        fields = (
            "name", "phone", "preferred_contact_time", "budget", "preferred_area",
            "source", "notes", "outcome_reason",
        )


class OwnerContactForm(ClientForm):
    class Meta(ClientForm.Meta):
        fields = ("name", "phone", "preferred_contact_time", "notes")


class OwnerInquiryCreateForm(forms.ModelForm):
    client = forms.ModelChoiceField(queryset=Client.objects.none(), required=False, label="Существующий контакт")
    contact_name = forms.CharField(max_length=150, required=False, label="Имя собственника")
    contact_phone = forms.CharField(max_length=30, required=False, label="Телефон")

    class Meta:
        model = OwnerInquiry
        fields = ("client", "deal_type", "address", "description", "expected_price", "currency", "source",
                  "assigned_to", "first_response_due_at")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "first_response_due_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "expected_price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        agency = agency_for(user)
        self.contact_scope = (Client.objects.filter(agency=agency) if agency else
                              Client.objects.filter(agency__isnull=True, owner=user))
        self.fields["client"].queryset = self.contact_scope.order_by("name", "pk")
        if agency and may_manage_agency(user):
            from .models import AgencyMembership
            self.fields["assigned_to"].queryset = User.objects.filter(
                pk__in=AgencyMembership.objects.filter(agency=agency).values("user_id"),
            ).select_related("realtor_profile").order_by("first_name", "username")
        else:
            self.fields["assigned_to"].queryset = User.objects.filter(pk=user.pk)
        self.fields["assigned_to"].label_from_instance = deal_responsible_label
        self.fields["assigned_to"].initial = user.pk
        self.fields["first_response_due_at"].initial = timezone.localtime(
            timezone.now() + timedelta(minutes=30),
        ).strftime("%Y-%m-%dT%H:%M")
        self.fields["contact_phone"].widget.attrs.update(PHONE_MASK_ATTRS)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-select" if isinstance(field.widget, forms.Select) else "form-control")
        mark_invalid_fields(self)

    def clean_contact_phone(self):
        phone = self.cleaned_data.get("contact_phone", "")
        return normalize_phone(phone) if phone else ""

    def clean(self):
        data = super().clean()
        client = data.get("client")
        name = (data.get("contact_name") or "").strip()
        phone = data.get("contact_phone")
        if client and (name or phone):
            self.add_error("client", "Выберите существующий контакт или заполните новый.")
        if not client:
            if not name:
                self.add_error("contact_name", "Укажите имя собственника.")
            if not phone:
                self.add_error("contact_phone", "Укажите телефон.")
            if phone and self.contact_scope.filter(phone=phone).exists():
                self.add_error("contact_phone", "Контакт с этим номером уже есть. Выберите его из списка.")
        if not (data.get("address") or "").strip() and not (data.get("description") or "").strip():
            self.add_error("address", "Укажите адрес или описание недвижимости.")
        if data.get("first_response_due_at") and data["first_response_due_at"] <= timezone.now():
            self.add_error("first_response_due_at", "Срок первого ответа должен быть в будущем.")
        return data


class OwnerInquiryProcessForm(forms.Form):
    result = forms.ChoiceField(choices=OwnerInquiry.RESULT_CHOICES[1:], label="Результат разговора")
    result_note = forms.CharField(label="Что обсудили", widget=forms.Textarea(attrs={"rows": 3}))
    next_action = forms.CharField(max_length=255, required=False, label="Следующее действие")
    next_action_due_at = forms.DateTimeField(required=False, label="Когда выполнить",
                                             widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
                                             input_formats=["%Y-%m-%dT%H:%M"])

    def __init__(self, *args, inquiry=None, **kwargs):
        super().__init__(*args, **kwargs)
        if inquiry and not self.is_bound:
            self.initial.update({
                "result": inquiry.result if inquiry.result != "pending" else "follow_up",
                "result_note": inquiry.result_note,
                "next_action": inquiry.next_action if inquiry.first_contacted_at else "",
                "next_action_due_at": timezone.localtime(inquiry.next_action_due_at).strftime("%Y-%m-%dT%H:%M"),
            })
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        mark_invalid_fields(self)

    def clean(self):
        data = super().clean()
        if data.get("result") != "declined":
            if not (data.get("next_action") or "").strip():
                self.add_error("next_action", "Укажите следующий шаг.")
            if not data.get("next_action_due_at"):
                self.add_error("next_action_due_at", "Укажите срок следующего шага.")
            elif data["next_action_due_at"] <= timezone.now():
                self.add_error("next_action_due_at", "Срок должен быть в будущем.")
        return data


class PropertyOwnerAttachForm(forms.Form):
    client = forms.ModelChoiceField(queryset=Client.objects.none(), label="Контакт")
    role = forms.ChoiceField(choices=PropertyOwnerLink.ROLE_CHOICES, label="Роль по объекту")

    def __init__(self, *args, user, property, **kwargs):
        super().__init__(*args, **kwargs)
        scope = Client.objects.filter(agency=property.agency) if property.agency_id else Client.objects.filter(agency__isnull=True, owner=property.owner)
        self.fields["client"].queryset = scope.exclude(
            pk__in=property.owner_links.values_list("client_id", flat=True),
        ).order_by("name", "pk")
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select"


class PropertyOwnerDetailsForm(forms.ModelForm):
    publication_approved = forms.BooleanField(required=False, label="Публикация согласована")

    class Meta:
        model = PropertyOwnerLink
        fields = ("role", "agreed_price", "agreement_expires_on", "showing_instructions")
        widgets = {
            "agreement_expires_on": forms.DateInput(attrs={"type": "date"}),
            "agreed_price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["publication_approved"].initial = bool(self.instance.publication_approved_at)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        mark_invalid_fields(self)


class ClientSearchForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = (
            "search_deal_type", "search_segment", "search_property_type", "search_rooms_min",
            "search_area_min", "search_area_max", "search_commercial_purpose",
            "preferred_area", "budget", "search_currency", "search_notes",
        )
        widgets = {
            "budget": forms.NumberInput(attrs={"step": "1000", "min": "0"}),
            "search_area_min": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "search_area_max": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "search_notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {"budget": "Бюджет до"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        if not self.is_bound and not self.initial.get("search_segment") and self.instance.search_property_type:
            self.initial["search_segment"] = (
                "commercial" if self.instance.search_property_type in Property.COMMERCIAL_TYPES else "residential"
            )
        self.fields["search_property_type"].widget.attrs["data-commercial-types"] = ",".join(sorted(Property.COMMERCIAL_TYPES))
        mark_invalid_fields(self)

    def clean(self):
        data = super().clean()
        if data.get("budget") and not data.get("search_currency"):
            self.add_error("search_currency", "Укажите валюту бюджета для подбора по цене.")
        minimum, maximum = data.get("search_area_min"), data.get("search_area_max")
        if minimum is not None and maximum is not None and minimum > maximum:
            self.add_error("search_area_max", "Максимальная площадь не может быть меньше минимальной.")
        segment = data.get("search_segment")
        property_type = data.get("search_property_type")
        if property_type:
            type_segment = "commercial" if property_type in Property.COMMERCIAL_TYPES else "residential"
            if segment and segment != type_segment:
                self.add_error("search_property_type", "Тип объекта не соответствует выбранному сегменту.")
            segment = type_segment
        elif not segment:
            if data.get("search_commercial_purpose"):
                segment = "commercial"
            elif data.get("search_rooms_min") is not None:
                segment = "residential"
        data["search_segment"] = segment
        if segment == "commercial":
            data["search_rooms_min"] = None
        elif segment == "residential":
            data["search_commercial_purpose"] = ""
        data["search_commercial_purpose"] = (data.get("search_commercial_purpose") or "").strip()
        return data

    def clean_budget(self):
        value = self.cleaned_data.get("budget")
        if value is not None and value < 0:
            raise forms.ValidationError("Бюджет не может быть отрицательным.")
        return value


class ClientPropertySuggestionForm(forms.ModelForm):
    class Meta:
        model = ClientPropertySuggestion
        fields = ("property",)

    def __init__(self, *args, owner, client, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["property"].queryset = workspace_queryset(Property.objects, owner).exclude(
            status__in=("archived", "reserved", "sold", "rented"),
        ).exclude(pk__in=client.property_suggestions.values_list("property_id", flat=True)).order_by("title")
        self.fields["property"].widget.attrs["class"] = "form-select"
        mark_invalid_fields(self)


class ClientPropertyReactionForm(forms.ModelForm):
    class Meta:
        model = ClientPropertySuggestion
        fields = ("reaction", "reaction_note")
        widgets = {"reaction_note": forms.TextInput(attrs={"placeholder": "Что сказал клиент?"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        mark_invalid_fields(self)


class ClientCsvImportForm(forms.Form):
    file = forms.FileField(label="Файл CSV", widget=forms.FileInput(attrs={"accept": ".csv,text/csv", "class": "form-control"}))

    def clean_file(self):
        upload = self.cleaned_data["file"]
        if upload.size > 1_000_000:
            raise forms.ValidationError("Файл слишком большой. Максимум — 1 МБ.")
        return upload


def deal_responsible_label(user):
    return staff_display_name(user)


class DealForm(forms.ModelForm):
    class Meta:
        model = Deal
        fields = (
            "client", "property", "lead", "responsible", "stage", "expected_commission", "outcome_note",
        )
        widgets = {
            "lead": forms.HiddenInput(),
            "expected_commission": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
            "outcome_note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner
        self.fields["client"].queryset = workspace_queryset(Client.objects, owner).order_by("name")
        self.fields["property"].queryset = workspace_queryset(Property.objects, owner).order_by("title")
        self.fields["lead"].queryset = workspace_queryset(Lead.objects.filter(client__isnull=False), owner, prefix="property__")
        agency = agency_for(owner)
        if agency and may_manage_agency(owner):
            self.fields["responsible"].queryset = User.objects.filter(
                agency_membership__agency=agency,
            ).select_related("realtor_profile").order_by("username")
            self.fields["responsible"].label_from_instance = deal_responsible_label
            if not self.instance.pk:
                self.fields["responsible"].initial = owner
        else:
            self.fields.pop("responsible")
        if self.instance.pk:
            for field_name in ("client", "property", "lead"):
                self.fields[field_name].disabled = True
        for field in self.fields.values():
            if not isinstance(field.widget, forms.HiddenInput):
                field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        if self.instance.pk:
            for field_name in ("stage", "responsible"):
                if field_name in self.fields:
                    self.fields[field_name].widget.attrs["form"] = "deal-edit-form"
        mark_invalid_fields(self)

    def clean(self):
        cleaned_data = super().clean()
        client = cleaned_data.get("client")
        property = cleaned_data.get("property")
        lead = cleaned_data.get("lead")
        responsible = cleaned_data.get("responsible")
        stage = cleaned_data.get("stage")
        if "responsible" in self.fields and not responsible and "responsible" not in self.errors:
            if self.instance.pk:
                responsible = self.instance.responsible
            elif lead and lead.assigned_to_id and self.fields["responsible"].queryset.filter(pk=lead.assigned_to_id).exists():
                responsible = lead.assigned_to
            else:
                responsible = self.owner
            cleaned_data["responsible"] = responsible
        if not self.instance.pk:
            self.instance.owner = self.owner
            if property:
                self.instance.agency = property.agency
        if client and property and not same_workspace(client, property):
            self.add_error("property", "Клиент и объект должны быть в одном рабочем пространстве.")
        if property and not property.agency_id and responsible and responsible.pk != self.owner.pk:
            self.add_error("responsible", "Личная сделка может быть назначена только вам.")
        if lead and client and property and (lead.client_id != client.pk or lead.property_id != property.pk):
            self.add_error("lead", "Заявка не относится к выбранным клиенту и объекту.")
        if lead and not self.instance.pk:
            if lead.status == "archived" or lead.outcome != "open":
                self.add_error("lead", "Заявка уже завершена или находится в архиве.")
            elif lead.qualification != "qualified" or not lead.first_contacted_at:
                self.add_error("lead", "Сначала зафиксируйте первый контакт и квалифицируйте заявку.")
        if client and property and stage in Deal.ACTIVE_STAGES:
            if Deal.objects.filter(client=client, property=property, stage__in=Deal.ACTIVE_STAGES).exclude(pk=self.instance.pk).exists():
                self.add_error(None, "По этому клиенту и объекту уже есть активная сделка.")
        if stage == "lost" and not cleaned_data.get("outcome_note", "").strip():
            self.add_error("outcome_note", "Укажите причину отказа.")
        return cleaned_data


class ClientInteractionForm(forms.ModelForm):
    class Meta:
        model = ClientInteraction
        fields = ("interaction_type", "text")
        widgets = {"text": forms.Textarea(attrs={"rows": 3, "placeholder": "Что произошло или о чём договорились?"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["interaction_type"].choices = [
            (value, label) for value, label in self.fields["interaction_type"].choices if value != "viewing"
        ]
        self.fields["interaction_type"].widget.attrs["class"] = "form-select"
        self.fields["text"].widget.attrs["class"] = "form-control"


class ClientReminderForm(forms.ModelForm):
    class Meta:
        model = ClientReminder
        fields = ("text", "due_at")
        widgets = {"due_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["due_at"].required = True
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class TaskForm(forms.ModelForm):
    class Meta:
        model = ClientReminder
        fields = ("text", "due_at", "client", "deal", "property")
        widgets = {
            "due_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["due_at"].required = True
        self.fields["client"].queryset = workspace_queryset(Client.objects, owner).order_by("name")
        self.fields["deal"].queryset = workspace_queryset(Deal.objects, owner).filter(stage__in=Deal.ACTIVE_STAGES).select_related("client", "property")
        self.fields["property"].queryset = workspace_queryset(Property.objects, owner).exclude(status="archived").order_by("title")
        if self.instance.pk and self.instance.deal_id:
            self.fields["deal"].queryset = workspace_queryset(Deal.objects, owner).select_related("client", "property")
        if self.instance.pk and self.instance.property_id:
            self.fields["property"].queryset = workspace_queryset(Property.objects, owner).order_by("title")
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        mark_invalid_fields(self)

    def clean(self):
        cleaned_data = super().clean()
        client = cleaned_data.get("client")
        deal = cleaned_data.get("deal")
        property = cleaned_data.get("property")
        if deal and client and deal.client_id != client.pk:
            self.add_error("client", "Клиент должен совпадать с клиентом сделки.")
        if deal and property and deal.property_id != property.pk:
            self.add_error("property", "Объект должен совпадать с объектом сделки.")
        if client and property and not same_workspace(client, property):
            self.add_error("property", "Контакт находится в другом рабочем пространстве.")
        return cleaned_data


class ShowingForm(forms.ModelForm):
    next_action = forms.CharField(max_length=255, required=False, label="Следующее действие")
    next_action_due_at = forms.DateTimeField(
        required=False, label="Срок следующего действия",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )

    class Meta:
        model = Showing
        fields = ("deal", "starts_at", "ends_at", "location", "status", "outcome_note")
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "outcome_note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["deal"].queryset = workspace_queryset(Deal.objects, owner).filter(stage__in=Deal.ACTIVE_STAGES).select_related("client", "property")
        if self.instance.pk and self.instance.deal_id:
            self.fields["deal"].queryset = workspace_queryset(Deal.objects, owner).select_related("client", "property")
        if self.instance.pk:
            self.fields["deal"].disabled = True
            task = self.instance.follow_up_task
            if task and not task.is_done and not task.cancelled_at:
                self.initial.update({
                    "next_action": task.text,
                    "next_action_due_at": timezone.localtime(task.due_at).strftime("%Y-%m-%dT%H:%M") if task.due_at else "",
                })
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        mark_invalid_fields(self)

    def clean(self):
        data = super().clean()
        if data.get("status") == "completed":
            previous_status = Showing.objects.filter(pk=self.instance.pk).values_list("status", flat=True).first() if self.instance.pk else None
            needs_action = (
                previous_status != "completed" or not self.instance.follow_up_task_id
                or bool(self.instance.follow_up_task_id and self.instance.follow_up_task.cancelled_at)
            )
            if needs_action or data.get("next_action") or data.get("next_action_due_at"):
                if not (data.get("next_action") or "").strip():
                    self.add_error("next_action", "Укажите, что сделать после показа.")
                if not data.get("next_action_due_at"):
                    self.add_error("next_action_due_at", "Укажите срок следующего действия.")
                elif data["next_action_due_at"] <= timezone.now() and (
                    not self.instance.follow_up_task_id
                    or data["next_action_due_at"] != self.instance.follow_up_task.due_at
                ):
                    self.add_error("next_action_due_at", "Срок должен быть в будущем.")
        return data


class AIRequestForm(forms.Form):
    MODE_CHOICES = [
        ("package", "Комплект объявления"),
        ("landing", "Комплект для лендинга"),
        ("improve", "Улучшить текст"),
        ("audit", "Проверить готовность"),
    ]
    IMPROVEMENT_CHOICES = [
        ("shorter", "Сделать короче"),
        ("stronger", "Сделать убедительнее"),
        ("premium", "Сделать премиальнее"),
        ("plain", "Убрать канцелярит"),
        ("audience", "Адаптировать для аудитории"),
    ]
    IMPROVEMENT_CONTENT_CHOICES = [
        ("headline", "Заголовок"),
        ("short_description", "Короткое описание"),
        ("full_description", "Полное описание"),
        ("landing_headline", "Заголовок лендинга"),
        ("landing_subtitle", "Подзаголовок лендинга"),
        ("landing_about", "Текст блока «Об объекте»"),
        ("cta", "Призыв к действию"),
    ]
    mode = forms.ChoiceField(label="Сценарий", choices=MODE_CHOICES, initial="package")
    content_type = forms.ChoiceField(
        label="Какой текст улучшить",
        choices=IMPROVEMENT_CONTENT_CHOICES,
        required=False,
        initial="full_description",
    )
    tone = forms.ChoiceField(label="Тон текста", choices=AIContent.TONE_CHOICES, initial="business")
    improvement = forms.ChoiceField(label="Как улучшить", choices=IMPROVEMENT_CHOICES, required=False)
    source_text = forms.CharField(label="Исходный текст или аудитория", required=False, widget=forms.Textarea(attrs={"rows": 4}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
        self.fields["source_text"].label = "Текст для улучшения"
        self.fields["source_text"].widget.attrs["placeholder"] = "Вставьте текст, который нужно улучшить. Для адаптации укажите аудиторию в первой строке."
        mark_invalid_fields(self)

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("mode") == "improve" and not cleaned_data.get("source_text", "").strip():
            self.add_error("source_text", "Вставьте текст, который нужно улучшить.")
        return cleaned_data


class AILeadReplyForm(forms.Form):
    tone = forms.ChoiceField(choices=AIContent.TONE_CHOICES, initial="business")


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
        mark_invalid_fields(self)

def normalize_phone(value):
    phone = re.sub(r"[\s()\-]", "", value.strip())
    if phone.startswith("00"):
        phone = f"+{phone[2:]}"
    if not re.fullmatch(r"\+?\d{7,15}", phone):
        raise ValidationError("Введите корректный номер телефона.")
    return phone


class RegistrationForm(forms.Form):
    phone = forms.CharField(label="Телефон", max_length=30, widget=forms.TelInput(attrs=PHONE_MASK_ATTRS))
    password1 = forms.CharField(label="Пароль", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Повторите пароль", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    terms_accepted = forms.BooleanField(
        required=True,
        label="Принимаю условия сервиса",
        error_messages={"required": "Для создания аккаунта необходимо принять условия сервиса."},
    )
    personal_data_consent = forms.BooleanField(
        required=True,
        label="Даю согласие на обработку персональных данных",
        error_messages={"required": "Для создания аккаунта необходимо согласие на обработку персональных данных."},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-control"
        mark_invalid_fields(self)

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
    username = forms.CharField(label="Телефон", widget=forms.TelInput(attrs={**PHONE_MASK_ATTRS, "autocomplete": "username", "autofocus": True}))

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        mark_invalid_fields(self)

    def clean_username(self):
        return normalize_phone(self.cleaned_data["username"])


class AccountPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        mark_invalid_fields(self)
