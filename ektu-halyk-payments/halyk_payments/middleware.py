"""
Send learners to our checkout instead of Open edX's track-selection page.

Open edX assumes payment is the ecommerce service's job. Its track-selection
page (``/course_modes/choose/``) therefore ends by sending the learner to
identity verification and never asks for money: without an ecommerce service —
and there is none here — the paid track simply cannot be bought, while the free
track stays one click away.

This middleware closes that gap for courses we sell. It is middleware rather
than a URL override because the plugin's URLs are appended after the platform's,
so a pattern of ours could never win; middleware runs before resolution and
catches the page no matter which flow led to it.

It does not, by itself, make a course paid-only. That takes removing the free
mode from the course — see ``halyk_courses``, which reports the courses where
this has been forgotten.
"""
import logging
import re

from django.shortcuts import redirect

log = logging.getLogger(__name__)

TRACK_SELECTION = re.compile(r"^/course_modes/choose/(?P<course_id>[^/]+)/?$")


class PaidCourseCheckoutMiddleware:
    """Redirect the track-selection page to the Halyk checkout."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        destination = self._checkout_for(request)
        if destination:
            return redirect(destination)
        return self.get_response(request)

    def _checkout_for(self, request):
        """The checkout URL this request should go to instead, or None."""
        from django.conf import settings

        if not getattr(settings, "HALYK_ENABLED", False):
            return None

        match = TRACK_SELECTION.match(request.path)
        if match is None:
            return None

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            # Let the platform's own login redirect happen first.
            return None

        from django.urls import reverse
        from opaque_keys import InvalidKeyError
        from opaque_keys.edx.keys import CourseKey

        from .services import already_enrolled_in_paid_mode, get_paid_mode

        try:
            course_key = CourseKey.from_string(match.group("course_id"))
        except InvalidKeyError:
            return None

        if get_paid_mode(course_key) is None:
            # Not a course we sell; the platform's page is the right one.
            return None

        if already_enrolled_in_paid_mode(user, course_key):
            # Already paid. Open edX sends these learners on to the course.
            return None

        log.info("Sending %s to checkout for %s", user.id, course_key)
        return reverse("halyk_payments:checkout", args=[str(course_key)])
