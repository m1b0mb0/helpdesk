from django.contrib import admin

from .models import SupportRequest


@admin.register(SupportRequest)
class SupportRequestAdmin(admin.ModelAdmin):
    list_display = ("event_id", "ticket_id", "status", "attempt_count", "dispatch_count", "updated_at")
    list_filter = ("status",)
    search_fields = ("event_id", "ticket_id")
    readonly_fields = tuple(field.name for field in SupportRequest._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False
