"""
REST API for other systems that order proctoring from this service.

Every request carries ``Authorization: Bearer <key>``; keys are issued in the
Django admin (API clients). A client sees only its own sessions.
"""
import json
from functools import wraps

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import services
from .models import ApiClient, Session


def api(*methods):
    def decorator(view):
        @csrf_exempt
        @require_http_methods(methods)
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            header = request.META.get("HTTP_AUTHORIZATION", "")
            client = ApiClient.by_key(header[7:].strip()) if header.startswith("Bearer ") else None
            if not client:
                return JsonResponse({"error": "unauthorized"}, status=401)
            try:
                body = json.loads(request.body or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return JsonResponse({"error": "invalid_json"}, status=400)
            key = kwargs.pop("key", None)
            if key is not None:
                session = Session.objects.filter(client=client, key=key).first()
                if not session:
                    return JsonResponse({"error": "not_found"}, status=404)
                return view(request, client, body, session)
            return view(request, client, body)
        return wrapped
    return decorator


def _text(body, name, limit=255):
    value = body.get(name, "")
    return value[:limit] if isinstance(value, str) else ""


@api("POST")
def sessions(request, client, body):
    """Order a session; give ``capture_url`` to the person who takes the exam."""
    return_url = _text(body, "return_url", 500)
    if return_url:
        try:
            URLValidator(schemes=["http", "https"])(return_url)
        except ValidationError:
            return JsonResponse({"error": "invalid_return_url"}, status=400)
    session = services.create_session(
        client=client,
        external_ref=_text(body, "external_ref"),
        subject_name=_text(body, "subject_name"),
        subject_ref=_text(body, "subject_ref"),
        exam_name=_text(body, "exam_name"),
        return_url=return_url,
        record_webcam=body.get("record_webcam") is not False,
        record_audio=body.get("record_audio") is not False,
        record_screen=body.get("record_screen") is not False,
    )
    return JsonResponse(services.as_dict(session, full=True), status=201)


@api("GET")
def session(request, client, body, session):  # pylint: disable=redefined-outer-name
    services.close_if_abandoned(session)
    return JsonResponse(services.as_dict(session, full=True))


@api("POST")
def exam_started(request, client, body, session):  # pylint: disable=redefined-outer-name
    """Mark the moment the exam itself began, for the reviewer's timeline."""
    services.exam_started(session)
    return JsonResponse(services.as_dict(session))


@api("POST")
def finish(request, client, body, session):  # pylint: disable=redefined-outer-name
    """The exam is over: the recording page stops and hands in the rest."""
    services.request_stop(session)
    return JsonResponse(services.as_dict(session))


@api("POST")
def review(request, client, body, session):  # pylint: disable=redefined-outer-name
    verdict = body.get("verdict")
    if verdict not in (Session.PASSED, Session.FAILED):
        return JsonResponse({"error": "invalid_verdict"}, status=400)
    if session.status != Session.FINISHED:
        return JsonResponse({"error": "not_finished"}, status=409)
    services.review(session, verdict, _text(body, "comment", 2000), label=f"API of {client.name}")
    return JsonResponse(services.as_dict(session))
