from django.contrib import admin

from .models import Lead, Property, RealtorProfile


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
