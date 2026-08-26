"""
Tests for giving money back and releasing holds.

Both move real money, and both have a way of going wrong that costs the
university: refunding more than was taken, or leaving a learner with a course
they no longer paid for.
"""
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from opaque_keys.edx.keys import CourseKey

from halyk_payments import services
from halyk_payments.client import HalykError
from halyk_payments.models import Payment, PaymentStatus


COURSE = CourseKey.from_string("course-v1:ENV+HYD_01+2022")


@pytest.fixture
def learner(db):
    return get_user_model().objects.create(username="learner", email="l@example.com")


@pytest.fixture
def paid(db, learner):
    return Payment.objects.create(
        invoice_id="1000004", user=learner, course_id=COURSE, course_mode="verified",
        amount=50, currency="KZT", status=PaymentStatus.PAID, enrolled=True,
        transaction_id="c5586ebc-8451-49c1-ad8d-917009a4b412",
    )


@pytest.fixture
def held(db, learner):
    """A payment refused after the card was authorised: money blocked, no access."""
    return Payment.objects.create(
        invoice_id="1000006", user=learner, course_id=COURSE, course_mode="verified",
        amount=50, currency="KZT", status=PaymentStatus.FAILED, enrolled=False,
        transaction_id="aaaaaaaa-0000-0000-0000-000000000000",
        failure_reason="Bank says: statusName AUTH does not grant access",
    )


def bank():
    client = mock.Mock()
    client.get_api_token.return_value = {"access_token": "t"}
    client.refund_operation.return_value = True
    client.cancel_operation.return_value = True
    return mock.patch.object(services, "HalykClient", return_value=client), client


# -- refunds -----------------------------------------------------------------

def test_a_full_refund_takes_the_course_back(paid):
    """Otherwise the learner keeps the course and the money."""
    patch, client = bank()
    with patch, mock.patch("common.djangoapps.student.models.CourseEnrollment.unenroll") as out:
        services.refund_payment(paid.pk)

    assert out.call_count == 1
    paid.refresh_from_db()
    assert paid.status == PaymentStatus.REFUNDED
    assert paid.refunded_amount == 50
    assert paid.enrolled is False
    # A full refund is sent without an amount, as the bank documents it.
    assert client.refund_operation.call_args.kwargs["amount"] is None


def test_a_partial_refund_leaves_the_course_alone(paid):
    patch, client = bank()
    with patch, mock.patch("common.djangoapps.student.models.CourseEnrollment.unenroll") as out:
        services.refund_payment(paid.pk, amount=20)

    assert out.call_count == 0
    paid.refresh_from_db()
    assert paid.status == PaymentStatus.PAID
    assert paid.refunded_amount == 20
    assert paid.enrolled is True
    assert client.refund_operation.call_args.kwargs["amount"] == 20


def test_refunds_cannot_add_up_to_more_than_was_paid(paid):
    patch, _ = bank()
    with patch, mock.patch("common.djangoapps.student.models.CourseEnrollment.unenroll"):
        services.refund_payment(paid.pk, amount=30)
        with pytest.raises(services.PaymentError):
            services.refund_payment(paid.pk, amount=30)

    paid.refresh_from_db()
    assert paid.refunded_amount == 30


def test_the_last_refund_closes_the_payment(paid):
    patch, _ = bank()
    with patch, mock.patch("common.djangoapps.student.models.CourseEnrollment.unenroll") as out:
        services.refund_payment(paid.pk, amount=30)
        services.refund_payment(paid.pk, amount=20)

    paid.refresh_from_db()
    assert paid.status == PaymentStatus.REFUNDED
    assert paid.enrolled is False
    assert out.call_count == 1


def test_the_banks_minimum_refund_is_respected(paid):
    """Below ten tenge the bank refuses, so there is no point asking."""
    patch, client = bank()
    with patch:
        with pytest.raises(services.PaymentError):
            services.refund_payment(paid.pk, amount=5)

    assert client.refund_operation.call_count == 0


def test_an_unpaid_payment_cannot_be_refunded(held):
    patch, client = bank()
    with patch:
        with pytest.raises(services.PaymentError):
            services.refund_payment(held.pk)

    assert client.refund_operation.call_count == 0


def test_a_refused_refund_changes_nothing(paid):
    """The bank is the one that decides; our records must not run ahead of it."""
    patch, client = bank()
    client.refund_operation.side_effect = HalykError("refund refused: code 100")

    with patch, mock.patch("common.djangoapps.student.models.CourseEnrollment.unenroll"):
        with pytest.raises(HalykError):
            services.refund_payment(paid.pk)

    paid.refresh_from_db()
    assert paid.refunded_amount == 0
    assert paid.status == PaymentStatus.PAID
    assert paid.enrolled is True


# -- holds -------------------------------------------------------------------

def test_a_hold_can_be_released(held):
    patch, client = bank()
    with patch:
        services.cancel_payment(held.pk)

    assert client.cancel_operation.call_count == 1
    held.refresh_from_db()
    assert held.status == PaymentStatus.CANCELLED


def test_a_payment_that_opened_a_course_is_not_cancelled(paid):
    """That is a refund; cancelling would release nothing and lose the record."""
    patch, client = bank()
    with patch:
        with pytest.raises(services.PaymentError):
            services.cancel_payment(paid.pk)

    assert client.cancel_operation.call_count == 0


# -- finding the bank's transaction id ---------------------------------------

def test_the_transaction_id_is_recovered_from_an_old_callback(db, learner):
    """Payments taken before the column existed keep it inside the callback."""
    old = Payment.objects.create(
        invoice_id="1000003", user=learner, course_id=COURSE, course_mode="verified",
        amount=50, currency="KZT", status=PaymentStatus.PAID, enrolled=True,
        callback_payload={"id": "06c057e3-5c2d-47f2-bc4d-37fba543ef05"},
    )

    assert services.transaction_id_for(old) == "06c057e3-5c2d-47f2-bc4d-37fba543ef05"


def test_the_transaction_id_is_asked_for_when_nothing_recorded_it(db, learner):
    from halyk_payments.client import TransactionStatus

    bare = Payment.objects.create(
        invoice_id="1000007", user=learner, course_id=COURSE, course_mode="verified",
        amount=50, currency="KZT", status=PaymentStatus.PAID,
    )
    patch, client = bank()
    client.get_payment_status.return_value = TransactionStatus(
        {"resultCode": "100", "transaction": {"id": "from-the-bank"}}
    )

    with patch:
        assert services.transaction_id_for(bare) == "from-the-bank"
