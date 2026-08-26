"""
The three pages of the payment flow.

- checkout  : shows the bank's widget for one course
- postlink  : the bank tells our server what happened  <- the only thing that grants access
- result    : what the learner sees when the browser comes back

The split matters: the learner's browser is never trusted, so ``result`` only
reports what the server already recorded.
"""
import json
import logging
import secrets
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey

from .client import (
    RETRYABLE_REASON_CODES,
    HalykClient,
    HalykError,
    total_paid,
    truncate_description,
)
from .models import Payment, PaymentStatus
from .services import (
    CheckoutError,
    confirm_with_bank,
    mark_failed,
    mark_paid_and_enroll,
    start_checkout,
)

log = logging.getLogger(__name__)

#: ePay's own language codes, from the payment-object documentation.
LANGUAGES = {"ru": "RUS", "kk": "KAZ", "en": "ENG"}


def _enabled():
    return bool(getattr(settings, "HALYK_ENABLED", False))


def _fake_gateway():
    """The fake gateway is only ever available on a debug deployment."""
    return bool(getattr(settings, "HALYK_FAKE_GATEWAY", False)) and settings.DEBUG


def _absolute(request, path):
    return request.build_absolute_uri(path)


def _language(request):
    code = (getattr(request, "LANGUAGE_CODE", "") or "")[:2].lower()
    return LANGUAGES.get(code, "RUS")


def _description(course_key):
    """
    What the learner will see on their statement.

    Falls back to the course key if the overview is not available; either way
    the bank's length limit is respected, because exceeding it is a hard error
    (reasonCode 3298), not a truncation.
    """
    title = str(course_key)
    try:
        from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
        overview = CourseOverview.get_from_id(course_key)
        if overview and overview.display_name:
            title = overview.display_name
    except Exception:  # pylint: disable=broad-except
        log.debug("No course overview for %s; using the key as description", course_key)
    return truncate_description(title)


def _latin_name(user):
    """
    The cardholder name, which ePay accepts only in Latin script.

    Rather than transliterating a Cyrillic name and getting it subtly wrong, the
    field is simply left out unless the profile name is already Latin — it is
    optional.
    """
    try:
        name = (user.profile.name or "").strip()
    except Exception:  # pylint: disable=broad-except
        return ""
    if name and name.isascii():
        return name[:64]
    return ""


@login_required
def checkout(request, course_id):
    """Start a payment for one course."""
    if not _enabled():
        raise Http404

    try:
        course_key = CourseKey.from_string(course_id)
    except InvalidKeyError as exc:
        raise Http404 from exc

    try:
        payment = start_checkout(request.user, course_key)
    except CheckoutError as exc:
        return render(request, "halyk_payments/result.html", {
            "state": "error",
            "message": str(exc),
            "course_id": course_id,
        }, status=400)

    client = HalykClient()
    result_url = _absolute(request, reverse("halyk_payments:result",
                                            args=[payment.invoice_id]))
    postlink_url = _absolute(request, reverse("halyk_payments:postlink"))

    if _fake_gateway():
        # Everything except the bank: the page offers a button that posts a
        # simulated callback, shaped like a real one, to our own endpoint.
        return render(request, "halyk_payments/checkout.html", {
            "payment": payment,
            "fake": True,
            "terminal": client.terminal,
            "postlink_url": reverse("halyk_payments:postlink"),
            "return_url": reverse("halyk_payments:result", args=[payment.invoice_id]),
        })

    if not client.is_configured():
        log.error("Halyk is enabled but the credentials are missing")
        return render(request, "halyk_payments/result.html", {
            "state": "error",
            "message": "Payments are not configured yet. Please contact support.",
            "course_id": course_id,
        }, status=503)

    try:
        token = client.get_payment_token(
            invoice_id=payment.invoice_id,
            amount=payment.amount,
            currency=payment.currency,
            secret_hash=payment.secret_hash,
        )
    except HalykError:
        mark_failed(payment.pk, reason="Could not obtain a payment token")
        return render(request, "halyk_payments/result.html", {
            "state": "error",
            "message": "The payment service is unavailable. Please try again later.",
            "course_id": course_id,
        }, status=502)

    # The payment object handed to halyk.showPaymentWidget(). Field names and
    # casing are the bank's, not ours; `auth` takes the whole token response.
    payment_object = {
        "invoiceId": payment.invoice_id,
        "backLink": result_url,
        "failureBackLink": result_url,
        "postLink": postlink_url,
        "failurePostLink": postlink_url,
        "language": _language(request),
        "description": _description(course_key),
        "accountId": str(request.user.id),
        "terminal": client.terminal,
        "amount": payment.amount,
        "currency": payment.currency,
        "auth": token,
    }
    name = _latin_name(request.user)
    if name:
        payment_object["name"] = name

    return render(request, "halyk_payments/checkout.html", {
        "payment": payment,
        "fake": False,
        "widget_js_url": client.widget_js_url,
        "widget_origin": client.widget_origin,
        "payment_object_json": json.dumps(payment_object),
    })


@csrf_exempt
@require_POST
def postlink(request):
    """
    The bank's server-to-server notification. This is what grants access.

    Returns 200 for anything it has finished handling, so the bank stops
    retrying; problems are logged rather than surfaced.
    """
    if not _enabled():
        raise Http404

    allowlist = getattr(settings, "HALYK_POSTLINK_IP_ALLOWLIST", []) or []
    if allowlist:
        source = request.META.get("REMOTE_ADDR", "")
        if source not in allowlist:
            log.warning("Rejected a Halyk callback from %s", source)
            return JsonResponse({"status": "rejected"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        payload = request.POST.dict()
    if not isinstance(payload, dict) or not payload:
        return HttpResponseBadRequest("empty payload")

    invoice_id = str(payload.get("invoiceId") or payload.get("invoiceID") or "")
    if not invoice_id:
        return HttpResponseBadRequest("no invoiceId")

    try:
        payment = Payment.objects.get(invoice_id=invoice_id)
    except Payment.DoesNotExist:
        log.warning("Halyk callback for an unknown invoice %s", invoice_id)
        return JsonResponse({"status": "unknown invoice"}, status=404)

    if payment.enrolled:
        return JsonResponse({"status": "already processed"})

    if not _secret_hash_ok(payment, payload):
        return JsonResponse({"status": "rejected"}, status=403)

    # "code" is "ok" on success and "error" otherwise; "reasonCode" is 0 on
    # success and one of the documented error codes otherwise.
    code = str(payload.get("code", "")).strip().lower()
    reason_code = _as_int(payload.get("reasonCode"))

    if code != "ok":
        if reason_code in RETRYABLE_REASON_CODES:
            # Documented as "не финальный": the payment may still complete, so
            # recording a failure here would be wrong. Leave it pending; the
            # learner's result page keeps polling.
            log.info("Halyk invoice %s not final yet (reasonCode %s)",
                     invoice_id, reason_code)
            return JsonResponse({"status": "pending"})
        mark_failed(
            payment.pk,
            reason=str(payload.get("reason", code))[:255],
            payload=payload,
        )
        return JsonResponse({"status": "recorded as failed"})

    # The callback claims success. Check that it is talking about the thing the
    # learner actually bought before believing any of it.
    mismatch = _payload_mismatch(payment, payload)
    if mismatch:
        log.error("Halyk callback for invoice %s does not match: %s",
                  invoice_id, mismatch)
        mark_failed(payment.pk, reason=mismatch, payload=payload)
        return JsonResponse({"status": "rejected"}, status=400)

    if getattr(settings, "HALYK_VERIFY_WITH_STATUS_API", True) and not _fake_gateway():
        verdict, detail, _ = confirm_with_bank(payment)
        if verdict is None:
            # Not decided, or the bank could not be reached. Leaving a real
            # payment pending for a human is always safer than opening a course
            # on an unverified message.
            log.info("Halyk invoice %s left pending: %s", invoice_id, detail)
            return JsonResponse({"status": "pending"})
        if verdict is False:
            mark_failed(payment.pk, reason=f"Bank says: {detail}", payload=payload)
            return JsonResponse({"status": "recorded as failed"})

    mark_paid_and_enroll(
        payment.pk,
        payload=payload,
        reference=str(payload.get("reference", ""))[:128],
        card_mask=str(payload.get("cardMask", ""))[:32],
    )
    return JsonResponse({"status": "ok"})


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _secret_hash_ok(payment, payload):
    """
    Is this callback about a checkout we opened?

    ``secret_hash`` is generated per payment and sent only to the bank, so the
    right value coming back is proof of origin. The documented postLink examples
    do not show the field, so its absence is not treated as forgery — the status
    API check is what catches that case — but a *wrong* value is.
    """
    if not payment.secret_hash:
        return True
    received = str(payload.get("secret_hash") or payload.get("secretHash") or "")
    if not received:
        log.warning(
            "Halyk callback for invoice %s carried no secret_hash; relying on "
            "the status API instead", payment.invoice_id,
        )
        return True
    if not secrets.compare_digest(received, payment.secret_hash):
        log.warning("Rejected a Halyk callback for invoice %s: wrong secret_hash",
                    payment.invoice_id)
        return False
    return True


def _payload_mismatch(payment, payload):
    """Return a description of the first thing that does not match, or ''."""
    expected = Decimal(payment.amount)
    paid = total_paid(payload)
    if payload.get("amount") is not None and paid is None:
        return f"an unreadable amount {payload.get('amount')!r}"
    if paid is not None:
        if paid < expected:
            return f"only {paid} of {expected} was settled"
        if paid > expected:
            # Not a reason to withhold a course from someone who overpaid, but
            # somebody should look at why.
            log.warning("Invoice %s was settled with %s, more than the %s asked",
                        payment.invoice_id, paid, expected)

    currency = str(payload.get("currency", "")).upper()
    if currency and currency != payment.currency.upper():
        return f"currency {currency} instead of {payment.currency}"

    terminal = str(payload.get("terminal", ""))
    expected = getattr(settings, "HALYK_TERMINAL_ID", "")
    if terminal and expected and terminal != expected:
        return "a different terminal"

    return ""


@login_required
def result(request, invoice_id):
    """
    What the learner sees after the bank sends the browser back.

    A paid invoice lands on its receipt rather than being bounced straight into
    the course: the moment after paying is exactly when someone wants proof
    that the money went somewhere, and a silent redirect gives them none.
    """
    payment = _own_payment(request, invoice_id)

    if payment.is_paid and payment.enrolled:
        return _receipt(request, payment)

    state = "pending" if payment.status == PaymentStatus.PENDING else payment.status
    return render(request, "halyk_payments/result.html", {
        "state": state,
        "payment": payment,
        "course_id": str(payment.course_id),
    })


@login_required
def receipt(request, invoice_id):
    """
    The receipt for a payment, at an address the learner can come back to.

    Everything on it was recorded when the bank confirmed the payment, so it
    reflects what actually happened rather than what the browser was told.
    """
    payment = _own_payment(request, invoice_id)
    if not payment.is_paid:
        # Nothing was paid, so there is nothing to show a receipt for.
        return redirect(reverse("halyk_payments:result", args=[payment.invoice_id]))
    return _receipt(request, payment)


def _own_payment(request, invoice_id):
    """This learner's payment, or 404 — never anybody else's."""
    if not _enabled():
        raise Http404
    payment = Payment.objects.filter(
        invoice_id=invoice_id, user=request.user,
    ).first()
    if payment is None:
        raise Http404
    return payment


def _receipt(request, payment):
    course_id = str(payment.course_id)
    course_name = course_id
    try:
        from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
        overview = CourseOverview.get_from_id(payment.course_id)
        if overview and overview.display_name:
            course_name = overview.display_name
    except Exception:  # pylint: disable=broad-except
        pass

    return render(request, "halyk_payments/receipt.html", {
        "payment": payment,
        "course_id": course_id,
        "course_name": course_name,
        "test_mode": bool(getattr(settings, "HALYK_TEST_MODE", True))
                     or _fake_gateway(),
    })
