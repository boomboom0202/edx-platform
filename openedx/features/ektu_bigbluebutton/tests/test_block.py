"""Tests for the online session block."""
import json
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import translation
from opaque_keys.edx.locator import BlockUsageLocator, CourseLocator
from webob import Request
from xblock.core import XBlock
from xblock.field_data import DictFieldData
from xblock.fields import ScopeIds
from xblock.reference.user_service import XBlockUser
from xblock.test.tools import TestRuntime

from common.djangoapps.student.tests.factories import UserFactory
from common.djangoapps.xblock_django.constants import (
    ATTR_KEY_IS_AUTHENTICATED,
    ATTR_KEY_USER_ID,
    ATTR_KEY_USER_IS_STAFF,
    ATTR_KEY_USERNAME,
)
from openedx.features.ektu_bigbluebutton.block import BigBlueButtonBlock
from openedx.features.ektu_bigbluebutton.client import BigBlueButtonError, Meeting, Recording

MODULE = "openedx.features.ektu_bigbluebutton.block"
COURSE = CourseLocator("EKTU", "C1", "2026")
BBB = {"EKTU_BBB_URL": "https://bbb.example/bigbluebutton/", "EKTU_BBB_SECRET": "test-secret"}
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def query_of(url):
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


@override_settings(CACHES=LOCMEM, LMS_ROOT_URL="https://lms.example", **BBB)
class BlockTestCase(TestCase):
    """Builds a block as seen by a given user, with the API client replaced."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.user = UserFactory(username="learner1")
        self.bbb = mock.Mock()
        self.bbb.get_meeting.return_value = None
        self.bbb.create_meeting.return_value = "111"
        self.bbb.get_recordings.return_value = []
        self.bbb.join_url.side_effect = lambda *args, **kwargs: "https://bbb.example/join?signed"
        patcher = mock.patch(f"{MODULE}.BigBlueButtonBlock._client", return_value=self.bbb)
        patcher.start()
        self.addCleanup(patcher.stop)
        for target, value in (
            (f"{MODULE}.CourseOverview.get_from_id", mock.Mock(display_name_with_default="Physics")),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def block(self, team=False, signed_in=True, course=COURSE, **fields):
        user = XBlockUser(is_current_user=True, full_name="Тест Слушатель")
        user.opt_attrs.update({
            ATTR_KEY_IS_AUTHENTICATED: signed_in,
            ATTR_KEY_USER_ID: self.user.id,
            ATTR_KEY_USERNAME: self.user.username,
            ATTR_KEY_USER_IS_STAFF: team,
        })
        runtime = TestRuntime(services={
            "user": mock.Mock(get_current_user=lambda: user),
            "i18n": mock.Mock(ugettext=lambda text: text),
        })
        runtime.handler_url = lambda block, name, *args, **kwargs: f"/handler/{name}"
        usage = BlockUsageLocator(course, "bigbluebutton", "session1")
        return BigBlueButtonBlock(
            runtime, field_data=DictFieldData(fields),
            scope_ids=ScopeIds("user", "bigbluebutton", usage, usage),
        )

    def handle(self, block, handler):
        return block.handle(handler, Request.blank("/"))

    @staticmethod
    def enrolled(value=True):
        return mock.patch(f"{MODULE}.CourseEnrollment.is_enrolled", return_value=value)


class JoinTest(BlockTestCase):

    def test_course_team_starts_a_session(self):
        response = self.handle(self.block(team=True, display_name="Lecture 1"), "join")
        assert response.status_code == 302
        assert response.location == "https://bbb.example/join?signed"
        args, kwargs = self.bbb.create_meeting.call_args
        assert args[1] == "Physics: Lecture 1"
        assert kwargs["record"] == "true"
        assert kwargs["autoStartRecording"] == "false"
        assert kwargs["logoutURL"].startswith("https://lms.example/courses/course-v1:EKTU+C1+2026/jump_to/")
        assert self.bbb.join_url.call_args.kwargs["role"] == "MODERATOR"
        assert self.bbb.join_url.call_args.kwargs["create_time"] == "111"

    def test_course_team_joins_a_running_session_without_recreating_it(self):
        self.bbb.get_meeting.return_value = Meeting(True, 4, "222")
        response = self.handle(self.block(team=True), "join")
        assert response.status_code == 302
        self.bbb.create_meeting.assert_not_called()
        assert self.bbb.join_url.call_args.kwargs["create_time"] == "222"

    def test_recording_can_be_switched_off(self):
        self.handle(self.block(team=True, record=False), "join")
        kwargs = self.bbb.create_meeting.call_args.kwargs
        assert kwargs["record"] == "false"
        assert kwargs["allowStartStopRecording"] == "false"

    def test_learner_joins_under_their_profile_name(self):
        self.bbb.get_meeting.return_value = Meeting(True, 4, "222")
        with self.enrolled():
            response = self.handle(self.block(), "join")
        assert response.status_code == 302
        kwargs = self.bbb.join_url.call_args.kwargs
        assert kwargs["role"] == "VIEWER"
        assert kwargs["full_name"] == "Тест Слушатель"
        assert kwargs["user_id"] == str(self.user.id)

    def test_learner_cannot_start_a_session(self):
        with self.enrolled():
            response = self.handle(self.block(), "join")
        assert response.status_code == 200
        assert 'http-equiv="refresh"' in response.text
        self.bbb.create_meeting.assert_not_called()
        self.bbb.join_url.assert_not_called()

    def test_learner_waits_until_the_teacher_is_really_in(self):
        self.bbb.get_meeting.return_value = Meeting(False, 0, "222")
        with self.enrolled():
            response = self.handle(self.block(), "join")
        assert response.status_code == 200
        self.bbb.join_url.assert_not_called()

    def test_not_enrolled_is_refused(self):
        self.bbb.get_meeting.return_value = Meeting(True, 4, "222")
        with self.enrolled(False):
            response = self.handle(self.block(), "join")
        assert response.status_code == 403
        self.bbb.join_url.assert_not_called()

    def test_anonymous_is_refused(self):
        self.bbb.get_meeting.return_value = Meeting(True, 4, "222")
        response = self.handle(self.block(signed_in=False), "join")
        assert response.status_code == 403
        self.bbb.join_url.assert_not_called()

    def test_server_down_gives_a_page_not_a_crash(self):
        self.bbb.get_meeting.side_effect = BigBlueButtonError("getMeetingInfo: ConnectionError")
        response = self.handle(self.block(team=True), "join")
        assert response.status_code == 503
        assert "<h1>" in response.text

    def test_not_configured_gives_a_page_not_a_crash(self):
        with mock.patch(f"{MODULE}.BigBlueButtonBlock._client", return_value=None):
            assert self.handle(self.block(team=True), "join").status_code == 503

    def test_client_language(self):
        self.bbb.get_meeting.return_value = Meeting(True, 4, "222")
        for language, expected in (("ru", "ru"), ("kk", "ru"), ("en", "en")):
            with translation.override(language):
                self.handle(self.block(team=True), "join")
            assert self.bbb.join_url.call_args.kwargs["userdata-bbb_override_default_locale"] == expected


class MeetingIdTest(BlockTestCase):

    def test_same_for_every_branch_and_different_in_a_rerun(self):
        published = self.block(course=COURSE.for_branch("published-branch"))
        draft = self.block(course=COURSE.for_branch("draft-branch"))
        rerun = self.block(course=CourseLocator("EKTU", "C1", "2027"))
        assert published._meeting_id == draft._meeting_id  # pylint: disable=protected-access
        assert published._meeting_id != rerun._meeting_id  # pylint: disable=protected-access


class StatusTest(BlockTestCase):

    def status(self, block):
        response = self.handle(block, "status")
        assert response.status_code == 200
        return json.loads(response.text)

    def test_status_with_recordings(self):
        self.bbb.get_meeting.return_value = Meeting(True, 4, "222")
        self.bbb.get_recordings.return_value = [Recording(1_700_000_000_000, 12, "https://bbb/rec")]
        with self.enrolled():
            data = self.status(self.block())
        assert data["available"] is True
        assert data["running"] is True
        assert data["participants"] == 4
        assert data["recordings"][0]["url"] == "https://bbb/rec"
        assert data["recordings"][0]["date"] == "15.11.2023 03:13"  # shown in UTC+5

    def test_recordings_are_for_enrolled_people_only(self):
        self.bbb.get_recordings.return_value = [Recording(1_700_000_000_000, 12, "https://bbb/rec")]
        with self.enrolled(False):
            response = self.handle(self.block(), "status")
        assert response.status_code == 403
        assert "bbb/rec" not in response.text

    def test_status_is_cached_for_many_open_pages(self):
        with self.enrolled():
            for _ in range(5):
                self.status(self.block())
        assert self.bbb.get_meeting.call_count == 1
        assert self.bbb.get_recordings.call_count == 1

    def test_server_down_is_reported_not_raised(self):
        self.bbb.get_meeting.side_effect = BigBlueButtonError("down")
        with self.enrolled():
            assert self.status(self.block()) == {"available": False}


class ViewsTest(BlockTestCase):

    def test_rendering_a_unit_never_calls_the_server(self):
        fragment = self.block(team=True, schedule="Вторник, 10:00").student_view()
        assert "/handler/join" in fragment.content
        assert "Вторник, 10:00" in fragment.content
        assert not self.bbb.method_calls

    def test_titles_are_escaped(self):
        fragment = self.block(team=True, display_name="<script>alert(1)</script>").student_view()
        assert "<script>alert(1)" not in fragment.content

    def test_learner_and_team_see_different_buttons(self):
        assert "Start the session" in self.block(team=True).student_view().content
        assert "Join the session" in self.block().student_view().content

    def test_empty_title_falls_back_to_the_default(self):
        assert "Online session" in self.block(display_name="").student_view().content

    def test_studio_never_offers_a_real_session(self):
        block = self.block(team=True)
        block.runtime.is_author_mode = True
        assert "/handler/join" not in block.student_view().content
        assert "/handler/join" not in block.author_view().content

    def test_registered_as_an_xblock(self):
        assert XBlock.load_class("bigbluebutton") is BigBlueButtonBlock
