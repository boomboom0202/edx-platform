"""What can happen to a session, whoever ordered it."""
import logging
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import storage
from .conf import conf
from .models import Event, Recording, Session

log = logging.getLogger(__name__)

REVIEW_SALT = "ektu-proctoring-review"

# What the recording page may report by itself.
PAGE_EVENTS = {
    "camera_lost", "camera_restored", "microphone_lost", "screen_lost", "screen_restored",
    "extra_display", "upload_delayed",
}
# Worth a reviewer's attention, as opposed to the normal course of a session.
WARNINGS = PAGE_EVENTS - {"camera_restored", "screen_restored"} | {
    "gap", "page_reloaded", "stopped_by_user", "page_closed_before_end", "never_recorded",
    "attempt_error", "attempt_removed", "abandoned",
}


class NoSpace(Exception):
    pass


def absolute_url(path):
    return settings.LMS_ROOT_URL.rstrip("/") + path


def capture_url(session):
    return absolute_url(reverse("ektu_proctoring:capture", args=[session.token]))


def review_url(session, signed=False):
    url = absolute_url(reverse("ektu_proctoring:review", args=[session.key]))
    if signed:
        url += "?access=" + signing.dumps(str(session.key), salt=REVIEW_SALT)
    return url


def has_review_access(session, access):
    """Whether ``access`` is an unexpired link signature for this session."""
    try:
        key = signing.loads(access, salt=REVIEW_SALT, max_age=conf("REVIEW_LINK_HOURS") * 3600)
    except signing.BadSignature:
        return False
    return key == str(session.key)


def add_event(session, kind, **details):
    return Event.objects.create(session=session, kind=kind, details=details)


def create_session(**fields):
    return Session.objects.create(**fields)


def begin(session, ip=None, user_agent=""):
    """The person agreed and the recorders are about to start."""
    if session.status == Session.FINISHED:
        return False
    if storage.free_mb() < conf("MIN_FREE_MB"):
        log.error("Proctoring storage is nearly full: %s MB free", storage.free_mb())
        raise NoSpace()
    now = timezone.now()
    first = session.status == Session.CREATED
    if first:
        session.status = Session.RECORDING
        session.consent_at = session.started_at = now
        session.ip, session.user_agent = ip, user_agent[:400]
    session.last_seen = now
    session.save()
    add_event(session, "started" if first else "page_reloaded")
    if first:
        notify(session, "ready")
    return True


def touch(session):
    """Note that the recording page is alive; a long silence becomes an event."""
    now = timezone.now()
    if session.last_seen and session.status == Session.RECORDING:
        silent = (now - session.last_seen).total_seconds()
        if silent > conf("GAP_SECONDS"):
            add_event(session, "gap", seconds=int(silent))
    Session.objects.filter(pk=session.pk).update(last_seen=now)
    session.last_seen = now


def append_chunk(session, kind, segment, seq, mime, elapsed_ms, stream):
    """
    Store one piece of a recording. Returns the piece the server expects next,
    which differs from ``seq + 1`` when the page has to resend something.
    """
    with transaction.atomic():
        recording, created = Recording.objects.select_for_update().get_or_create(
            session=session, kind=kind, segment=segment,
            defaults={
                "path": f"{storage.relative_dir(session)}/{kind}-{segment}.webm",
                "mime": mime[:80],
                "started_at": timezone.now() - timedelta(milliseconds=max(elapsed_ms, 0)),
            },
        )
        if created:
            # get_or_create does not lock the row it has just inserted.
            recording = Recording.objects.select_for_update().get(pk=recording.pk)
        if seq == recording.next_seq:
            recording.size += storage.append(recording, stream, conf("MAX_CHUNK_BYTES"))
            recording.next_seq += 1
            recording.save(update_fields=["size", "next_seq"])
        return recording.next_seq


def save_photo(session, data):
    path = f"{storage.relative_dir(session)}/photo.jpg"
    target = storage.absolute(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    Recording.objects.update_or_create(
        session=session, kind=Recording.PHOTO, segment=1,
        defaults={"path": path, "mime": "image/jpeg", "size": len(data), "started_at": timezone.now()},
    )


def exam_started(session):
    if not session.exam_started_at:
        session.exam_started_at = timezone.now()
        session.save(update_fields=["exam_started_at"])
        add_event(session, "exam_started")


def request_stop(session, reason="exam_finished"):
    """The exam is over: ask the page to hand in what is left and stop."""
    from .tasks import force_finish  # pylint: disable=import-outside-toplevel

    if not session.exam_finished_at:
        session.exam_finished_at = timezone.now()
    add_event(session, reason)
    if session.status == Session.RECORDING:
        session.status = Session.STOPPING
        session.save(update_fields=["status", "exam_finished_at"])
        gone = not session.last_seen or (
            timezone.now() - session.last_seen
        ).total_seconds() > conf("GAP_SECONDS")
        if gone:
            add_event(session, "page_closed_before_end")
        transaction.on_commit(lambda: _later(force_finish, session.pk, conf("STOP_GRACE_SECONDS")))
    elif session.status == Session.CREATED:
        # The exam ended although nothing was ever recorded.
        session.save(update_fields=["exam_finished_at"])
        add_event(session, "never_recorded")
        finish(session)
    else:
        session.save(update_fields=["exam_finished_at"])


def finish(session):
    from .tasks import finalize_session  # pylint: disable=import-outside-toplevel

    if session.status == Session.FINISHED:
        return
    session.status = Session.FINISHED
    session.finished_at = timezone.now()
    session.save(update_fields=["status", "finished_at"])
    add_event(session, "finished")
    transaction.on_commit(lambda: _later(finalize_session, session.pk, 0))
    notify(session, "finished")


def close_if_abandoned(session):
    """Close a session whose page has been silent for long, e.g. the exam was never submitted."""
    silent_since = session.last_seen or session.created
    if session.is_active and (timezone.now() - silent_since).total_seconds() > 10 * conf("GAP_SECONDS"):
        add_event(session, "abandoned")
        finish(session)


def review(session, verdict, comment="", reviewer=None, label=""):
    session.verdict = verdict
    session.review_comment = comment
    session.reviewed_by = reviewer
    session.reviewer_label = label[:255]
    session.reviewed_at = timezone.now()
    session.save()
    notify(session, "reviewed")


def purge(session):
    """Delete the files and keep the record of what happened."""
    storage.delete_files(session)
    session.recordings.all().delete()
    session.purged_at = timezone.now()
    session.save(update_fields=["purged_at"])


def notify(session, what):
    """Tell whoever ordered the session that it changed."""
    from . import edx  # pylint: disable=import-outside-toplevel
    from .tasks import send_webhook  # pylint: disable=import-outside-toplevel

    if session.client_id:
        if session.client.webhook_url:
            transaction.on_commit(lambda: _later(send_webhook, session.pk, 0, what))
    elif what == "ready":
        edx.mark_ready(session)
    elif what == "reviewed":
        edx.push_review(session)


def _later(task, session_id, countdown, *args):
    """Queue a task; do the work right here when the queue cannot be reached."""
    try:
        task.apply_async((session_id, *args), countdown=countdown)
    except Exception:  # pylint: disable=broad-except
        log.exception("Could not queue %s, running it now", task.name)
        task(session_id, *args)


def as_dict(session, full=False):
    """The session as the REST API and webhooks show it."""
    def stamp(moment):
        return moment.isoformat() if moment else None

    data = {
        "id": str(session.key),
        "external_ref": session.external_ref,
        "status": session.status,
        "verdict": session.verdict,
        "subject_name": session.subject_name,
        "subject_ref": session.subject_ref,
        "exam_name": session.exam_name,
        "created": stamp(session.created),
        "started_at": stamp(session.started_at),
        "exam_started_at": stamp(session.exam_started_at),
        "exam_finished_at": stamp(session.exam_finished_at),
        "finished_at": stamp(session.finished_at),
        "review_comment": session.review_comment,
        "reviewed_at": stamp(session.reviewed_at),
        "recordings_deleted_at": stamp(session.purged_at),
    }
    if full:
        data["capture_url"] = capture_url(session)
        data["review_url"] = review_url(session, signed=True)
        data["events"] = [
            {"kind": event.kind, "at": stamp(event.at), "details": event.details}
            for event in session.events.all()
        ]
        data["recordings"] = [
            {"kind": rec.kind, "segment": rec.segment, "size": rec.size,
             "started_at": stamp(rec.started_at), "duration_ms": rec.duration_ms}
            for rec in session.recordings.all()
        ]
    return data
