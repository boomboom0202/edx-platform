"""The recording page a learner keeps open during the exam, and what it sends."""
import io
import json

from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import Http404, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from common.djangoapps.edxmako.shortcuts import render_to_response

from . import services, storage
from .conf import conf
from .models import Recording, Session


def _session(token):
    session = Session.objects.select_related("client").filter(token=token).first()
    if not session:
        raise Http404
    return session


def _stream(request):
    """The request body as a stream, also when something has already read it."""
    return io.BytesIO(request.body) if hasattr(request, "_body") else request


def _number(request, name):
    try:
        return max(int(request.GET.get(name, "")), 0)
    except ValueError:
        return None


def _texts():
    return {
        "camera_ok": _("Camera and microphone are on"),
        "camera_failed": _("The camera or the microphone is not available. Allow them in the browser and try again."),
        "screen_ok": _("The entire screen is shared"),
        "screen_failed": _("Screen sharing was not allowed. Try again."),
        "screen_not_entire": _("You shared a window or a tab. Share the entire screen instead."),
        "extra_display": _("A second display is connected. Disconnect it: it is noted in the record."),
        "no_space": _("The recording cannot start right now. Tell your course team."),
        "start_failed": _("The recording could not start. Check the internet connection and try again."),
        "camera_lost": _("The camera stopped. Turn it on again."),
        "screen_lost": _("Screen sharing stopped. Turn it on again."),
        "upload_delayed": _("The connection is unstable. Do not close this tab: the recording is sent when it returns."),
        "saving": _("Saving the recording…"),
        "confirm_stop": _("Stop the recording? Stop only after you have submitted the exam."),
        "leave": _("The recording stops if you close this tab."),
    }


@never_cache
def capture(request, token):
    session = _session(token)
    last = session.recordings.exclude(kind=Recording.PHOTO).aggregate(last=Max("segment"))["last"] or 0
    config = {
        "status": session.status,
        "csrf": get_token(request),
        "segment": last + 1,
        "webcam": session.record_webcam,
        "audio": session.record_audio,
        "screen": session.record_screen,
        "timeslice": conf("TIMESLICE_MS"),
        "bits": {"webcam": conf("WEBCAM_BITS"), "screen": conf("SCREEN_BITS"), "audio": conf("AUDIO_BITS")},
        "urls": {
            name: reverse(f"ektu_proctoring:{name}", args=[session.token])
            for name in ("start", "chunk", "photo", "event", "heartbeat", "finish")
        },
        "text": _texts(),
    }
    return render_to_response("ektu/proctoring_capture.html", {"session": session, "config": config})


@login_required
def resume(request):
    """Send a learner to the recording page of the exam they are taking."""
    session = Session.objects.filter(
        user=request.user, client=None, status__in=(Session.CREATED, Session.RECORDING),
    ).first()
    if not session:
        raise Http404
    return redirect("ektu_proctoring:capture", session.token)


@require_POST
def start(request, token):
    session = _session(token)
    ip = request.META.get("HTTP_CF_CONNECTING_IP") or request.META.get("REMOTE_ADDR")
    try:
        if not services.begin(session, ip, request.META.get("HTTP_USER_AGENT", "")):
            return JsonResponse({"error": "finished"}, status=409)
    except services.NoSpace:
        return JsonResponse({"error": "no_space"}, status=507)
    return JsonResponse({"status": session.status})


@require_POST
def chunk(request, token):
    session = _session(token)
    if not session.is_active:
        return JsonResponse({"error": "not_recording", "stop": True}, status=409)
    kind, segment, seq = request.GET.get("kind"), _number(request, "segment"), _number(request, "seq")
    allowed = {Recording.WEBCAM: session.record_webcam, Recording.SCREEN: session.record_screen}
    if not allowed.get(kind) or not segment or seq is None:
        return JsonResponse({"error": "bad_request"}, status=400)
    try:
        expected = services.append_chunk(
            session, kind, segment, seq, request.content_type or "video/webm",
            _number(request, "elapsed") or 0, _stream(request),
        )
    except storage.ChunkTooLarge:
        return JsonResponse({"error": "too_large"}, status=413)
    services.touch(session)
    return JsonResponse({"next": expected, "stop": session.status == Session.STOPPING})


@require_POST
def photo(request, token):
    session = _session(token)
    if not session.is_active or not session.record_webcam:
        return JsonResponse({"error": "not_recording"}, status=409)
    data = _stream(request).read(conf("MAX_PHOTO_BYTES") + 1)
    if not data.startswith(b"\xff\xd8") or len(data) > conf("MAX_PHOTO_BYTES"):
        return JsonResponse({"error": "bad_request"}, status=400)
    services.save_photo(session, data)
    return JsonResponse({})


@require_POST
def event(request, token):
    session = _session(token)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    if not session.is_active or body.get("kind") not in services.PAGE_EVENTS:
        return JsonResponse({"error": "bad_request"}, status=400)
    services.add_event(session, body["kind"])
    return JsonResponse({})


@require_POST
def heartbeat(request, token):
    session = _session(token)
    if session.is_active:
        services.touch(session)
    return JsonResponse({"status": session.status, "stop": session.status != Session.RECORDING})


@require_POST
def finish(request, token):
    session = _session(token)
    if session.status == Session.RECORDING and not session.exam_finished_at:
        services.add_event(session, "stopped_by_user")
    if session.is_active:
        services.finish(session)
    return JsonResponse({"status": session.status})
