"""
The edx-proctoring backend: how Open edX orders sessions from this service.

Registered through the ``openedx.proctoring`` entry point in setup.py and
chosen with ``PROCTORING_BACKENDS``. Studio loads this module too, where the
app itself is not installed, so models are imported only inside the methods
the LMS calls.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.translation import gettext as _
from edx_proctoring.backends.backend import ProctoringBackendProvider
from edx_proctoring.statuses import SoftwareSecureReviewStatus

log = logging.getLogger(__name__)


class EktuProctoringBackend(ProctoringBackendProvider):
    verbose_name = "EKTU Proctoring"
    passing_statuses = (SoftwareSecureReviewStatus.clean,)
    has_dashboard = True
    supports_onboarding = False

    @staticmethod
    def _session(external_id):
        from .models import Session  # pylint: disable=import-outside-toplevel
        return Session.objects.filter(key=external_id).first() if external_id else None

    def register_exam_attempt(self, exam, context):
        from . import services  # pylint: disable=import-outside-toplevel

        session = services.create_session(
            external_ref=context["attempt_code"],
            user=get_user_model().objects.filter(email=context.get("email") or "").first(),
            subject_name=context.get("full_name", ""),
            course_id=exam["course_id"],
            exam_name=exam["exam_name"],
        )
        return str(session.key)

    def get_attempt(self, attempt):
        from . import services  # pylint: disable=import-outside-toplevel

        session = self._session(attempt.get("external_id"))
        return {"download_url": services.capture_url(session)} if session else {}

    def get_software_download_url(self):
        return settings.LMS_ROOT_URL.rstrip("/") + reverse("ektu_proctoring:resume")

    def start_exam_attempt(self, exam, attempt):
        from . import services  # pylint: disable=import-outside-toplevel

        # A fault here must never keep a learner from starting or submitting an exam.
        try:
            session = self._session(attempt)
            if session:
                services.exam_started(session)
        except Exception:  # pylint: disable=broad-except
            log.exception("Could not mark the exam start of session %s", attempt)

    def stop_exam_attempt(self, exam, attempt):
        self._stop(attempt, "exam_finished")

    def mark_erroneous_exam_attempt(self, exam, attempt):
        self._stop(attempt, "attempt_error")

    def remove_exam_attempt(self, exam, attempt):
        # The recording stays as evidence until the retention period ends.
        self._stop(attempt, "attempt_removed")
        return True

    def _stop(self, external_id, reason):
        from . import services  # pylint: disable=import-outside-toplevel

        try:
            session = self._session(external_id)
            if session:
                services.request_stop(session, reason)
        except Exception:  # pylint: disable=broad-except
            log.exception("Could not stop session %s (%s)", external_id, reason)

    def on_review_callback(self, attempt, payload):
        return payload

    def on_exam_saved(self, exam):
        return None

    # pylint: disable=unused-argument,too-many-positional-arguments
    def get_instructor_url(
        self, course_id, user, exam_id=None, attempt_id=None,
        show_configuration_dashboard=False, encrypted_video_review_url=None,
    ):
        url = settings.LMS_ROOT_URL.rstrip("/") + reverse("ektu_proctoring:review_list")
        url += f"?course_id={course_id}"
        return url + (f"&attempt={attempt_id}" if attempt_id else "")

    def get_proctoring_config(self):
        return {
            "name": self.verbose_name,
            "instructions": [
                _("Use a computer with a webcam and a microphone, and the Chrome, Edge, Firefox or Yandex browser."),
                _("Select Start System Check. A recording page opens in a new tab."),
                _("On that page, agree to the recording, allow the camera and the microphone, "
                  "and share your entire screen."),
                _("Keep the recording tab open, come back to this tab and select Start Exam."),
                _("The recording stops by itself when you submit the exam."),
            ],
        }
