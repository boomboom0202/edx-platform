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
from decimal import Decimal, InvalidOperation

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
    truncate_description,
)
from .models import Payment, PaymentStatus
from .services import CheckoutError, mark_failed, mark_paid_and_enroll, start_checkout

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
        confirmed = _confirm_with_bank(payment)
        if confirmed is None:
            # Not decided, or the bank could not be reached. Leaving a real
            # payment pending for a human is always safer than opening a course
            # on an unverified message.
            return JsonResponse({"status": "pending"})
        if not confirmed:
            mark_failed(payment.pk, reason="Not confirmed by the status API",
                        payload=payload)
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
    amount = payload.get("amount")
    if amount is not None:
        try:
            paid = Decimal(str(amount))
        except InvalidOperation:
            return f"an unreadable amount {amount!r}"
        if paid != Decimal(payment.amount):
            return f"amount {amount} instead of {payment.amount}"

    currency = str(payload.get("currency", "")).upper()
    if currency and currency != payment.currency.upper():
        return f"currency {currency} instead of {payment.currency}"

    terminal = str(payload.get("terminal", ""))
    expected = getattr(settings, "HALYK_TERMINAL_ID", "")
    if terminal and expected and terminal != expected:
        return "a different terminal"

    return ""


def _confirm_with_bank(payment):
    """
    Ask the bank directly, so a forged callback cannot enroll anybody.

    Returns True when the money is confirmed, False when the bank says the
    payment did not happen, and None when there is no answer yet — a
    transaction still in flight, or a bank we could not reach. None must not be
    recorded as a failure: the payment may still succeed.
    """
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

    return True


@login_required
def result(request, invoice_id):
    """What the learner sees after the bank sends the browser back."""
    if not _enabled():
        raise Http404

    payment = Payment.objects.filter(
        invoice_id=invoice_id, user=request.user,
    ).first()
    if payment is None:
        raise Http404

    if payment.is_paid and payment.enrolled:
        return redirect(f"/courses/{payment.course_id}/course/")

    state = "pending" if payment.status == PaymentStatus.PENDING else payment.status
    return render(request, "halyk_payments/result.html", {
        "state": state,
        "payment": payment,
        "course_id": str(payment.course_id),
    })
