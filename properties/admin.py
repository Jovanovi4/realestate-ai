from django.contrib import admin

from .models import AIContent, Client, ClientInteraction, ClientReminder, Lead, Property, RealtorProfile, UserLegalAcceptance


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "owner",
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
    list_display = ("name", "phone", "contact_purpose", "client", "property", "status", "personal_data_consent_at", "created_at")
    search_fields = ("name", "phone", "property__title")
    list_filter = ("contact_purpose", "status")


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "owner", "status", "source", "created_at")
    search_fields = ("name", "phone", "preferred_area")
    list_filter = ("status", "source")


@admin.register(ClientInteraction)
class ClientInteractionAdmin(admin.ModelAdmin):
    list_display = ("client", "interaction_type", "created_at")
    list_filter = ("interaction_type",)


@admin.register(ClientReminder)
class ClientReminderAdmin(admin.ModelAdmin):
    list_display = ("client", "text", "due_at", "is_done")
    list_filter = ("is_done",)


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
