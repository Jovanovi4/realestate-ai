from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from billing.enforcement import enabled as billing_enforcement_enabled
from .forms import SafeImageField

from .models import AIContent, Agency, AgencyInvitation, AgencyMembership, AuditEvent, Client, ClientInteraction, ClientPropertySuggestion, ClientReminder, Deal, Lead, OutboundMessage, Property, RealtorProfile, Showing, TelegramDelivery, TelegramPublication, TelegramSubscriber, UserLegalAcceptance


class BillingReadOnlyWhenEnforced:
    """Admin may inspect CRM, but quota and access writes use cabinet workflows."""

    def has_add_permission(self, request):
        return not billing_enforcement_enabled() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return not billing_enforcement_enabled() and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not billing_enforcement_enabled() and super().has_delete_permission(request, obj)


class TelegramReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OutboundMessage)
class OutboundMessageAdmin(TelegramReadOnlyAdmin):
    list_display = ("id", "channel", "status", "attempts", "created_at", "next_attempt_at", "last_error", "sent_at")
    list_filter = ("channel", "status")
    search_fields = ("dedupe_key",)
    readonly_fields = ("dedupe_key", "channel", "payload", "status", "attempts", "next_attempt_at",
                       "lease_until", "expires_at", "last_error", "created_at", "sent_at")


@admin.register(TelegramSubscriber)
class TelegramSubscriberAdmin(TelegramReadOnlyAdmin):
    list_display = ("feed", "chat_id", "display_name", "active", "created_at")
    list_filter = ("active",)
    search_fields = ("chat_id", "display_name")


@admin.register(TelegramPublication)
class TelegramPublicationAdmin(TelegramReadOnlyAdmin):
    list_display = ("property", "feed", "created_at")
    search_fields = ("property__title",)


@admin.register(TelegramDelivery)
class TelegramDeliveryAdmin(TelegramReadOnlyAdmin):
    list_display = ("publication", "subscriber", "sent_at", "attempts", "last_error")
    list_filter = ("sent_at",)


@admin.register(Property)
class PropertyAdmin(BillingReadOnlyWhenEnforced, admin.ModelAdmin):
    actions = ("archive_selected", "restore_selected")
    readonly_fields = ("archived_at", "status_before_archive")

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Перенести выбранные объекты в архив", permissions=["change"])
    def archive_selected(self, request, queryset):
        with transaction.atomic():
            for property in queryset.select_for_update().exclude(status="archived"):
                property.status = "archived"
                property.save(update_fields=["status", "updated_at"])

    @admin.action(description="Восстановить объекты без перепубликации", permissions=["change"])
    def restore_selected(self, request, queryset):
        if billing_enforcement_enabled():
            self.message_user(request, "Восстановите объекты через кабинет: там проверяется свободное место тарифа.", level="error")
            return
        with transaction.atomic():
            for property in queryset.select_for_update().filter(status="archived"):
                previous = property.status_before_archive
                property.status = previous if previous in dict(Property.STATUS_CHOICES) and previous != "archived" else "draft"
                property.save(update_fields=["status", "updated_at"])

    def save_model(self, request, obj, form, change):
        if billing_enforcement_enabled() and change:
            old = Property.objects.get(pk=obj.pk)
            if ((old.status == "archived" and obj.status != "archived") or
                    (not old.landing_published and obj.landing_published and obj.status != "archived") or
                    bool(request.FILES.get("landing_logo"))):
                raise PermissionDenied("Квотируемые изменения объекта выполняйте через кабинет.")
        super().save_model(request, obj, form, change)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "landing_logo":
            kwargs.update(form_class=SafeImageField, purpose="logo")
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    list_display = (
        "title",
        "owner",
        "agency",
        "property_type",
        "status",
        "price",
        "currency",
        "created_at",
    )

    search_fields = (
        "title",
        "address",
    )

    list_filter = (
        "property_type",
        "status",
        "currency",
    )


@admin.register(Lead)
class LeadAdmin(BillingReadOnlyWhenEnforced, admin.ModelAdmin):
    readonly_fields = ("archived_at", "outcome_at")

    def has_delete_permission(self, request, obj=None):
        return False

    list_display = ("name", "phone", "source", "assigned_to", "qualification", "outcome", "first_contacted_at", "next_action_due_at", "status", "created_at")
    search_fields = ("name", "phone", "property__title")
    list_filter = ("source", "qualification", "outcome", "contact_purpose", "status")


@admin.register(Client)
class ClientAdmin(BillingReadOnlyWhenEnforced, admin.ModelAdmin):
    readonly_fields = ("legacy_status",)
    list_display = ("name", "phone", "owner", "agency", "status", "source", "created_at")
    search_fields = ("name", "phone", "preferred_area")
    list_filter = ("status", "source")


@admin.register(ClientInteraction)
class ClientInteractionAdmin(BillingReadOnlyWhenEnforced, admin.ModelAdmin):
    list_display = ("client", "interaction_type", "created_at")
    list_filter = ("interaction_type",)


@admin.register(ClientPropertySuggestion)
class ClientPropertySuggestionAdmin(BillingReadOnlyWhenEnforced, admin.ModelAdmin):
    list_display = ("client", "property", "reaction", "sent_at", "reacted_at")
    list_filter = ("reaction",)
    search_fields = ("client__name", "property__title")


@admin.register(ClientReminder)
class ClientReminderAdmin(BillingReadOnlyWhenEnforced, admin.ModelAdmin):
    actions = None
    exclude = ("lead",)
    list_display = ("text", "owner", "agency", "client", "deal", "lead", "due_at", "is_done", "cancelled_at")
    list_filter = ("is_done",)

    def has_change_permission(self, request, obj=None):
        return not (obj and obj.lead_id) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not (obj and obj.lead_id) and super().has_delete_permission(request, obj)


@admin.register(Deal)
class DealAdmin(BillingReadOnlyWhenEnforced, admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        return False

    list_display = ("client", "property", "agency", "stage", "responsible", "expected_commission")
    list_filter = ("stage",)
    search_fields = ("client__name", "client__phone", "property__title")


@admin.register(Showing)
class ShowingAdmin(BillingReadOnlyWhenEnforced, admin.ModelAdmin):
    list_display = ("deal", "starts_at", "status", "owner", "agency")
    list_filter = ("status",)
    search_fields = ("deal__client__name", "deal__property__title")


@admin.register(RealtorProfile)
class RealtorProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name", "phone")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "photo":
            kwargs.update(form_class=SafeImageField, purpose="profile")
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(AIContent)
class AIContentAdmin(BillingReadOnlyWhenEnforced, admin.ModelAdmin):
    list_display = ("property", "content_type", "tone", "provider", "model", "is_applied", "created_at")
    list_filter = ("content_type", "tone", "provider", "is_applied")
    search_fields = ("property__title", "content")


@admin.register(UserLegalAcceptance)
class UserLegalAcceptanceAdmin(admin.ModelAdmin):
    list_display = ("user", "terms_version", "terms_accepted_at", "personal_data_consent_version", "personal_data_consent_at")
    search_fields = ("user__username",)


@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "created_at")
    search_fields = ("name", "owner__username")

    def has_add_permission(self, request):
        return not billing_enforcement_enabled() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return not billing_enforcement_enabled() and super().has_change_permission(request, obj)


@admin.register(AgencyMembership)
class AgencyMembershipAdmin(admin.ModelAdmin):
    list_display = ("agency", "user", "role", "joined_at")
    list_filter = ("role",)

    def has_add_permission(self, request):
        return not billing_enforcement_enabled() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return not billing_enforcement_enabled() and super().has_change_permission(request, obj)


@admin.register(AgencyInvitation)
class AgencyInvitationAdmin(admin.ModelAdmin):
    list_display = ("agency", "role", "created_by", "expires_at", "accepted_by")

    def has_add_permission(self, request):
        return not billing_enforcement_enabled() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return not billing_enforcement_enabled() and super().has_change_permission(request, obj)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "agency", "actor", "model_name", "object_pk", "action")
    list_filter = ("action", "model_name")
    readonly_fields = ("created_at", "agency", "owner", "actor", "model_name", "object_pk", "action", "changed_fields")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
