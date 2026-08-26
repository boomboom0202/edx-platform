"""
Tests for the learner's order history.

It is the page someone opens when they think they have been charged twice, so
what it leaves out matters as much as what it shows.
"""
import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory
from opaque_keys.edx.keys import CourseKey

from halyk_payments import views
from halyk_payments.models import Payment, PaymentStatus


COURSE = CourseKey.from_string("course-v1:ENV+HYD_01+2022")


@pytest.fixture(autouse=True)
def enabled(settings):
    settings.HALYK_ENABLED = True
    return settings


@pytest.fixture
def buyer(db):
    return get_user_model().objects.create(username="buyer", email="b@example.com")


def make(user, invoice, status=PaymentStatus.PAID, **extra):
    return Payment.objects.create(
        invoice_id=invoice, user=user, course_id=COURSE, course_mode="verified",
        amount=50000, currency="KZT", status=status, **extra,
    )


def orders_for(user):
    request = RequestFactory().get("/halyk/orders/")
    request.user = user
    return views.orders(request)


def test_a_learner_sees_their_own_orders(buyer):
    make(buyer, "1000001", enrolled=True)

    body = orders_for(buyer).content.decode()

    assert "1000001" in body
    assert "50 000" in body


def test_nobody_sees_anybody_elses(buyer, db):
    make(buyer, "1000001", enrolled=True)
    stranger = get_user_model().objects.create(username="stranger", email="s@e.com")

    assert "1000001" not in orders_for(stranger).content.decode()


def test_an_abandoned_checkout_is_not_an_order(buyer):
    """
    Opening the payment page and closing it leaves a pending row. Listing those
    would bury the real orders under every idle click.
    """
    make(buyer, "1000002", status=PaymentStatus.PENDING)

    assert "1000002" not in orders_for(buyer).content.decode()


def test_a_pending_payment_the_bank_told_us_about_is_shown(buyer):
    """Money may have moved; the learner has to see it is being sorted out."""
    make(buyer, "1000003", status=PaymentStatus.PENDING,
         callback_payload={"code": "ok"})

    assert "1000003" in orders_for(buyer).content.decode()


def test_failed_and_refunded_orders_are_shown(buyer):
    make(buyer, "1000004", status=PaymentStatus.FAILED,
         callback_payload={"code": "error"})
    make(buyer, "1000005", status=PaymentStatus.REFUNDED, refunded_amount=50000)

    body = orders_for(buyer).content.decode()

    assert "1000004" in body
    assert "1000005" in body


def test_only_a_real_payment_offers_a_receipt(buyer):
    make(buyer, "1000006", status=PaymentStatus.FAILED,
         callback_payload={"code": "error"})

    assert "/halyk/receipt/1000006/" not in orders_for(buyer).content.decode()


def test_an_empty_history_says_so(buyer):
    assert "not bought any courses" in orders_for(buyer).content.decode()


def test_amounts_are_grouped_and_carry_the_currency(buyer):
    payment = make(buyer, "1000007", enrolled=True)

    assert payment.display_amount == "50 000 ₸"
    assert Payment(amount=50, currency="KZT").display_amount == "50 ₸"
    assert Payment(amount=100, currency="XYZ").display_amount == "100 XYZ"
