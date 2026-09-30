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


def test_an_order_settled_partly_with_bonuses_is_fully_paid(payment):
    """
    Halyk lets a cardholder cover part of an order with loyalty bonuses and
    pays the merchant the whole of it. Reading `amount` alone would refuse a
    50 tenge course settled as 15 in cash and 35 in bonuses.
    """
    response, enroll = post(callback(payment, amount=15000, amount_bonus=35000))

    assert response.status_code == 200
    assert enroll.call_count == 1
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PAID


def test_bonuses_do_not_cover_a_short_payment(payment):
    response, enroll = post(callback(payment, amount=15000, amount_bonus=5000))

    assert response.status_code == 400
    assert enroll.call_count == 0


def test_an_overpayment_still_opens_the_course(payment):
    """Withholding a course from someone who paid too much helps nobody."""
    response, enroll = post(callback(payment, amount=60000))

    assert response.status_code == 200
    assert enroll.call_count == 1


def test_a_callback_from_another_terminal_does_not_open_the_course(payment):
    response, enroll = post(callback(payment, terminal="someone-elses-terminal"))

    assert response.status_code == 400
    assert enroll.call_count == 0


def test_a_callback_for_an_unknown_invoice_is_refused(payment):
    response, enroll = post(callback(payment, invoiceId="9999999"))

    assert response.status_code == 404
    assert enroll.call_count == 0


def unsigned(payment, **overrides):
    body = callback(payment, **overrides)
    del body["secret_hash"]
    return body


def test_an_unsigned_callback_is_not_rejected_but_the_bank_decides(payment):
    """
    The documented postLink examples do not show secret_hash, so its absence is
    not treated as forgery. Nothing in such a callback is believed, though: the
    bank is asked even with HALYK_VERIFY_WITH_STATUS_API switched off, which
    would otherwise open the course on an unverified message.
    """
    with _with_status(_transaction()) as bank:
        response, enroll = post(unsigned(payment))

    assert response.status_code == 200
    assert enroll.call_count == 1
    assert bank.return_value.get_payment_status.call_count == 1


def test_an_unsigned_failure_cannot_knock_over_somebody_elses_checkout(payment):
    """
    Invoice numbers run in sequence, so anyone can guess one and post "code:
    error" for it. That used to mark a learner's payment failed while they were
    in the middle of paying, and send them to pay a second time.
    """
    with _with_status(_transaction(result_code="107")):
        response, enroll = post(unsigned(payment, code="error", reasonCode=484))

    assert response.status_code == 200
    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING
    assert payment.transaction_id == ""


def test_an_unsigned_failure_does_not_outvote_the_bank(payment):
    """The callback said "error"; the bank says the money was taken."""
    with _with_status(_transaction()):
        _, enroll = post(unsigned(payment, code="error", reasonCode=484))

    assert enroll.call_count == 1
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PAID


def test_an_unsigned_callback_leaves_nothing_of_itself_behind(payment):
    """What gets recorded is the bank's answer, not the stranger's claims."""
    bank_answer = _transaction(transaction_id="bank-tx-1", reference="REF-BANK",
                               card_mask="400000...0002")
    with _with_status(bank_answer):
        post(unsigned(payment, id="forged-tx", reference="FORGED", cardMask="FORGED"))

    payment.refresh_from_db()
    assert payment.transaction_id == "bank-tx-1"
    assert payment.reference == "REF-BANK"
    assert payment.card_mask == "400000...0002"
    assert payment.callback_payload == bank_answer


def test_an_unsigned_callback_with_the_bank_unreachable_stays_pending(payment):
    from halyk_payments.client import HalykError

    with _with_status(raises=HalykError("boom")):
        _, enroll = post(unsigned(payment, code="error", reasonCode=484))

    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PENDING


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


def _transaction(status_name="CHARGE", amount=50000, result_code="100",
                 transaction_id="", reference="", card_mask=""):
    body = {
        "resultCode": result_code,
        "resultMessage": "SUCCESS",
        "transaction": {"statusName": status_name, "amount": amount,
                        "terminalID": TERMINAL},
    }
    for key, value in (("id", transaction_id), ("reference", reference),
                       ("cardMask", card_mask)):
        if value:
            body["transaction"][key] = value
    return body


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


def test_the_banks_transaction_id_is_recorded_not_the_callbacks(payment, halyk_settings):
    """Every later refund is addressed by this id, so it has to be the bank's."""
    halyk_settings.HALYK_VERIFY_WITH_STATUS_API = True

    with _with_status(_transaction(transaction_id="bank-tx-9")):
        post(callback(payment, id="callback-tx"))

    payment.refresh_from_db()
    assert payment.transaction_id == "bank-tx-9"
    # A signed callback is still what support gets to read.
    assert payment.callback_payload["id"] == "callback-tx"


# -- a decision already made is not reopened ---------------------------------

def test_a_repeated_success_does_not_hand_back_access_that_was_withdrawn(payment):
    """
    Paid, then access taken away by hand (a partial refund with
    --withdraw-access). The bank re-sending its old success message must not
    quietly give the course back.
    """
    Payment.objects.filter(pk=payment.pk).update(
        status=PaymentStatus.PAID, enrolled=False)

    _, enroll = post(callback(payment))

    assert enroll.call_count == 0
    payment.refresh_from_db()
    assert payment.enrolled is False


def test_a_late_failure_does_not_rewrite_a_refund(payment):
    """A refunded order must stay "Refunded" in the learner's history."""
    Payment.objects.filter(pk=payment.pk).update(
        status=PaymentStatus.REFUNDED, enrolled=False, refunded_amount=50000)

    post(callback(payment, code="error", reasonCode=484))

    payment.refresh_from_db()
    assert payment.status == PaymentStatus.REFUNDED


# -- source restriction ------------------------------------------------------

# Public addresses on purpose: the platform's helper skips anything that is
# not globally routable, documentation ranges (203.0.113.0/24 and friends)
# included, so those would never exercise the real rule.
BANK_IP = "95.56.12.34"
ATTACKER_IP = "37.99.40.2"


def test_the_allowlist_reads_the_address_behind_the_proxy(payment, halyk_settings):
    """
    Behind Tutor's Caddy the connection comes from the proxy's own private
    address; the bank's is the one Caddy appended to X-Forwarded-For.
    """
    halyk_settings.HALYK_POSTLINK_IP_ALLOWLIST = [BANK_IP]
    halyk_settings.CLOSEST_CLIENT_IP_FROM_HEADERS = []
    request = RequestFactory().post(
        "/halyk/postlink/", data=json.dumps(callback(payment)),
        content_type="application/json",
        REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR=BANK_IP,
    )

    with mock.patch("common.djangoapps.student.models.CourseEnrollment.enroll"):
        response = views.postlink(request)

    assert response.status_code == 200


def test_the_allowlist_cannot_be_fooled_with_a_forged_header(payment, halyk_settings):
    """A client can put anything at the left of X-Forwarded-For; Caddy then
    appends the address it really saw, and that is the one that counts."""
    halyk_settings.HALYK_POSTLINK_IP_ALLOWLIST = [BANK_IP]
    halyk_settings.CLOSEST_CLIENT_IP_FROM_HEADERS = []
    request = RequestFactory().post(
        "/halyk/postlink/", data=json.dumps(callback(payment)),
        content_type="application/json",
        REMOTE_ADDR="172.18.0.5",
        HTTP_X_FORWARDED_FOR=f"{BANK_IP}, {ATTACKER_IP}",
    )

    response = views.postlink(request)

    assert response.status_code == 403


def test_the_ip_allowlist_shuts_out_everyone_else(payment, halyk_settings):
    halyk_settings.HALYK_POSTLINK_IP_ALLOWLIST = ["203.0.113.7"]
    request = RequestFactory().post(
        "/halyk/postlink/", data=json.dumps(callback(payment)),
        content_type="application/json", REMOTE_ADDR="198.51.100.4",
    )

    response = views.postlink(request)

    assert response.status_code == 403
