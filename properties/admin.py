from django.contrib import admin

from .models import AIContent, Agency, AgencyInvitation, AgencyMembership, AuditEvent, Client, ClientInteraction, ClientReminder, Deal, Lead, Property, RealtorProfile, Showing, UserLegalAcceptance


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
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
class LeadAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "contact_purpose", "client", "property", "assigned_to", "status", "personal_data_consent_at", "created_at")
    search_fields = ("name", "phone", "property__title")
    list_filter = ("contact_purpose", "status")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "owner", "agency", "status", "source", "created_at")
    search_fields = ("name", "phone", "preferred_area")
    list_filter = ("status", "source")


@admin.register(ClientInteraction)
class ClientInteractionAdmin(admin.ModelAdmin):
    list_display = ("client", "interaction_type", "created_at")
    list_filter = ("interaction_type",)


@admin.register(ClientReminder)
class ClientReminderAdmin(admin.ModelAdmin):
    list_display = ("text", "owner", "agency", "client", "deal", "due_at", "is_done")
    list_filter = ("is_done",)


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ("client", "property", "agency", "stage", "responsible", "expected_commission")
    list_filter = ("stage",)
    search_fields = ("client__name", "client__phone", "property__title")


@admin.register(Showing)
class ShowingAdmin(admin.ModelAdmin):
    list_display = ("deal", "starts_at", "status", "owner", "agency")
    list_filter = ("status",)
    search_fields = ("deal__client__name", "deal__property__title")


@admin.register(RealtorProfile)
class RealtorProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name", "phone")


@admin.register(AIContent)
class AIContentAdmin(admin.ModelAdmin):
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


@admin.register(AgencyMembership)
class AgencyMembershipAdmin(admin.ModelAdmin):
    list_display = ("agency", "user", "role", "joined_at")
    list_filter = ("role",)


@admin.register(AgencyInvitation)
class AgencyInvitationAdmin(admin.ModelAdmin):
    list_display = ("agency", "role", "created_by", "expires_at", "accepted_by")


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
