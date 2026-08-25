"""
Tests for the receipt, which is the learner's proof that the money went
somewhere. The part that matters is that it is proof of *their* payment.
"""
import pytest
from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import RequestFactory
from opaque_keys.edx.keys import CourseKey

from halyk_payments import views
from halyk_payments.models import Payment, PaymentStatus


COURSE = CourseKey.from_string("course-v1:ENV+HYD_01+2022")


@pytest.fixture(autouse=True)
def enabled(settings):
    settings.HALYK_ENABLED = True
    settings.HALYK_TEST_MODE = True
    return settings


@pytest.fixture
def buyer(db):
    return get_user_model().objects.create(username="buyer", email="b@example.com")


@pytest.fixture
def paid(db, buyer):
    return Payment.objects.create(
        invoice_id="1000001", user=buyer, course_id=COURSE, course_mode="verified",
        amount=50, currency="KZT", status=PaymentStatus.PAID, enrolled=True,
        reference="411111111117", card_mask="440043...2222",
    )


def get(view, invoice_id, user):
    request = RequestFactory().get(f"/halyk/receipt/{invoice_id}/")
    request.user = user
    return view(request, invoice_id)


def test_the_buyer_sees_their_receipt(paid, buyer):
    response = get(views.receipt, paid.invoice_id, buyer)

    assert response.status_code == 200
    body = response.content.decode()
    assert paid.invoice_id in body
    assert paid.reference in body
    assert "440043...2222" in body


def test_nobody_else_can_see_it(paid, db):
    """A receipt names a person, a course and a card; it is not public."""
    stranger = get_user_model().objects.create(username="stranger", email="s@example.com")

    with pytest.raises(Http404):
        get(views.receipt, paid.invoice_id, stranger)


def test_an_unpaid_invoice_has_no_receipt(db, buyer):
    pending = Payment.objects.create(
        invoice_id="1000002", user=buyer, course_id=COURSE, course_mode="verified",
        amount=50, currency="KZT",
    )

    response = get(views.receipt, pending.invoice_id, buyer)

    assert response.status_code == 302
    assert response["Location"].endswith(f"/result/{pending.invoice_id}/")


def test_a_paid_invoice_lands_on_the_receipt_instead_of_the_course(paid, buyer):
    """The moment after paying is when someone wants to see where the money went."""
    response = get(views.result, paid.invoice_id, buyer)

    assert response.status_code == 200
    assert paid.invoice_id in response.content.decode()


def test_a_test_payment_says_so_on_the_receipt(paid, buyer, enabled):
    enabled.HALYK_TEST_MODE = True
    assert "Test payment." in get(views.receipt, paid.invoice_id, buyer).content.decode()
