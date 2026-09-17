from django.contrib import admin

from .models import AIContent, Lead, Property, RealtorProfile


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
    list_display = ("name", "phone", "property", "status", "created_at")
    search_fields = ("name", "phone", "property__title")
    list_filter = ("status",)


@admin.register(RealtorProfile)
class RealtorProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name", "phone")


@admin.register(AIContent)
class AIContentAdmin(admin.ModelAdmin):
    list_display = ("property", "content_type", "tone", "provider", "model", "is_applied", "created_at")
    list_filter = ("content_type", "tone", "provider", "is_applied")
    search_fields = ("property__title", "content")
