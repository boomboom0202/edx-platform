"""
Tests for turning a hold into a charge.

On a two-step terminal every payment stops with the money merely blocked. What
matters is that a course opens only once the money has actually moved, and that
a charge which was already made does not read as a failure.
"""
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from opaque_keys.edx.keys import CourseKey

from halyk_payments import services
from halyk_payments.client import HalykError, TransactionStatus
from halyk_payments.models import Payment


COURSE = CourseKey.from_string("course-v1:ENV+HYD_01+2022")
TERMINAL = "67e34d63-102f-4bd1-898e-370781d0074d"


@pytest.fixture(autouse=True)
def halyk_settings(settings):
    settings.HALYK_TERMINAL_ID = TERMINAL
    settings.HALYK_ACCEPTED_STATUSES = ["CHARGE"]
    settings.HALYK_AUTO_CAPTURE = True
    return settings


@pytest.fixture
def payment(db):
    user = get_user_model().objects.create(username="learner", email="l@example.com")
    return Payment.objects.create(
        invoice_id="1000004", user=user, course_id=COURSE, course_mode="verified",
        amount=50, currency="KZT",
        transaction_id="c5586ebc-8451-49c1-ad8d-917009a4b412",
    )


def _status(status_name, amount=50):
    return TransactionStatus({
        "resultCode": "100",
        "resultMessage": "SUCCESS",
        "transaction": {
            "id": "c5586ebc-8451-49c1-ad8d-917009a4b412",
            "amount": amount, "amountBonus": 0,
            "statusName": status_name, "terminalID": TERMINAL,
        },
    })


def bank(statuses, charge_raises=None):
    """A client whose status answers come back in the given order."""
    client = mock.Mock()
    client.get_api_token.return_value = {"access_token": "t"}
    client.get_payment_status.side_effect = list(statuses)
    if charge_raises:
        client.charge_operation.side_effect = charge_raises
    return mock.patch.object(services, "HalykClient", return_value=client), client


def test_a_hold_is_charged_and_then_opens_the_course(payment):
    patch, client = bank([_status("AUTH"), _status("CHARGE")])
    with patch:
        verdict, detail, status = services.confirm_with_bank(payment)

    assert client.charge_operation.call_count == 1
    assert client.charge_operation.call_args.args[0] == payment.transaction_id
    assert verdict is True
    assert status.status_name == "CHARGE"


def test_the_whole_authorised_sum_is_taken(payment):
    """The learner gets the whole course, so the whole amount is charged."""
    patch, client = bank([_status("AUTH"), _status("CHARGE")])
    with patch:
        services.confirm_with_bank(payment)

    assert client.charge_operation.call_args.kwargs.get("amount") is None


def test_a_charge_already_made_is_not_a_failure(payment):
    """
    A callback delivered twice charges on the first pass and is refused on the
    second, the transaction having left AUTH. The bank's answer afterwards is
    what counts, not the refusal.
    """
    patch, client = bank([_status("AUTH"), _status("CHARGE")],
                         charge_raises=HalykError("charge refused: code 100"))
    with patch:
        verdict, detail, status = services.confirm_with_bank(payment)

    assert verdict is True
    assert status.status_name == "CHARGE"


def test_a_charge_that_really_failed_opens_nothing(payment):
    patch, _ = bank([_status("AUTH"), _status("AUTH")],
                    charge_raises=HalykError("charge refused"))
    with patch:
        verdict, detail, _ = services.confirm_with_bank(payment)

    assert verdict is False
    assert "AUTH" in detail


def test_an_unreadable_status_after_charging_is_left_open(payment):
    """
    The money may well have moved, so calling this a failure would be wrong —
    it stays pending for the reconcile command to settle.
    """
    patch, _ = bank([_status("AUTH"), HalykError("network")])
    with patch:
        verdict, _, _ = services.confirm_with_bank(payment)

    assert verdict is None


def test_nothing_is_charged_when_capture_is_switched_off(payment, halyk_settings):
    halyk_settings.HALYK_AUTO_CAPTURE = False
    patch, client = bank([_status("AUTH")])
    with patch:
        verdict, _, _ = services.confirm_with_bank(payment)

    assert client.charge_operation.call_count == 0
    assert verdict is False


def test_an_already_charged_payment_is_not_charged_again(payment):
    patch, client = bank([_status("CHARGE")])
    with patch:
        verdict, _, _ = services.confirm_with_bank(payment)

    assert client.charge_operation.call_count == 0
    assert verdict is True


def test_a_hold_for_the_wrong_amount_is_never_charged(payment):
    """Verify first, take money second — never the other way round."""
    patch, client = bank([_status("AUTH", amount=1)])
    with patch:
        verdict, _, _ = services.confirm_with_bank(payment)

    assert client.charge_operation.call_count == 0
    assert verdict is False
