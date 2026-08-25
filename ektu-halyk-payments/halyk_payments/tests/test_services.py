"""
Tests for the rules that decide who gets access.

These cover the two things that would cost the university money if they broke:
the price must come from the course, and one invoice must grant access once.
Run inside the LMS test settings:

    pytest halyk_payments/tests/test_services.py
"""
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from opaque_keys.edx.keys import CourseKey

from halyk_payments.models import Payment, PaymentStatus
from halyk_payments import services


COURSE = CourseKey.from_string("course-v1:ENV+HYD_01+2022")


@pytest.fixture
def learner(db):
    return get_user_model().objects.create(username="learner", email="l@example.com")


@pytest.fixture
def payment(db, learner):
    return Payment.objects.create(
        invoice_id="123456789012", user=learner, course_id=COURSE,
        course_mode="verified", amount=50000, currency="KZT",
    )


def test_price_comes_from_the_course_not_the_request(db, learner, settings):
    """A learner cannot influence what they are charged."""
    settings.HALYK_COURSE_MODE = "verified"
    settings.HALYK_CURRENCY = "KZT"
    mode = mock.Mock(mode_slug="verified", min_price=50000, currency="kzt")

    with mock.patch.object(services, "get_paid_mode", return_value=mode), \
            mock.patch.object(services, "already_enrolled_in_paid_mode", return_value=False):
        created = services.start_checkout(learner, COURSE)

    assert created.amount == 50000
    assert created.course_mode == "verified"
    assert created.status == PaymentStatus.PENDING
    assert created.enrolled is False


def test_checkout_assigns_an_invoice_number_and_a_secret(db, learner, settings):
    """
    Both are needed before the bank can be called: the number identifies the
    order, and the secret is what makes the callback verifiable.
    """
    settings.HALYK_COURSE_MODE = "verified"
    settings.HALYK_CURRENCY = "KZT"
    mode = mock.Mock(mode_slug="verified", min_price=50000, currency="kzt")

    with mock.patch.object(services, "get_paid_mode", return_value=mode), \
            mock.patch.object(services, "already_enrolled_in_paid_mode", return_value=False):
        created = services.start_checkout(learner, COURSE)

    assert created.invoice_id and created.invoice_id.isdigit()
    assert 6 <= len(created.invoice_id) <= 15
    assert len(created.secret_hash) >= 16


def test_reloading_checkout_does_not_open_a_second_invoice(db, learner, settings):
    """Otherwise every page refresh would leave another order at the bank."""
    settings.HALYK_COURSE_MODE = "verified"
    settings.HALYK_CURRENCY = "KZT"
    mode = mock.Mock(mode_slug="verified", min_price=50000, currency="kzt")

    with mock.patch.object(services, "get_paid_mode", return_value=mode), \
            mock.patch.object(services, "already_enrolled_in_paid_mode", return_value=False):
        first = services.start_checkout(learner, COURSE)
        second = services.start_checkout(learner, COURSE)

    assert first.pk == second.pk
    assert Payment.objects.count() == 1


def test_a_course_priced_in_another_currency_is_refused(db, learner, settings):
    """Charging a dollar price as tenge would undercharge by a factor of 500."""
    settings.HALYK_COURSE_MODE = "verified"
    settings.HALYK_CURRENCY = "KZT"
    mode = mock.Mock(mode_slug="verified", min_price=100, currency="usd")

    with mock.patch.object(services, "get_paid_mode", return_value=mode), \
            mock.patch.object(services, "already_enrolled_in_paid_mode", return_value=False):
        with pytest.raises(services.CheckoutError):
            services.start_checkout(learner, COURSE)


def test_a_course_without_a_price_cannot_be_bought(db, learner):
    with mock.patch.object(services, "get_paid_mode", return_value=None):
        with pytest.raises(services.CheckoutError):
            services.start_checkout(learner, COURSE)


def test_a_repeated_callback_enrolls_only_once(db, payment):
    """The bank may deliver the same notification more than once."""
    with mock.patch("common.djangoapps.student.models.CourseEnrollment.enroll") as enroll:
        services.mark_paid_and_enroll(payment.pk, payload={"code": "ok"})
        services.mark_paid_and_enroll(payment.pk, payload={"code": "ok"})

    assert enroll.call_count == 1
    payment.refresh_from_db()
    assert payment.enrolled is True
    assert payment.status == PaymentStatus.PAID


def test_a_late_failure_does_not_revoke_granted_access(db, payment):
    with mock.patch("common.djangoapps.student.models.CourseEnrollment.enroll"):
        services.mark_paid_and_enroll(payment.pk)

    services.mark_failed(payment.pk, reason="late notice")

    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PAID
    assert payment.enrolled is True


def test_enrolment_grants_the_mode_that_was_paid_for(db, payment):
    with mock.patch("common.djangoapps.student.models.CourseEnrollment.enroll") as enroll:
        services.mark_paid_and_enroll(payment.pk)

    _, kwargs = enroll.call_args
    assert kwargs["mode"] == "verified"
