"""The Open edX side of a session: the exam attempt kept by edx-proctoring."""
import logging

from edx_proctoring.api import get_exam_attempt_by_external_id, update_attempt_status
from edx_proctoring.exceptions import ProctoredBaseException
from edx_proctoring.statuses import ProctoredExamStudentAttemptStatus, ReviewStatus
from edx_proctoring.views import BaseReviewCallback

from .models import Session

log = logging.getLogger(__name__)


def mark_ready(session):
    """Recording runs: let the learner press "Start exam" in the course."""
    attempt = get_exam_attempt_by_external_id(str(session.key))
    if attempt and attempt["status"] in (
        ProctoredExamStudentAttemptStatus.created,
        ProctoredExamStudentAttemptStatus.download_software_clicked,
    ):
        update_attempt_status(attempt["id"], ProctoredExamStudentAttemptStatus.ready_to_start)


def push_review(session):
    """Hand the verdict to edx-proctoring, which marks the attempt verified or rejected."""
    attempt = get_exam_attempt_by_external_id(str(session.key))
    if not attempt or session.verdict == Session.PENDING:
        return
    passed = session.verdict == Session.PASSED
    data = {
        # edx-proctoring counts "suspicious" as failing and "violation" as passing.
        "status": ReviewStatus.passed if passed else ReviewStatus.suspicious,
        "comments": [{
            "comment": session.review_comment[:255],
            "status": "ok" if passed else "suspicious",
        }] if session.review_comment else [],
    }
    if session.reviewed_by:
        data["reviewed_by"] = session.reviewed_by.email
    try:
        BaseReviewCallback().make_review(attempt, data)
    except ProctoredBaseException:
        log.exception("Could not save the review of session %s", session.key)
        raise
