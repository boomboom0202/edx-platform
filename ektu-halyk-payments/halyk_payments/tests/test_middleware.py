"""
Tests for the redirect that puts a price in front of a paid course.

Without it, Open edX's track-selection page walks the learner through identity
verification and never asks for money.
"""
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from opaque_keys.edx.keys import CourseKey

from halyk_payments import middleware as mw
from halyk_payments.middleware import PaidCourseCheckoutMiddleware


COURSE = "course-v1:ENV+HYD_01+2022"
CHOOSE_URL = f"/course_modes/choose/{COURSE}/"


@pytest.fixture(autouse=True)
def enabled(settings):
    settings.HALYK_ENABLED = True
    return settings


@pytest.fixture
def learner(db):
    return get_user_model().objects.create(username="learner", email="l@example.com")


def run(path, user, for_sale=True, already_paid=False):
    """Send one request through the middleware and report where it ended up."""
    request = RequestFactory().get(path)
    request.user = user
    downstream = mock.Mock(return_value="passed through")

    mode = mock.Mock(mode_slug="verified", min_price=50000) if for_sale else None
    with mock.patch("halyk_payments.services.get_paid_mode", return_value=mode), \
            mock.patch("halyk_payments.services.already_enrolled_in_paid_mode",
                       return_value=already_paid):
        return PaidCourseCheckoutMiddleware(downstream)(request)


def test_a_paid_course_sends_the_learner_to_checkout(learner):
    response = run(CHOOSE_URL, learner)

    assert response.status_code == 302
    assert response["Location"] == f"/halyk/checkout/{COURSE}/"


def test_a_free_course_keeps_the_platforms_own_page(learner):
    assert run(CHOOSE_URL, learner, for_sale=False) == "passed through"


def test_someone_who_already_paid_is_not_asked_again(learner):
    """Open edX sends these learners on to the course; leave that alone."""
    assert run(CHOOSE_URL, learner, already_paid=True) == "passed through"


def test_an_anonymous_visitor_is_left_to_the_login_redirect():
    assert run(CHOOSE_URL, AnonymousUser()) == "passed through"


def test_other_pages_are_untouched(learner):
    for path in ("/dashboard", f"/courses/{COURSE}/about", "/course_modes/choose/"):
        assert run(path, learner) == "passed through"


def test_a_malformed_course_id_is_left_alone(learner):
    assert run("/course_modes/choose/not-a-course-key/", learner) == "passed through"


def test_nothing_is_redirected_when_payments_are_off(learner, enabled):
    enabled.HALYK_ENABLED = False
    assert run(CHOOSE_URL, learner) == "passed through"


def test_the_course_id_survives_the_redirect(learner):
    """Course keys carry ':' and '+', which URL building can mangle."""
    response = run(CHOOSE_URL, learner)
    key = CourseKey.from_string(response["Location"].split("/checkout/")[1].rstrip("/"))
    assert str(key) == COURSE


def test_the_pattern_only_matches_the_track_selection_page():
    assert mw.TRACK_SELECTION.match(CHOOSE_URL)
    assert mw.TRACK_SELECTION.match(f"/course_modes/choose/{COURSE}")
    assert not mw.TRACK_SELECTION.match(f"/course_modes/choose/{COURSE}/extra/")
