"""
The online session block.

A course team member adds it to a unit and gives it a title. Each block is its
own room: a course can have one permanent block or one per session. The course
team starts the session and joins as moderators; enrolled learners can join once
it is running, under the name in their profile. Everything else about the
session (recording, breakout rooms, polls) happens inside BigBlueButton.

Rendering a unit never calls BigBlueButton, so a problem with that server cannot
slow down or break course pages: the status and recordings arrive afterwards
through the ``status`` handler.
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.template import Context, Template
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext
from web_fragments.fragment import Fragment
from webob import Response
from xblock.completable import XBlockCompletionMode
from xblock.core import XBlock
from xblock.fields import Boolean, Scope, String
from xblock.utils.studio_editable import StudioEditableXBlockMixin

from common.djangoapps.student.models import CourseEnrollment
from common.djangoapps.xblock_django.constants import (
    ATTR_KEY_IS_AUTHENTICATED,
    ATTR_KEY_USER_ID,
    ATTR_KEY_USER_IS_STAFF,
    ATTR_KEY_USERNAME,
)
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

from .client import BigBlueButtonClient, BigBlueButtonError

log = logging.getLogger(__name__)

# Field labels are translated when Studio displays them.
_ = lambda text: text  # pylint: disable=invalid-name

HERE = Path(__file__).parent

# Times are shown in Kazakhstan time (UTC+5 since 2024), where the university is.
LOCAL_TIME = timezone(timedelta(hours=5))

# Many learners can have a unit open at once, and each page asks for the status
# in the background. These keep that from reaching the server every time.
STATUS_CACHE_SECONDS = 15
RECORDINGS_CACHE_SECONDS = 60

# How often the waiting page looks again, while the teacher has not started yet.
WAITING_REFRESH_SECONDS = 15

# BigBlueButton has no Kazakh, so those users get Russian rather than whatever
# their browser asks for.
CLIENT_LOCALES = {"ru": "ru", "kk": "ru", "en": "en"}


def _render(name, context):
    template = Template((HERE / "templates" / name).read_text(encoding="utf-8"))
    return template.render(Context(context))


def _message_page(title, text, status=200, refresh_seconds=None):
    """A small standalone page, shown in the tab the session would have opened in."""
    return Response(
        _render("message.html", {
            "title": title,
            "text": text,
            "language": translation.get_language() or "ru",
            "refresh_seconds": refresh_seconds,
        }),
        status=status,
        content_type="text/html",
        charset="utf-8",
    )


@XBlock.needs("user")
@XBlock.wants("i18n")
class BigBlueButtonBlock(StudioEditableXBlockMixin, XBlock):
    """A BigBlueButton meeting room inside a course unit."""

    # Opening a session says nothing about having learned anything.
    completion_mode = XBlockCompletionMode.EXCLUDED
    has_author_view = True
    icon_class = "other"

    display_name = String(
        display_name=_("Title"),
        help=_("The name of the session. Learners see it above the button."),
        default=_("Online session"),
        scope=Scope.settings,
    )
    schedule = String(
        display_name=_("When"),
        help=_("Optional. Shown to learners as written, for example: Tuesdays at 10:00."),
        default="",
        scope=Scope.settings,
    )
    record = Boolean(
        display_name=_("Allow recording"),
        help=_(
            "The teacher can start and stop recording during the session. "
            "Nothing is recorded until they do. Recordings appear in this block."
        ),
        default=True,
        scope=Scope.settings,
    )

    editable_fields = ("display_name", "schedule", "record")

    # ---- Who is asking ----------------------------------------------------

    def _user(self):
        return self.runtime.service(self, "user").get_current_user()

    def _is_course_team(self):
        return bool(self._user().opt_attrs.get(ATTR_KEY_USER_IS_STAFF))

    def _can_attend(self):
        """The course team, and learners enrolled in the course."""
        attrs = self._user().opt_attrs
        if not attrs.get(ATTR_KEY_IS_AUTHENTICATED):
            return False
        if self._is_course_team():
            return True
        user = get_user_model().objects.filter(id=attrs.get(ATTR_KEY_USER_ID)).first()
        return bool(user) and CourseEnrollment.is_enrolled(user, self._course_key)

    # ---- The meeting ------------------------------------------------------

    @property
    def _usage_key(self):
        """The same key in Studio and in the LMS, whatever branch it was loaded from."""
        return self.scope_ids.usage_id.for_branch(None).version_agnostic()

    @property
    def _course_key(self):
        return self._usage_key.context_key

    @property
    def _title(self):
        # gettext("") returns the catalog's header, so an emptied title gets the default.
        return self.ugettext(self.display_name or self.fields["display_name"].default)

    @property
    def _meeting_id(self):
        """Stable for this block, and different in a re-run of the course."""
        return "ektu-" + hashlib.sha256(str(self._usage_key).encode()).hexdigest()[:40]

    @staticmethod
    def _client():
        """A client, or None if this LMS has no BigBlueButton server configured."""
        url = getattr(settings, "EKTU_BBB_URL", "")
        secret = getattr(settings, "EKTU_BBB_SECRET", "")
        return BigBlueButtonClient(url, secret) if url and secret else None

    def _create_options(self):
        unit_url = settings.LMS_ROOT_URL + reverse(
            "jump_to", args=[str(self._course_key), str(self._usage_key)],
        )
        record = "true" if self.record else "false"
        return {
            "record": record,
            "autoStartRecording": "false",
            "allowStartStopRecording": record,
            "notifyRecordingIsOn": record,
            "muteOnStart": "true",
            "logoutURL": unit_url,
            "meta_bbb-origin": "Open edX",
            "meta_bbb-origin-server-name": "open.ektu.kz",
            "meta_course-id": str(self._course_key),
        }

    def _meeting_name(self):
        overview = CourseOverview.get_from_id(self._course_key)
        return f"{overview.display_name_with_default}: {self._title}"

    def _join_url(self, client, role, create_time):
        attrs = self._user().opt_attrs
        language = (translation.get_language() or "").split("-")[0]
        options = {"errorRedirectUrl": settings.LMS_ROOT_URL}
        if language in CLIENT_LOCALES:
            options["userdata-bbb_override_default_locale"] = CLIENT_LOCALES[language]
        return client.join_url(
            self._meeting_id,
            full_name=self._user().full_name or attrs.get(ATTR_KEY_USERNAME),
            role=role,
            user_id=str(attrs.get(ATTR_KEY_USER_ID)),
            create_time=create_time,
            **options,
        )

    # ---- Handlers ---------------------------------------------------------

    @XBlock.handler
    def join(self, request, suffix=""):  # pylint: disable=unused-argument
        """
        Send the browser into the session.

        The course team starts it if it is not on yet. Learners cannot start one
        by themselves: they get a page that keeps looking until the teacher has.
        """
        if not self._can_attend():
            return _message_page(
                gettext("You cannot join this session"),
                gettext("Sign in with an account enrolled in this course to join the session."),
                status=403,
            )

        client = self._client()
        if client is None:
            return self._unavailable_page()

        try:
            meeting = client.get_meeting(self._meeting_id)
            if self._is_course_team():
                create_time = meeting.create_time if meeting else client.create_meeting(
                    self._meeting_id, self._meeting_name(), **self._create_options()
                )
                url = self._join_url(client, "MODERATOR", create_time)
            elif meeting and meeting.running:
                url = self._join_url(client, "VIEWER", meeting.create_time)
            else:
                return _message_page(
                    self._title,
                    gettext(
                        "The teacher has not started this session yet. "
                        "Keep this page open: it will take you in as soon as the session begins."
                    ),
                    refresh_seconds=WAITING_REFRESH_SECONDS,
                )
        except BigBlueButtonError as error:
            log.warning("Online session %s: %s", self._usage_key, error)
            return self._unavailable_page()

        log.info(
            "Online session %s: user %s joins, course team: %s",
            self._usage_key, self._user().opt_attrs.get(ATTR_KEY_USER_ID), self._is_course_team(),
        )
        return Response(status=302, location=url)

    @XBlock.handler
    def status(self, request, suffix=""):  # pylint: disable=unused-argument
        """Whether the session is on, and its recordings, for the page to show."""
        if not self._can_attend():
            return Response(status=403)
        client = self._client()
        if client is None:
            return self._json({"available": False})
        try:
            return self._json({
                "available": True,
                **self._cached_meeting(client),
                "recordings": self._cached_recordings(client),
            })
        except BigBlueButtonError as error:
            log.warning("Online session %s: %s", self._usage_key, error)
            return self._json({"available": False})

    def _cached_meeting(self, client):
        key = f"ektu_bbb:meeting:{self._meeting_id}"
        state = cache.get(key)
        if state is None:
            meeting = client.get_meeting(self._meeting_id)
            state = {
                "running": bool(meeting and meeting.running),
                "participants": meeting.participants if meeting else 0,
            }
            cache.set(key, state, STATUS_CACHE_SECONDS)
        return state

    def _cached_recordings(self, client):
        key = f"ektu_bbb:recordings:{self._meeting_id}"
        recordings = cache.get(key)
        if recordings is None:
            recordings = [
                {
                    "date": datetime.fromtimestamp(item.start_ms / 1000, LOCAL_TIME).strftime("%d.%m.%Y %H:%M"),
                    "length": gettext("{minutes} min").format(minutes=item.minutes),
                    "url": item.url,
                }
                for item in client.get_recordings(self._meeting_id)
            ]
            cache.set(key, recordings, RECORDINGS_CACHE_SECONDS)
        return recordings

    @staticmethod
    def _json(data):
        return Response(json.dumps(data), content_type="application/json", charset="utf-8")

    @staticmethod
    def _unavailable_page():
        return _message_page(
            gettext("Online sessions are not available right now"),
            gettext("Please try again in a few minutes."),
            status=503,
        )

    # ---- Views ------------------------------------------------------------

    def student_view(self, context=None):
        """What the course team and learners see in the unit."""
        if getattr(self.runtime, "is_author_mode", False):
            return self.author_view(context)

        team = self._is_course_team()
        fragment = Fragment(_render("student.html", {
            "title": self._title,
            "schedule": self.schedule,
            "join_url": self.runtime.handler_url(self, "join"),
            "button": gettext("Start the session") if team else gettext("Join the session"),
            "hint": (
                gettext("You are on the course team, so you join as a moderator. The session opens in a new tab.")
                if team else gettext("The session opens in a new tab.")
            ),
            "recordings_title": gettext("Recordings"),
        }))
        fragment.add_css((HERE / "static" / "block.css").read_text(encoding="utf-8"))
        fragment.add_javascript((HERE / "static" / "block.js").read_text(encoding="utf-8"))
        fragment.initialize_js("EktuBigBlueButton", json_args={
            "running": gettext("In progress"),
            "notRunning": gettext("Not started"),
            "participants": gettext("Participants: {count}"),
            "unavailable": gettext("Online sessions are not available right now"),
            "watch": gettext("Watch"),
            "teamJoin": gettext("Join the session"),
            "isTeam": team,
        })
        return fragment

    def author_view(self, context=None):  # pylint: disable=unused-argument
        """What Studio shows in the unit: nothing there may start a real session."""
        fragment = Fragment(_render("author.html", {
            "title": self._title,
            "schedule": self.schedule,
            "note": gettext(
                "Online session. The course team and learners join it from the published course, not from Studio."
            ),
            "recording": gettext("Recording allowed") if self.record else gettext("Recording not allowed"),
        }))
        fragment.add_css((HERE / "static" / "block.css").read_text(encoding="utf-8"))
        return fragment
