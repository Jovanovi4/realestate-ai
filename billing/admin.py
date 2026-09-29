from django.contrib import admin

from .models import (
    AccountInventory, BillingAccount, BillingPeriod, DemoCreditGrant, FileReference,
    Plan, PlanVersion, ResourceReservation, StoredFile, Subscription, UsageEntry, UsageOperation, UsageResult,
)


class NoDeleteAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyBillingAdmin(NoDeleteAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BillingAccount)
class BillingAccountAdmin(ReadOnlyBillingAdmin):
    list_display = ("id", "personal_user", "agency", "payer", "created_at")
    list_select_related = ("personal_user", "agency", "payer")


@admin.register(Plan)
class PlanAdmin(NoDeleteAdmin):
    list_display = ("code", "name")

    def get_readonly_fields(self, request, obj=None):
        return ("code",) if obj else ()


@admin.register(PlanVersion)
class PlanVersionAdmin(NoDeleteAdmin):
    list_display = ("plan", "version", "available", "monthly_price_minor", "currency", "sale_ready")
    list_select_related = ("plan",)
    list_filter = ("plan", "available")

    def get_readonly_fields(self, request, obj=None):
        return tuple(name.removesuffix("_id") for name in PlanVersion.TERMS) + ("created_at",) if obj else ("created_at",)


@admin.register(Subscription)
class SubscriptionAdmin(ReadOnlyBillingAdmin):
    list_display = ("account", "anchor_at", "cancel_at_period_end", "is_suspended")
    list_select_related = ("account",)


@admin.register(BillingPeriod)
class BillingPeriodAdmin(ReadOnlyBillingAdmin):
    list_display = ("subscription", "plan_version", "starts_at", "ends_at", "source", "ai_credits_allocated", "ai_credits_used", "ai_credits_reserved", "voided_at")
    list_filter = ("source", "plan_version")
    list_select_related = ("subscription", "plan_version", "plan_version__plan")


@admin.register(DemoCreditGrant)
class DemoCreditGrantAdmin(ReadOnlyBillingAdmin):
    list_display = ("user", "account", "credits_allocated", "credits_used", "credits_reserved", "created_at")
    list_select_related = ("user", "account")


@admin.register(UsageOperation)
class UsageOperationAdmin(ReadOnlyBillingAdmin):
    list_display = ("id", "account", "action", "ai_action", "status", "planned_credits", "provider", "total_tokens", "started_at")
    list_filter = ("action", "status", "provider")
    list_select_related = ("account",)
    list_per_page = 25


@admin.register(UsageEntry)
class UsageEntryAdmin(ReadOnlyBillingAdmin):
    list_display = ("account", "operation_id", "metric", "delta", "created_at")
    list_filter = ("metric",)
    list_select_related = ("account",)
    list_per_page = 25


@admin.register(ResourceReservation)
class ResourceReservationAdmin(ReadOnlyBillingAdmin):
    list_display = ("id", "account", "action", "amounts", "status", "created_at", "finished_at")
    list_filter = ("action", "status")
    list_select_related = ("account",)
    list_per_page = 25


@admin.register(StoredFile)
class StoredFileAdmin(ReadOnlyBillingAdmin):
    list_display = ("id", "account", "size_bytes", "state", "issue_code", "checked_at")
    list_filter = ("state",)
    list_select_related = ("account",)


@admin.register(FileReference)
class FileReferenceAdmin(ReadOnlyBillingAdmin):
    list_display = ("resource_type", "resource_id", "field_name", "file_id")


@admin.register(AccountInventory)
class AccountInventoryAdmin(ReadOnlyBillingAdmin):
    list_display = ("account", "files_complete", "last_issue_count", "checked_at")
    list_select_related = ("account",)


@admin.register(UsageResult)
class UsageResultAdmin(ReadOnlyBillingAdmin):
    list_display = ("operation_id", "content_id")
