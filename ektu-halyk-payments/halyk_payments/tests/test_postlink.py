"""
Tests for the callback, which is the only thing in this app that grants access.

Everything here is about one question: under exactly which circumstances does a
learner end up enrolled? A mistake in either direction costs the university
money — a course opened without payment, or a payment taken without a course.
"""
import json
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from opaque_keys.edx.keys import CourseKey

from halyk_payments import views
from halyk_payments.client import TransactionStatus
from halyk_payments.models import Payment, PaymentStatus


COURSE = CourseKey.from_string("course-v1:ENV+HYD_01+2022")
TERMINAL = "67e34d63-102f-4bd1-898e-370781d0074d"


@pytest.fixture(autouse=True)
def halyk_settings(settings):
    settings.HALYK_ENABLED = True
    settings.HALYK_FAKE_GATEWAY = False
    settings.HALYK_TERMINAL_ID = TERMINAL
    settings.HALYK_POSTLINK_IP_ALLOWLIST = []
    settings.HALYK_VERIFY_WITH_STATUS_API = False
    settings.HALYK_ACCEPTED_STATUSES = ["CHARGE"]
    return settings


@pytest.fixture
def payment(db):
    user = get_user_model().objects.create(username="learner", email="l@example.com")
    return Payment.objects.create(
        invoice_id="1000001", user=user, course_id=COURSE, course_mode="verified",
        amount=50000, currency="KZT", secret_hash="a" * 32,
    )


def callback(payment, **overrides):
    """A success callback shaped the way the bank documents it."""
    body = {
        "invoiceId": payment.invoice_id,
        "amount": payment.amount,
        "currency": payment.currency,
        "terminal": TERMINAL,
        "accountId": str(payment.user_id),
        "code": "ok",
        "reason": "success",
        "reasonCode": 0,
        "approvalCode": "157911",
        "reference": "411111111117",
        "cardMask": "440043...2222",
        "cardType": "VISA",
        "secret_hash": payment.secret_hash,
    }
    body.update(overrides)
    return body


def post(body):
    request = RequestFactory().post(
        "/halyk/postlink/", data=json.dumps(body), content_type="application/json",
    )
    with mock.patch("common.djangoapps.student.models.CourseEnrollment.enroll") as enroll:
        response = views.postlink(request)
    return response, enroll


# -- the happy path ----------------------------------------------------------

def test_a_confirmed_payment_enrolls_the_learner(payment):
    response, enroll = post(callback(payment))

    assert response.status_code == 200
    assert enroll.call_count == 1
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PAID
    assert payment.enrolled is True
    assert payment.reference == "411111111117"


# -- forged and mismatched callbacks -----------------------------------------

def test_a_callback_with_the_wrong_secret_is_refused(payment):
    """secret_hash never leaves the server, so a wrong one means a forgery."""
    response, enroll = post(callback(payment, secret_hash="b" * 32))

    assert response.status_code == 403
    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.enrolled is False
    assert payment.status == PaymentStatus.PENDING


def test_a_callback_for_a_smaller_amount_does_not_open_the_course(payment):
    response, enroll = post(callback(payment, amount=1))

    assert response.status_code == 400
    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.enrolled is False


def test_a_callback_from_another_terminal_does_not_open_the_course(payment):
    response, enroll = post(callback(payment, terminal="someone-elses-terminal"))

    assert response.status_code == 400
    assert enroll.call_count == 0


def test_a_callback_for_an_unknown_invoice_is_refused(payment):
    response, enroll = post(callback(payment, invoiceId="9999999"))

    assert response.status_code == 404
    assert enroll.call_count == 0


def test_a_missing_secret_hash_is_tolerated(payment):
    """
    The documented postLink examples do not show the field, so its absence
    cannot be treated as forgery; the status API is what catches that case.
    """
    body = callback(payment)
    del body["secret_hash"]
    response, enroll = post(body)

    assert response.status_code == 200
    assert enroll.call_count == 1


# -- failures ----------------------------------------------------------------

def test_a_final_error_is_recorded_as_a_failure(payment):
    """484 is "недостаточно средств" and is documented as final."""
    response, enroll = post(callback(payment, code="error", reasonCode=484,
                                     reason="Недостаточно средств на карте"))

    assert response.status_code == 200
    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.FAILED


def test_a_non_final_error_leaves_the_payment_pending(payment):
    """
    454 is documented as "не финальный, необходимо запросить статус оплаты".
    Recording it as a failure would close a payment that may still succeed.
    """
    response, enroll = post(callback(payment, code="error", reasonCode=454,
                                     reason="Операция не удалась"))

    assert response.status_code == 200
    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING


def test_a_repeated_callback_does_not_enroll_twice(payment):
    post(callback(payment))
    _, enroll = post(callback(payment))

    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.enrolled is True


# -- the status API as the second opinion ------------------------------------

def _with_status(status_body=None, raises=None):
    """Patch the client so the verification step sees a chosen answer."""
    from halyk_payments import services

    client = mock.Mock()
    client.get_api_token.return_value = {"access_token": "t"}
    if raises is not None:
        client.get_payment_status.side_effect = raises
    else:
        client.get_payment_status.return_value = TransactionStatus(status_body)
    return mock.patch.object(services, "HalykClient", return_value=client)


def _transaction(status_name="CHARGE", amount=50000, result_code="100"):
    return {
        "resultCode": result_code,
        "resultMessage": "SUCCESS",
        "transaction": {"statusName": status_name, "amount": amount,
                        "terminalID": TERMINAL},
    }


def test_the_bank_is_asked_before_access_is_granted(payment, halyk_settings):
    halyk_settings.HALYK_VERIFY_WITH_STATUS_API = True

    with _with_status(_transaction()):
        response, enroll = post(callback(payment))

    assert response.status_code == 200
    assert enroll.call_count == 1


def test_money_only_blocked_on_the_card_does_not_open_the_course(payment, halyk_settings):
    """
    AUTH means a two-step terminal is holding the money pending a capture this
    plugin does not issue. Treating it as paid would open a course against money
    that may never arrive.
    """
    halyk_settings.HALYK_VERIFY_WITH_STATUS_API = True

    with _with_status(_transaction(status_name="AUTH")):
        response, enroll = post(callback(payment))

    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.FAILED


def test_a_transaction_still_in_progress_leaves_the_payment_pending(payment, halyk_settings):
    halyk_settings.HALYK_VERIFY_WITH_STATUS_API = True

    with _with_status(_transaction(result_code="107")):
        response, enroll = post(callback(payment))

    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING


def test_an_unreachable_bank_leaves_the_payment_pending(payment, halyk_settings):
    """
    A network problem is not evidence that the learner did not pay. Leaving the
    payment for a human is safer than either enrolling or writing it off.
    """
    from halyk_payments.client import HalykError

    halyk_settings.HALYK_VERIFY_WITH_STATUS_API = True

    with _with_status(raises=HalykError("boom")):
        response, enroll = post(callback(payment))

    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING


def test_the_status_api_catches_a_forged_amount(payment, halyk_settings):
    """The callback says the right amount; the bank says otherwise."""
    halyk_settings.HALYK_VERIFY_WITH_STATUS_API = True

    with _with_status(_transaction(amount=1)):
        response, enroll = post(callback(payment))

    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.FAILED


# -- source restriction ------------------------------------------------------

def test_the_ip_allowlist_shuts_out_everyone_else(payment, halyk_settings):
    halyk_settings.HALYK_POSTLINK_IP_ALLOWLIST = ["203.0.113.7"]
    request = RequestFactory().post(
        "/halyk/postlink/", data=json.dumps(callback(payment)),
        content_type="application/json", REMOTE_ADDR="198.51.100.4",
    )

    response = views.postlink(request)

    assert response.status_code == 403
