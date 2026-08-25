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

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey

from .client import HalykClient, HalykError
from .models import Payment, PaymentStatus
from .services import CheckoutError, mark_failed, mark_paid_and_enroll, start_checkout

log = logging.getLogger(__name__)


def _enabled():
    return bool(getattr(settings, "HALYK_ENABLED", False))


def _fake_gateway():
    """The fake gateway is only ever available on a debug deployment."""
    return bool(getattr(settings, "HALYK_FAKE_GATEWAY", False)) and settings.DEBUG


def _absolute(request, path):
    return request.build_absolute_uri(path)


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

    if _fake_gateway():
        # Everything except the bank: the page offers a button that posts a
        # simulated callback to our own postlink endpoint.
        return render(request, "halyk_payments/checkout.html", {
            "payment": payment,
            "fake": True,
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
            account_id=request.user.id,
        )
    except HalykError:
        mark_failed(payment.pk, reason="Could not obtain a payment token")
        return render(request, "halyk_payments/result.html", {
            "state": "error",
            "message": "The payment service is unavailable. Please try again later.",
            "course_id": course_id,
        }, status=502)

    # What the widget needs. Field names follow the ePay payment object;
    # confirm against the bank's documentation before going live.
    payment_object = {
        "invoiceId": payment.invoice_id,
        "backLink": _absolute(request, reverse("halyk_payments:result",
                                               args=[payment.invoice_id])),
        "failureBackLink": _absolute(request, reverse("halyk_payments:result",
                                                      args=[payment.invoice_id])),
        "postLink": _absolute(request, reverse("halyk_payments:postlink")),
        "language": (request.LANGUAGE_CODE or "ru")[:2],
        "description": f"Course access: {payment.course_id}",
        "accountId": str(request.user.id),
        "terminal": client.terminal,
        "amount": payment.amount,
        "currency": payment.currency,
        "email": request.user.email,
        "auth": token,
    }

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
    if not payload:
        return HttpResponseBadRequest("empty payload")

    secret = getattr(settings, "HALYK_POSTLINK_SECRET", "")
    if secret and payload.get("secret") != secret:
        log.warning("Rejected a Halyk callback with a bad shared secret")
        return JsonResponse({"status": "rejected"}, status=403)

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

    # CONTRACT: the field that carries the outcome. Confirm the exact name and
    # the success value against the documentation.
    code = str(payload.get("code", payload.get("status", ""))).lower()
    succeeded = code in ("ok", "success", "0", "auth", "charge")

    if succeeded and getattr(settings, "HALYK_VERIFY_WITH_STATUS_API", True) \
            and not _fake_gateway():
        succeeded = _confirm_with_bank(payment)

    if not succeeded:
        mark_failed(payment.pk, reason=str(payload.get("reason", code))[:255],
                    payload=payload)
        return JsonResponse({"status": "recorded as failed"})

    mark_paid_and_enroll(
        payment.pk,
        payload=payload,
        reference=str(payload.get("reference", ""))[:128],
        card_mask=str(payload.get("cardMask", ""))[:32],
    )
    return JsonResponse({"status": "ok"})


def _confirm_with_bank(payment):
    """
    Ask the bank directly, so a forged callback cannot enroll anybody.

    A failure to reach the bank is treated as "not confirmed": it is always
    safer to leave a real payment pending for a human than to open a course on
    an unverified message.
    """
    client = HalykClient()
    try:
        token = client.get_payment_token(
            invoice_id=payment.invoice_id,
            amount=payment.amount,
            currency=payment.currency,
        )
        status = client.get_payment_status(payment.invoice_id, token["access_token"])
    except (HalykError, KeyError) as exc:
        log.error("Could not confirm invoice %s with the bank: %s",
                  payment.invoice_id, exc)
        return False

    # CONTRACT: the shape of the status response.
    outcome = str(status.get("transaction", {}).get("statusName",
                  status.get("status", ""))).upper()
    amount_ok = int(status.get("transaction", {}).get("amount", payment.amount)) == payment.amount
    if not amount_ok:
        log.error("Invoice %s was paid for the wrong amount", payment.invoice_id)
        return False
    return outcome in ("AUTH", "CHARGE", "OK", "SUCCESS")


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
