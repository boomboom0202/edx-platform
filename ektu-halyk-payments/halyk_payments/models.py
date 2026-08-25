"""
Payment records for the Halyk ePay integration.

One row per checkout attempt. The row is the single source of truth for what
the learner is buying: the amount and the course mode are copied here from the
CourseMode at checkout time and are never taken from the browser, so a tampered
request cannot change the price or the mode that gets granted.
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from opaque_keys.edx.django.models import CourseKeyField


class PaymentStatus(models.TextChoices):
    """Lifecycle of a single checkout attempt."""

    PENDING = "pending", _("Pending")
    PAID = "paid", _("Paid")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")
    REFUNDED = "refunded", _("Refunded")


class Payment(models.Model):
    """A single attempt to pay for access to a course."""

    # Our own identifier, sent to the bank as invoiceId and echoed back in the
    # callback. Generated server side; never accepted from a request.
    invoice_id = models.CharField(max_length=64, unique=True, db_index=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="halyk_payments",
    )
    course_id = CourseKeyField(max_length=255, db_index=True)
    course_mode = models.CharField(
        max_length=100,
        help_text=_("The CourseMode slug the learner is buying, e.g. 'verified'."),
    )

    # Copied from CourseMode at checkout. Minor units are avoided: Halyk works
    # in whole tenge and CourseMode.min_price is already an integer.
    amount = models.PositiveIntegerField()
    currency = models.CharField(max_length=8, default="KZT")

    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING, db_index=True,
    )

    # Whatever the bank gives us back, kept for support and reconciliation.
    reference = models.CharField(max_length=128, blank=True, default="")
    card_mask = models.CharField(max_length=32, blank=True, default="")
    failure_reason = models.CharField(max_length=255, blank=True, default="")
    callback_payload = models.JSONField(blank=True, null=True)

    # Set once the learner has actually been given access, so a repeated
    # callback cannot enroll twice.
    enrolled = models.BooleanField(default=False)

    paid_at = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Halyk payment")
        verbose_name_plural = _("Halyk payments")
        ordering = ("-created",)
        indexes = [models.Index(fields=["user", "course_id", "status"])]

    def __str__(self):
        return f"{self.invoice_id} {self.amount} {self.currency} [{self.status}]"

    @property
    def is_paid(self):
        return self.status == PaymentStatus.PAID
