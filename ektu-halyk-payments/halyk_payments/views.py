"""
The pages of the payment flow.

- checkout        : shows what is being bought, straight away
- payment_object  : the bank's token and the widget's settings, fetched by the
                    checkout page in the background
- postlink        : the bank tells our server what happened  <- the only thing
                    that grants access
- result          : what the learner sees when the browser comes back

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
    already_enrolled_in_paid_mode,
    confirm_with_bank,
    mark_failed,
    mark_paid_and_enroll,
    restore_paid_access,
    start_checkout,
)

log = logging.getLogger(__name__)

#: ePay's own language codes, from the payment-object documentation.
LANGUAGES = {"ru": "RUS", "kk": "KAZ", "en": "ENG"}

#: How a callback relates to the checkout it names. See ``_signature``.
SIGNED, UNSIGNED, FORGED = "signed", "unsigned", "forged"


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


def _course_home(course_key):
    return f"/courses/{course_key}/course/"


def _course_name(course_key):
    """The course's display name, or its key when there is no overview."""
    try:
        from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
        overview = CourseOverview.get_from_id(course_key)
        if overview and overview.display_name:
            return overview.display_name
    except Exception:  # pylint: disable=broad-except
        log.debug("No course overview for %s; using the key", course_key)
    return str(course_key)


def _description(course_key):
    """
    What the learner will see on their statement.

    The bank's length limit is respected, because exceeding it is a hard error
    (reasonCode 3298), not a truncation.
    """
    return truncate_description(_course_name(course_key))


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


def _error(request, message, course_id, status):
    return render(request, "halyk_payments/result.html", {
        "state": "error",
        "message": message,
        "course_id": course_id,
    }, status=status)


@login_required
def checkout(request, course_id):
    """
    Get the learner into a course we sell, taking money only if they have not
    paid for it already.

    Someone who bought the course and left it comes straight back in: the
    purchase stands until it is refunded, so there is nothing to pay twice.

    The page renders at once. The bank's token — a round trip to the bank's
    OAuth server, and the slow part — is fetched by the page in the background
    (``payment_object``), so the learner reads the summary while it arrives
    instead of looking at a blank tab.
    """
    if not _enabled():
        raise Http404

    try:
        course_key = CourseKey.from_string(course_id)
    except InvalidKeyError as exc:
        raise Http404 from exc

    if already_enrolled_in_paid_mode(request.user, course_key):
        return redirect(_course_home(course_key))
    if restore_paid_access(request.user, course_key) is not None:
        return redirect(_course_home(course_key))

    try:
        payment = start_checkout(request.user, course_key)
    except CheckoutError as exc:
        return _error(request, str(exc), course_id, status=400)

    client = HalykClient()
    context = {
        "payment": payment,
        "course_name": _course_name(course_key),
        "fake": False,
    }

    if _fake_gateway():
        # Everything except the bank: the page offers a button that posts a
        # simulated callback, shaped like a real one, to our own endpoint.
        context.update({
            "fake": True,
            "terminal": client.terminal,
            "postlink_url": reverse("halyk_payments:postlink"),
            "return_url": reverse("halyk_payments:result", args=[payment.invoice_id]),
        })
        return render(request, "halyk_payments/checkout.html", context)

    if not client.is_configured():
        log.error("Halyk is enabled but the credentials are missing")
        return _error(request, "Payments are not configured yet. Please contact support.",
                      course_id, status=503)

    context.update({
        "widget_js_url": client.widget_js_url,
        "widget_origin": client.widget_origin,
        "payment_object_url": reverse("halyk_payments:payment_object",
                                      args=[payment.invoice_id]),
    })
    return render(request, "halyk_payments/checkout.html", context)


@login_required
@require_POST
def payment_object(request, invoice_id):
    """
    The object handed to ``halyk.showPaymentWidget()``, token included.

    A POST, and so behind the platform's CSRF check: it asks the bank for a
    token, and a page elsewhere must not be able to make a learner's browser
    do that. It goes back as JSON and is never written into the HTML, so
    nothing in it — a course title, a learner's name — can be read as markup.
    """
    payment = _own_payment(request, invoice_id)
    if payment.status != PaymentStatus.PENDING:
        return JsonResponse({
            "error": "This payment is no longer open.",
            "result": reverse("halyk_payments:result", args=[payment.invoice_id]),
        }, status=409)

    client = HalykClient()
    if not client.is_configured():
        log.error("Halyk is enabled but the credentials are missing")
        return JsonResponse({"error": "Payments are not configured yet."}, status=503)

    try:
        token = client.get_payment_token(
            invoice_id=payment.invoice_id,
            amount=payment.amount,
            currency=payment.currency,
            secret_hash=payment.secret_hash,
        )
    except HalykError:
        # The invoice stays open: nothing was paid, and the page can simply ask
        # again. Writing it off would only put a failed order into the
        # learner's history because the bank was slow to answer.
        return JsonResponse({
            "error": "The payment service is unavailable. Please try again later.",
        }, status=502)

    result_url = _absolute(request, reverse("halyk_payments:result",
                                            args=[payment.invoice_id]))
    postlink_url = _absolute(request, reverse("halyk_payments:postlink"))

    # Field names and casing are the bank's, not ours; `auth` takes the whole
    # token response.
    obj = {
        "invoiceId": payment.invoice_id,
        "backLink": result_url,
        "failureBackLink": result_url,
        "postLink": postlink_url,
        "failurePostLink": postlink_url,
        "language": _language(request),
        "description": _description(payment.course_id),
        "accountId": str(request.user.id),
        "terminal": client.terminal,
        "amount": payment.amount,
        "currency": payment.currency,
        "auth": token,
    }
    name = _latin_name(request.user)
    if name:
        obj["name"] = name
    return JsonResponse(obj)


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
        source = _client_ip(request)
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

    signature = _signature(payment, payload)
    if signature == FORGED:
        return JsonResponse({"status": "rejected"}, status=403)

    if signature == UNSIGNED:
        # An unsigned message proves nothing, in either direction. Invoice
        # numbers run in sequence, so anyone could post "code: error" for
        # somebody else's invoice and knock their checkout over while they are
        # paying — and they would then pay a second time. The callback is only
        # a prompt: the bank's own answer decides, whatever it claimed.
        log.info("Halyk callback for invoice %s is unsigned; asking the bank",
                 invoice_id)
        return _settle_with_bank(payment)

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
            transaction_id=str(payload.get("id", ""))[:64],
        )
        return JsonResponse({"status": "recorded as failed"})

    # The callback claims success. Check that it is talking about the thing the
    # learner actually bought before believing any of it.
    mismatch = _payload_mismatch(payment, payload)
    if mismatch:
        log.error("Halyk callback for invoice %s does not match: %s",
                  invoice_id, mismatch)
        mark_failed(payment.pk, reason=mismatch, payload=payload,
                    transaction_id=str(payload.get("id", ""))[:64])
        return JsonResponse({"status": "rejected"}, status=400)

    if getattr(settings, "HALYK_VERIFY_WITH_STATUS_API", True) and not _fake_gateway():
        return _settle_with_bank(payment, signed_payload=payload)

    mark_paid_and_enroll(
        payment.pk,
        payload=payload,
        reference=str(payload.get("reference", ""))[:128],
        card_mask=str(payload.get("cardMask", ""))[:32],
        transaction_id=str(payload.get("id", ""))[:64],
    )
    return JsonResponse({"status": "ok"})


def _settle_with_bank(payment, signed_payload=None):
    """
    Let the bank's status API decide, and record what it says.

    The values that matter later — the transaction id every refund is
    addressed by, the reference support quotes — are taken from the bank's
    answer rather than from the callback, which is only a claim about it. A
    signed callback is still what gets stored, for support to read; an
    unsigned one is not kept at all.
    """
    verdict, detail, status = confirm_with_bank(payment)

    if verdict is None:
        # Not decided, or the bank could not be reached. Leaving a real
        # payment pending for a human is always safer than opening a course
        # on an unverified message, or writing off one that may have gone
        # through.
        log.info("Halyk invoice %s left pending: %s", payment.invoice_id, detail)
        return JsonResponse({"status": "pending"})

    claimed = signed_payload or {}
    record = signed_payload if signed_payload is not None else status.body
    transaction_id = (status.transaction_id or str(claimed.get("id", "")))[:64]

    if verdict is False:
        mark_failed(payment.pk, reason=f"Bank says: {detail}", payload=record,
                    transaction_id=transaction_id)
        return JsonResponse({"status": "recorded as failed"})

    mark_paid_and_enroll(
        payment.pk,
        payload=record,
        reference=(status.reference or str(claimed.get("reference", "")))[:128],
        card_mask=(status.card_mask or str(claimed.get("cardMask", "")))[:32],
        transaction_id=transaction_id,
    )
    return JsonResponse({"status": "ok"})


def _client_ip(request):
    """
    The address the callback really came from.

    Behind Tutor's Caddy, REMOTE_ADDR is the proxy's own container address, so
    an allowlist compared against it would either shut the bank out or let
    everybody in. The platform already reads the real address through its
    proxies — it rate-limits logins with it — so the same helper is used here.
    """
    try:
        from edx_django_utils import ip
    except ImportError:  # pragma: no cover - always present inside the LMS
        return request.META.get("REMOTE_ADDR", "")
    return ip.get_safest_client_ip(request)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _signature(payment, payload):
    """
    Whether this callback proves it is about a checkout we opened.

    ``secret_hash`` is generated per payment and sent only to the bank, so the
    right value coming back is proof of origin and a wrong one is a forgery.
    The documented postLink examples do not show the field, so a callback may
    arrive without it; such a callback is not rejected, but nothing in it is
    believed either — see ``postlink``.
    """
    if not payment.secret_hash:
        # Payments created before secret_hash existed cannot be signed.
        return UNSIGNED
    received = str(payload.get("secret_hash") or payload.get("secretHash") or "")
    if not received:
        return UNSIGNED
    if not secrets.compare_digest(received, payment.secret_hash):
        log.warning("Rejected a Halyk callback for invoice %s: wrong secret_hash",
                    payment.invoice_id)
        return FORGED
    return SIGNED


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
        "course_name": _course_name(payment.course_id),
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


@login_required
def orders(request):
    """
    Everything this learner has bought, and what became of it.

    Abandoned checkouts are left out — opening the payment page and closing it
    is not an order, and a list full of them would bury the real ones. A
    pending payment the bank did tell us about stays, because money may have
    moved and the learner needs to see that it is being sorted out.
    """
    if not _enabled():
        raise Http404

    payments = list(
        Payment.objects
        .filter(user=request.user)
        .exclude(invoice_id=None)
        .exclude(status=PaymentStatus.PENDING, callback_payload__isnull=True)
        .order_by("-created")
    )

    names = _course_names({payment.course_id for payment in payments})

    return render(request, "halyk_payments/orders.html", {
        "orders": [(payment, names.get(payment.course_id, str(payment.course_id)))
                   for payment in payments],
    })


def _course_names(course_keys):
    """Display names for a set of courses, in one query."""
    if not course_keys:
        return {}
    try:
        from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
        return {
            overview.id: overview.display_name
            for overview in CourseOverview.objects.filter(id__in=list(course_keys))
            if overview.display_name
        }
    except Exception:  # pylint: disable=broad-except
        log.debug("Could not read course names for the order list")
        return {}


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
    return render(request, "halyk_payments/receipt.html", {
        "payment": payment,
        "course_id": str(payment.course_id),
        "course_name": _course_name(payment.course_id),
        "test_mode": bool(getattr(settings, "HALYK_TEST_MODE", True))
                     or _fake_gateway(),
    })
