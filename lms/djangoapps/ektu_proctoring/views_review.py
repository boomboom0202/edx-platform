"""Where staff watch a recording and decide whether the exam counts."""
from datetime import timedelta, timezone

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache

from common.djangoapps.edxmako.shortcuts import render_to_response
from common.djangoapps.student.models import CourseAccessRole
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

from . import services, storage
from .models import Recording, Session

PER_PAGE = 50
# The same clock as the course log (ektu_audit).
LOCAL_TIME = timezone(timedelta(hours=5), "UTC+5")


def event_label(kind):
    return {
        "started": _("Recording started"),
        "exam_started": _("Exam started"),
        "exam_finished": _("Exam submitted"),
        "finished": _("Recording finished"),
        "camera_lost": _("Camera stopped"),
        "camera_restored": _("Camera turned on again"),
        "microphone_lost": _("Microphone stopped"),
        "screen_lost": _("Screen sharing stopped"),
        "screen_restored": _("Screen sharing turned on again"),
        "extra_display": _("A second display is connected"),
        "page_reloaded": _("Recording page reopened"),
        "upload_delayed": _("Unstable connection"),
        "gap": _("No recording received"),
        "stopped_by_user": _("Recording stopped by the learner before the exam ended"),
        "page_closed_before_end": _("Recording page was closed before the exam ended"),
        "never_recorded": _("Nothing was recorded"),
        "attempt_error": _("Exam attempt ended with an error"),
        "attempt_removed": _("Exam attempt was removed by staff"),
        "abandoned": _("Recording page went silent; session closed"),
    }.get(kind, kind)


def status_label(status):
    return {
        Session.CREATED: _("Recording not started"),
        Session.RECORDING: _("Recording in progress"),
        Session.STOPPING: _("Finishing"),
        Session.FINISHED: _("Recording finished"),
    }[status]


def verdict_label(verdict):
    return {
        Session.PENDING: _("Not reviewed"),
        Session.PASSED: _("Exam accepted"),
        Session.FAILED: _("Rules were broken"),
    }[verdict]


def reviewable_courses(user):
    """Course ids whose sessions the user may review; None means every session."""
    if user.is_staff or user.is_superuser:
        return None
    return {
        str(course_id) for course_id in CourseAccessRole.objects.filter(
            user=user, role__in=("staff", "instructor"),
        ).values_list("course_id", flat=True) if course_id
    }


def can_review(user, session):
    if not user.is_authenticated:
        return False
    courses = reviewable_courses(user)
    return courses is None or (not session.client_id and session.course_id in courses)


def local(moment):
    return moment.astimezone(LOCAL_TIME) if moment else None


@never_cache
def review_list(request):
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    courses = reviewable_courses(request.user)
    if courses is not None and not courses:
        raise PermissionDenied
    sessions = Session.objects.select_related("user", "client")
    if courses is not None:
        sessions = sessions.filter(client=None, course_id__in=courses)

    attempt = request.GET.get("attempt", "")
    if attempt:
        try:
            found = sessions.filter(key=attempt).first()
        except (ValueError, TypeError):
            found = None
        if found:
            return redirect("ektu_proctoring:review", found.key)

    chosen = {name: request.GET.get(name, "") for name in ("course_id", "verdict", "status")}
    course_ids = sorted(set(sessions.exclude(course_id="").values_list("course_id", flat=True)))
    names = dict(CourseOverview.objects.filter(id__in=course_ids).values_list("id", "display_name"))
    names = {str(key): name for key, name in names.items()}
    if chosen["course_id"]:
        sessions = sessions.filter(course_id=chosen["course_id"])
    if chosen["verdict"] in dict(Session.VERDICTS):
        sessions = sessions.filter(verdict=chosen["verdict"])
    if chosen["status"] in dict(Session.STATUSES):
        sessions = sessions.filter(status=chosen["status"])
    sessions = sessions.annotate(
        warnings=Count("events", filter=Q(events__kind__in=services.WARNINGS)),
    )
    page = Paginator(sessions, PER_PAGE).get_page(request.GET.get("page"))
    query = "&".join(f"{name}={value}" for name, value in chosen.items() if value)
    return render_to_response("ektu/proctoring_review.html", {
        "view": "list",
        "page": page,
        "rows": [{
            "session": session,
            "url": reverse("ektu_proctoring:review", args=[session.key]),
            "who": session.subject_name or (session.user.username if session.user else session.subject_ref),
            "where": names.get(session.course_id) or (session.client.name if session.client else ""),
            "created": local(session.created),
            "status": status_label(session.status),
            "verdict": verdict_label(session.verdict),
        } for session in page],
        "chosen": chosen,
        "query": query,
        "courses": [(course_id, names.get(course_id, course_id)) for course_id in course_ids],
        "verdicts": [(value, verdict_label(value)) for value, _label in Session.VERDICTS],
        "statuses": [(value, status_label(value)) for value, _label in Session.STATUSES],
    })


def _authorize(request, session):
    """Return the access suffix for links, or raise/redirect when the viewer may not look."""
    access = request.GET.get("access", "")
    if can_review(request.user, session):
        return ""
    if access and services.has_review_access(session, access):
        return "?access=" + access
    if not request.user.is_authenticated:
        return None
    raise PermissionDenied


@never_cache
def review(request, key):
    session = get_object_or_404(Session.objects.select_related("user", "client", "reviewed_by"), key=key)
    suffix = _authorize(request, session)
    if suffix is None:
        return redirect_to_login(request.get_full_path())
    services.close_if_abandoned(session)

    if request.method == "POST":
        verdict = request.POST.get("verdict")
        if session.status != Session.FINISHED or verdict not in (Session.PASSED, Session.FAILED):
            raise PermissionDenied
        by_staff = not suffix
        services.review(
            session, verdict, request.POST.get("comment", "").strip()[:2000],
            reviewer=request.user if by_staff else None,
            label="" if by_staff else f"link of {session.client.name}",
        )
        return redirect(reverse("ektu_proctoring:review", args=[session.key]) + suffix)

    recordings = list(session.recordings.all())
    media = [rec for rec in recordings if rec.kind != Recording.PHOTO]
    origin = min((rec.started_at for rec in media), default=session.started_at or session.created)

    def source(rec):
        return {
            "url": reverse("ektu_proctoring:media", args=[session.key, rec.pk]) + suffix,
            "start": int((rec.started_at - origin).total_seconds() * 1000),
        }

    photo = next((rec for rec in recordings if rec.kind == Recording.PHOTO), None)
    course = CourseOverview.objects.filter(id=session.course_id).first() if session.course_id else None
    return render_to_response("ektu/proctoring_review.html", {
        "view": "detail",
        "session": session,
        "who": session.subject_name or (session.user.username if session.user else session.subject_ref),
        "username": session.user.username if session.user else "",
        "where": course.display_name if course else (session.client.name if session.client else ""),
        "status": status_label(session.status),
        "verdict": verdict_label(session.verdict),
        "created": local(session.created),
        "reviewed_at": local(session.reviewed_at),
        "reviewer": session.reviewed_by.username if session.reviewed_by else session.reviewer_label,
        "photo_url": reverse("ektu_proctoring:media", args=[session.key, photo.pk]) + suffix if photo else "",
        "player": {
            kind: [source(rec) for rec in sorted(media, key=lambda rec: rec.started_at) if rec.kind == kind]
            for kind in (Recording.SCREEN, Recording.WEBCAM)
        },
        "indexed": all(rec.seekable for rec in media),
        "events": [{
            "label": event_label(event.kind),
            "warning": event.kind in services.WARNINGS,
            "at": local(event.at),
            "offset": int((event.at - origin).total_seconds() * 1000),
            "seconds": event.details.get("seconds"),
        } for event in session.events.all()],
        "can_decide": session.status == Session.FINISHED,
        "list_url": "" if suffix else reverse("ektu_proctoring:review_list"),
        "has_media": bool(media),
    })


def media(request, key, recording_id):
    session = get_object_or_404(Session, key=key)
    if _authorize(request, session) is None:
        raise PermissionDenied
    recording = get_object_or_404(Recording, pk=recording_id, session=session)
    path = storage.absolute(recording.path)
    if not path.is_file():
        raise Http404
    return storage.ranged_response(request, path, recording.mime.split(";")[0] or "video/webm")
