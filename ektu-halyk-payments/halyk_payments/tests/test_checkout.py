"""
Tests for getting into a course we sell: when a learner pays, and when they
must not be asked to.

The case that prompted most of this: a learner paid, left the course from
their dashboard, came back — and was charged again. Leaving is not a refund.
The offer ends access only when the money is returned (sections 6.6 and 6.8),
so until then the purchase stands and coming back is free.
"""
import json
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory
from django.urls import reverse
from opaque_keys.edx.keys import CourseKey

from halyk_payments import services, views
from halyk_payments.models import Payment, PaymentStatus


COURSE = CourseKey.from_string("course-v1:ENV+HYD_01+2022")
ENROLLMENT = "common.djangoapps.student.models.CourseEnrollment"


class Enrollments:
    """
    What the platform's CourseEnrollment does, as far as payments care:
    unenrolling keeps the row and its mode and only switches it off.
    """

    def __init__(self):
        self.rows = {}
        self.enroll_calls = 0

    def mode_for_user(self, user, course_id):
        return self.rows.get((user.pk, str(course_id)), (None, None))

    def enroll(self, user, course_key, mode=None, **kwargs):
        self.enroll_calls += 1
        self.rows[(user.pk, str(course_key))] = (mode or "audit", True)

    def unenroll(self, user, course_id, **kwargs):
        mode, _ = self.rows.get((user.pk, str(course_id)), ("audit", False))
        self.rows[(user.pk, str(course_id))] = (mode, False)


@pytest.fixture(autouse=True)
def configured(settings):
    settings.HALYK_ENABLED = True
    settings.HALYK_FAKE_GATEWAY = False
    settings.HALYK_COURSE_MODE = "verified"
    settings.HALYK_CURRENCY = "KZT"
    return settings


@pytest.fixture
def enrollments():
    state = Enrollments()
    with mock.patch(f"{ENROLLMENT}.enrollment_mode_for_user", side_effect=state.mode_for_user), \
            mock.patch(f"{ENROLLMENT}.enroll", side_effect=state.enroll), \
            mock.patch(f"{ENROLLMENT}.unenroll", side_effect=state.unenroll):
        yield state


@pytest.fixture
def for_sale():
    mode = mock.Mock(mode_slug="verified", min_price=50000, currency="kzt")
    with mock.patch.object(services, "get_paid_mode", return_value=mode):
        yield mode


@pytest.fixture
def bank():
    client = mock.Mock()
    client.is_configured.return_value = True
    client.terminal = "67e34d63-102f-4bd1-898e-370781d0074d"
    client.widget_js_url = "https://test-epay.epayment.kz/payform/payment-api.js"
    client.widget_origin = "https://test-epay.epayment.kz"
    client.get_payment_token.return_value = {"access_token": "tok", "expires_in": 1200}
    with mock.patch.object(views, "HalykClient", return_value=client):
        yield client


@pytest.fixture
def learner(db):
    return get_user_model().objects.create(username="learner", email="l@example.com")


def open_checkout(user):
    request = RequestFactory().get(f"/halyk/checkout/{COURSE}/")
    request.user = user
    return views.checkout(request, str(COURSE))


def paid(user, **extra):
    fields = {
        "invoice_id": "1000001", "user": user, "course_id": COURSE,
        "course_mode": "verified", "amount": 50000, "currency": "KZT",
        "status": PaymentStatus.PAID, "enrolled": True,
    }
    fields.update(extra)
    return Payment.objects.create(**fields)


# -- the page itself ---------------------------------------------------------

def test_the_checkout_page_does_not_wait_for_the_bank(learner, enrollments, for_sale, bank):
    """
    The bank's token used to be fetched before the page was sent, so the learner
    looked at a blank tab for as long as the bank's OAuth server took. It is
    now fetched by the page itself, after it has appeared.
    """
    response = open_checkout(learner)

    assert response.status_code == 200
    assert bank.get_payment_token.call_count == 0
    body = response.content.decode()
    payment = Payment.objects.get(user=learner)
    assert reverse("halyk_payments:payment_object", args=[payment.invoice_id]) in body


def test_the_checkout_page_says_which_course_it_is(learner, enrollments, for_sale, bank):
    with mock.patch.object(views, "_course_name", return_value="Hydrology"):
        body = open_checkout(learner).content.decode()

    assert "Hydrology" in body


def test_a_hostile_course_title_is_shown_not_run(learner, enrollments, for_sale, bank):
    """
    The title used to be written into a <script> block as JSON, where
    "</script>" ends the block and whatever follows runs as the learner.
    """
    evil = '</script><script>alert("x")</script>'
    with mock.patch.object(views, "_course_name", return_value=evil):
        body = open_checkout(learner).content.decode()

    assert evil not in body
    assert "&lt;/script&gt;" in body


def test_someone_already_in_the_course_goes_straight_to_it(learner, enrollments, for_sale, bank):
    enrollments.enroll(learner, COURSE, mode="verified")

    response = open_checkout(learner)

    assert response.status_code == 302
    assert response["Location"] == f"/courses/{COURSE}/course/"
    assert not Payment.objects.filter(user=learner).exists()


# -- leaving and coming back -------------------------------------------------

def test_a_buyer_who_left_the_course_comes_back_without_paying(learner, enrollments, for_sale, bank):
    purchase = paid(learner)
    enrollments.enroll(learner, COURSE, mode="verified")
    enrollments.unenroll(learner, COURSE)

    response = open_checkout(learner)

    assert response.status_code == 302
    assert response["Location"] == f"/courses/{COURSE}/course/"
    assert enrollments.mode_for_user(learner, COURSE) == ("verified", True)
    # No second invoice, and the bank was never asked for anything.
    assert list(Payment.objects.filter(user=learner)) == [purchase]
    assert bank.get_payment_token.call_count == 0


def test_a_buyer_who_left_is_restored_in_the_mode_they_paid_for(learner, enrollments, for_sale, bank):
    """Leaving and coming back must not drop them to the free track."""
    paid(learner)
    enrollments.rows[(learner.pk, str(COURSE))] = ("audit", True)

    open_checkout(learner)

    assert enrollments.mode_for_user(learner, COURSE) == ("verified", True)


def test_after_a_full_refund_the_course_has_to_be_bought_again(learner, enrollments, for_sale, bank):
    """The money came back, so the purchase is over (offer, section 6.8)."""
    paid(learner, status=PaymentStatus.REFUNDED, enrolled=False, refunded_amount=50000)

    response = open_checkout(learner)

    assert response.status_code == 200
    assert Payment.objects.filter(user=learner, status=PaymentStatus.PENDING).count() == 1


def test_a_partial_refund_that_kept_access_still_lets_them_back(learner, enrollments, for_sale, bank):
    paid(learner, refunded_amount=20000)

    response = open_checkout(learner)

    assert response.status_code == 302
    assert enrollments.mode_for_user(learner, COURSE) == ("verified", True)


def test_access_withdrawn_by_hand_is_not_restored(learner, enrollments, for_sale, bank):
    """Paid, then access taken away (a partial refund with --withdraw-access)."""
    paid(learner, enrolled=False, refunded_amount=20000)

    response = open_checkout(learner)

    assert response.status_code == 200
    assert enrollments.mode_for_user(learner, COURSE) == (None, None)


def test_someone_elses_purchase_does_not_let_anybody_in(learner, enrollments, for_sale, bank, db):
    stranger = get_user_model().objects.create(username="stranger", email="s@example.com")
    paid(stranger)

    response = open_checkout(learner)

    assert response.status_code == 200
    assert enrollments.mode_for_user(learner, COURSE) == (None, None)


def test_start_checkout_refuses_a_second_charge_however_it_is_reached(learner, enrollments, for_sale):
    """The backstop behind the view: no path may open a second invoice."""
    paid(learner)

    with pytest.raises(services.CheckoutError):
        services.start_checkout(learner, COURSE)


# -- the payment object ------------------------------------------------------

def fetch_object(user, invoice_id, method="post"):
    request = getattr(RequestFactory(), method)(f"/halyk/payment-object/{invoice_id}/")
    request.user = user
    request.LANGUAGE_CODE = "ru"
    return views.payment_object(request, invoice_id)


@pytest.fixture
def pending(learner, enrollments, for_sale):
    return services.start_checkout(learner, COURSE)


def test_the_payment_object_carries_what_the_widget_needs(learner, pending, bank):
    response = fetch_object(learner, pending.invoice_id)

    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["invoiceId"] == pending.invoice_id
    assert body["amount"] == 50000
    assert body["currency"] == "KZT"
    assert body["auth"] == {"access_token": "tok", "expires_in": 1200}
    assert body["language"] == "RUS"
    bank.get_payment_token.assert_called_once_with(
        invoice_id=pending.invoice_id, amount=50000, currency="KZT",
        secret_hash=pending.secret_hash,
    )


def test_the_secret_never_reaches_the_browser(learner, pending, bank):
    """secret_hash proves a callback came from the bank; the page must not know it."""
    response = fetch_object(learner, pending.invoice_id)

    assert pending.secret_hash not in response.content.decode()


def test_nobody_can_fetch_somebody_elses_payment_object(learner, pending, bank, db):
    from django.http import Http404

    stranger = get_user_model().objects.create(username="stranger", email="s@example.com")
    with pytest.raises(Http404):
        fetch_object(stranger, pending.invoice_id)
    assert bank.get_payment_token.call_count == 0


def test_the_payment_object_is_post_only(learner, pending, bank):
    assert fetch_object(learner, pending.invoice_id, method="get").status_code == 405


def test_a_settled_payment_has_no_payment_object(learner, pending, bank):
    """Paid in another tab: the page is sent to the result instead of the bank."""
    Payment.objects.filter(pk=pending.pk).update(status=PaymentStatus.PAID)

    response = fetch_object(learner, pending.invoice_id)

    assert response.status_code == 409
    assert json.loads(response.content)["result"] == reverse(
        "halyk_payments:result", args=[pending.invoice_id])
    assert bank.get_payment_token.call_count == 0


def test_a_slow_bank_does_not_write_the_invoice_off(learner, pending, bank):
    """
    It used to be marked failed, which put a "not completed" order into the
    learner's history because the bank did not answer in time.
    """
    from halyk_payments.client import HalykError

    bank.get_payment_token.side_effect = HalykError("timeout")

    response = fetch_object(learner, pending.invoice_id)

    assert response.status_code == 502
    pending.refresh_from_db()
    assert pending.status == PaymentStatus.PENDING


def test_the_payment_object_is_behind_csrf(learner, pending, bank):
    """
    It makes the server ask the bank for a token. Another site must not be able
    to make a signed-in learner's browser do that.
    """
    browser = Client(enforce_csrf_checks=True)
    browser.force_login(learner)
    url = reverse("halyk_payments:payment_object", args=[pending.invoice_id])

    assert browser.post(url).status_code == 403
    assert bank.get_payment_token.call_count == 0
