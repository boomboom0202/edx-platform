"""Django admin: API clients and a read-only look at sessions."""
from django.contrib import admin, messages

from .models import ApiClient, Session


@admin.register(ApiClient)
class ApiClientAdmin(admin.ModelAdmin):
    list_display = ("name", "key_prefix", "is_active", "webhook_url", "created")
    readonly_fields = ("key_prefix", "created")
    actions = ["issue_new_key"]

    def save_model(self, request, obj, form, change):
        key = None if change else obj.issue_key()
        super().save_model(request, obj, form, change)
        if key:
            self._show(request, obj, key)

    @admin.action(description="Issue a new API key (the old one stops working)")
    def issue_new_key(self, request, queryset):
        for client in queryset:
            key = client.issue_key()
            client.save()
            self._show(request, client, key)

    def _show(self, request, client, key):
        messages.warning(request, f"API key of {client.name}: {key} — copy it now, it is not shown again.")


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("key", "exam_name", "course_id", "client", "status", "verdict", "created")
    list_filter = ("status", "verdict", "client")
    search_fields = ("key", "external_ref", "course_id", "exam_name")
    raw_id_fields = ("user", "reviewed_by")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
