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
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .client import (
    MIN_REFUND,
    HalykClient,
    HalykError,
    invoice_number,
    new_secret_hash,
)
from .models import Payment, PaymentStatus

log = logging.getLogger(__name__)


class PaymentError(Exception):
    """This operation on a payment is not allowed."""


class CheckoutError(PaymentError):
    """The learner cannot start this checkout."""


def confirm_with_bank(payment):
    """
    Ask the bank what happened to an invoice, and answer one question: may this
    learner be enrolled?

    Returns ``(verdict, detail, transaction)``:

    - ``True``  — the money is confirmed; the transaction carries the reference
      and card mask worth recording.
    - ``False`` — the bank says this did not happen. ``detail`` says what the
      bank actually reported, because "not confirmed" on its own is useless
      when someone is looking at a learner who insists they paid.
    - ``None``  — no answer yet: a transaction still in flight, or a bank we
      could not reach. Never record this as a failure; the payment may still
      succeed.

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
        return None, f"the bank could not be reached ({exc})", None

    if status.in_progress:
        log.info("Halyk invoice %s still in progress (%s)", payment.invoice_id, status)
        return None, f"still in progress ({status})", status

    if not status.ok:
        detail = f"resultCode {status.result_code} — {status.result_message}"
        log.warning("Halyk status check for invoice %s: %s", payment.invoice_id, detail)
        return False, detail, status

    # Card and bonuses both count: a course settled partly with Halyk loyalty
    # bonuses is still paid for in full as far as the merchant is concerned.
    if status.total is not None and status.total < Decimal(payment.amount):
        detail = f"the bank recorded {status.total} settled, not {payment.amount}"
        log.error("Invoice %s: %s", payment.invoice_id, detail)
        return False, detail, status

    expected_terminal = getattr(settings, "HALYK_TERMINAL_ID", "")
    if status.terminal and expected_terminal and status.terminal != expected_terminal:
        detail = f"terminal {status.terminal} is not ours"
        log.error("Invoice %s: %s", payment.invoice_id, detail)
        return False, detail, status

    if status.status_name not in accepted:
        detail = (f"statusName {status.status_name or '(none)'} does not grant "
                  f"access; accepted: {', '.join(sorted(accepted))}")
        log.warning("Invoice %s: %s", payment.invoice_id, detail)
        return False, detail, status

    return True, "", status


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
def mark_paid_and_enroll(payment_id, payload=None, reference="", card_mask="",
                         transaction_id=""):
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
    if transaction_id:
        payment.transaction_id = transaction_id

    CourseEnrollment.enroll(payment.user, payment.course_id, mode=payment.course_mode)
    payment.enrolled = True
    payment.save()

    log.info(
        "Halyk invoice %s paid, %s enrolled in %s as %s",
        payment.invoice_id, payment.user.id, payment.course_id, payment.course_mode,
    )
    return payment


def transaction_id_for(payment):
    """
    The bank's id for this transaction, which every money operation needs.

    Recorded from the postLink where possible. Payments taken before that was
    stored keep it inside the saved callback, and failing both it can still be
    asked for — so an old payment is never unrefundable just because the column
    is empty.
    """
    if payment.transaction_id:
        return payment.transaction_id

    stored = (payment.callback_payload or {}).get("id")
    if stored:
        return str(stored)

    client = HalykClient()
    token = client.get_api_token()
    status = client.get_payment_status(payment.invoice_id, token["access_token"])
    return status.transaction_id


@transaction.atomic
def cancel_payment(payment_id):
    """
    Release money the bank is only holding on the card.

    Valid solely while the transaction is in AUTH — once it has been charged,
    the way back is a refund. Nobody is unenrolled here, because a payment that
    was only ever held never opened a course.
    """
    payment = Payment.objects.select_for_update().get(pk=payment_id)

    if payment.enrolled:
        raise PaymentError(
            f"Invoice {payment.invoice_id} opened a course; cancelling a hold "
            f"is not the way to undo that — refund it instead."
        )

    client = HalykClient()
    token = client.get_api_token()
    client.cancel_operation(transaction_id_for(payment), token["access_token"])

    payment.status = PaymentStatus.CANCELLED
    payment.failure_reason = "Hold released"
    payment.save()
    log.info("Halyk invoice %s: hold released", payment.invoice_id)
    return payment


@transaction.atomic
def refund_payment(payment_id, amount=None, external_id=None, unenroll=None):
    """
    Give money back on a payment that was actually charged.

    ``amount`` refunds part of it; left out, everything still outstanding. A
    full refund takes the course away again, because otherwise the learner
    keeps what they were given and the money too; a partial one leaves the
    enrolment alone. Pass ``unenroll`` to overrule either.
    """
    from common.djangoapps.student.models import CourseEnrollment

    payment = Payment.objects.select_for_update().get(pk=payment_id)

    if not payment.is_paid:
        raise PaymentError(
            f"Invoice {payment.invoice_id} is {payment.status}; only a paid "
            f"one can be refunded."
        )

    outstanding = payment.amount - payment.refunded_amount
    if outstanding <= 0:
        raise PaymentError(f"Invoice {payment.invoice_id} is already fully refunded.")

    amount = outstanding if amount is None else int(amount)
    if amount > outstanding:
        raise PaymentError(
            f"Only {outstanding} {payment.currency} of invoice "
            f"{payment.invoice_id} is left to refund."
        )
    if amount < MIN_REFUND:
        raise PaymentError(
            f"The bank refuses refunds below {MIN_REFUND} {payment.currency}."
        )

    client = HalykClient()
    token = client.get_api_token()
    client.refund_operation(
        transaction_id_for(payment), token["access_token"],
        # A full refund is sent without an amount, as the bank documents it.
        amount=None if amount == payment.amount else amount,
        external_id=external_id or f"ektu-{payment.invoice_id}",
    )

    payment.refunded_amount += amount
    fully_refunded = payment.refunded_amount >= payment.amount
    if fully_refunded:
        payment.status = PaymentStatus.REFUNDED

    if unenroll is None:
        unenroll = fully_refunded
    if unenroll and payment.enrolled:
        CourseEnrollment.unenroll(payment.user, payment.course_id)
        payment.enrolled = False

    payment.save()
    log.info("Halyk invoice %s: refunded %s of %s%s", payment.invoice_id,
             amount, payment.amount, ", access withdrawn" if unenroll else "")
    return payment


@transaction.atomic
def mark_failed(payment_id, reason="", payload=None, transaction_id=""):
    """Record that an invoice did not result in a payment."""
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if transaction_id:
        # Worth keeping even on a refusal: money held on the card still has to
        # be released, and cancelling it needs this id.
        payment.transaction_id = transaction_id
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
