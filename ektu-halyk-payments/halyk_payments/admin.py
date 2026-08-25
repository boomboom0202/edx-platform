"""Read-mostly admin for support and reconciliation."""
from django.contrib import admin

from .models import Payment


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("invoice_id", "user", "course_id", "amount", "currency",
                    "status", "enrolled", "created")
    list_filter = ("status", "enrolled", "currency", "course_mode")
    search_fields = ("invoice_id", "reference", "user__username", "user__email")
    readonly_fields = ("invoice_id", "user", "course_id", "course_mode", "amount",
                       "currency", "reference", "card_mask", "callback_payload",
                       "paid_at", "created", "modified")
    ordering = ("-created",)

    def has_add_permission(self, request):
        # Payments are only ever created by the checkout flow.
        return False
