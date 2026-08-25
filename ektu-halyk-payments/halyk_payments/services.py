"""
The rules that decide who gets access to a course.

Two invariants are enforced here and nowhere else:

1. The price and the course mode come from the CourseMode row, never from the
   request. A learner cannot ask to pay one tenge for a course.
2. Access is granted exactly once per invoice, from the server-to-server
   callback only. A browser redirect never enrolls anybody, and a repeated
   callback does not enroll twice.
"""
import logging

from django.db import transaction
from django.utils import timezone

from decimal import Decimal

from .client import HalykClient, HalykError, invoice_number, new_secret_hash
from .models import Payment, PaymentStatus

log = logging.getLogger(__name__)


class CheckoutError(Exception):
    """The learner cannot start this checkout."""


def confirm_with_bank(payment):
    """
    Ask the bank what happened to an invoice, and answer one question: may this
    learner be enrolled?

    Returns the transaction when the money is confirmed (truthy, and it carries
    the reference and card mask worth recording), False when the bank says the
    payment did not happen, and None when there is no answer yet — a
    transaction still in flight, or a bank we could not reach. None must never
    be recorded as a failure: the payment may still succeed.

    This is the only thing that can turn a pending payment into access, whether
    it is called from the bank's callback or from the reconcile command.
    """
    from django.conf import settings

    client = HalykClient()
    accepted = {
        str(name).upper()
        for name in getattr(settings, "HALYK_ACCEPTED_STATUSES", ["CHARGE"])
    }

    try:
        token = client.get_api_token()
        status = client.get_payment_status(payment.invoice_id, token["access_token"])
    except (HalykError, KeyError) as exc:
        log.error("Could not reach the bank about invoice %s: %s",
                  payment.invoice_id, exc)
        return None

    if status.in_progress:
        log.info("Halyk invoice %s still in progress (%s)", payment.invoice_id, status)
        return None

    if not status.ok:
        log.warning("Halyk status check for invoice %s: %s (%s)",
                    payment.invoice_id, status, status.result_message)
        return False

    if status.amount is not None and status.amount != Decimal(payment.amount):
        log.error("Invoice %s was paid for %s instead of %s",
                  payment.invoice_id, status.amount, payment.amount)
        return False

    expected_terminal = getattr(settings, "HALYK_TERMINAL_ID", "")
    if status.terminal and expected_terminal and status.terminal != expected_terminal:
        log.error("Invoice %s belongs to another terminal", payment.invoice_id)
        return False

    if status.status_name not in accepted:
        log.warning("Invoice %s is %s, which does not grant access",
                    payment.invoice_id, status.status_name)
        return False

    return status


def get_paid_mode(course_key):
    """
    The mode being sold for this course, or None if the course is not for sale.

    Imported lazily so the module can be imported outside a running LMS (tests,
    management commands).
    """
    from common.djangoapps.course_modes.models import CourseMode
    from django.conf import settings

    slug = getattr(settings, "HALYK_COURSE_MODE", "verified")
    mode = CourseMode.objects.filter(
        course_id=course_key, mode_slug=slug, min_price__gt=0,
    ).first()
    return mode


def already_enrolled_in_paid_mode(user, course_key):
    from common.djangoapps.student.models import CourseEnrollment
    from django.conf import settings

    slug = getattr(settings, "HALYK_COURSE_MODE", "verified")
    enrollment_mode, is_active = CourseEnrollment.enrollment_mode_for_user(user, course_key)
    return bool(is_active and enrollment_mode == slug)


def start_checkout(user, course_key):
    """
    Create a pending payment for this learner and course.

    The amount is read from the CourseMode here, which is the only place it is
    ever decided.
    """
    from django.conf import settings

    mode = get_paid_mode(course_key)
    if mode is None:
        raise CheckoutError("This course is not for sale.")

    if already_enrolled_in_paid_mode(user, course_key):
        raise CheckoutError("You already have access to this course.")

    currency = (mode.currency or settings.HALYK_CURRENCY).upper()
    if currency != settings.HALYK_CURRENCY.upper():
        # Halyk settles in tenge; refusing is safer than silently charging a
        # tenge amount for a price that was entered in another currency.
        raise CheckoutError(
            f"This course is priced in {currency}, but payments are taken in "
            f"{settings.HALYK_CURRENCY.upper()}."
        )

    amount = int(mode.min_price)

    # Reloading the checkout page should not open a new invoice at the bank
    # every time. Reuse the learner's outstanding one as long as it is still
    # for the same thing at the same price.
    existing = Payment.objects.filter(
        user=user, course_id=course_key, status=PaymentStatus.PENDING,
        course_mode=mode.mode_slug, amount=amount, currency=currency,
    ).exclude(invoice_id=None).order_by("-created").first()
    if existing is not None:
        return existing

    with transaction.atomic():
        payment = Payment.objects.create(
            user=user,
            course_id=course_key,
            course_mode=mode.mode_slug,
            amount=amount,
            currency=currency,
            secret_hash=new_secret_hash(),
        )
        # The bank's invoice number has to be unique on its last six digits, so
        # it comes from the primary key rather than from randomness. The key
        # only exists after the insert, hence the second write.
        payment.invoice_id = invoice_number(payment.pk)
        payment.save(update_fields=["invoice_id", "modified"])

    return payment


@transaction.atomic
def mark_paid_and_enroll(payment_id, payload=None, reference="", card_mask=""):
    """
    Record a confirmed payment and give the learner access.

    Safe to call more than once for the same invoice: the row is locked and the
    enrolment only happens on the transition into the paid state.
    """
    from common.djangoapps.student.models import CourseEnrollment

    payment = Payment.objects.select_for_update().get(pk=payment_id)

    if payment.enrolled:
        log.info("Halyk invoice %s already granted, ignoring", payment.invoice_id)
        return payment

    payment.status = PaymentStatus.PAID
    payment.paid_at = payment.paid_at or timezone.now()
    if payload is not None:
        payment.callback_payload = payload
    if reference:
        payment.reference = reference
    if card_mask:
        payment.card_mask = card_mask

    CourseEnrollment.enroll(payment.user, payment.course_id, mode=payment.course_mode)
    payment.enrolled = True
    payment.save()

    log.info(
        "Halyk invoice %s paid, %s enrolled in %s as %s",
        payment.invoice_id, payment.user.id, payment.course_id, payment.course_mode,
    )
    return payment


@transaction.atomic
def mark_failed(payment_id, reason="", payload=None):
    """Record that an invoice did not result in a payment."""
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.enrolled:
        # A late failure notice must never revoke access that was already
        # granted against a confirmed payment; that is a refund, handled by hand.
        log.warning(
            "Halyk invoice %s reported as failed after access was granted",
            payment.invoice_id,
        )
        return payment
    payment.status = PaymentStatus.FAILED
    payment.failure_reason = (reason or "")[:255]
    if payload is not None:
        payment.callback_payload = payload
    payment.save()
    return payment
