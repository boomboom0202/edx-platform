"""Tests for the EKTU proctoring service."""
import hashlib
import hmac
import io
import json
import tempfile
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from edx_proctoring.api import (
    create_exam,
    create_exam_attempt,
    get_exam_attempt_by_id,
    get_exam_attempt_data,
    update_attempt_status,
)
from edx_proctoring.runtime import get_runtime_service, set_runtime_service
from edx_proctoring.statuses import ProctoredExamStudentAttemptStatus as Status
from edx_proctoring.tests.test_services import (
    MockCertificateService,
    MockCreditService,
    MockGradesService,
    MockInstructorService,
)
from opaque_keys.edx.keys import CourseKey

from common.djangoapps.student.roles import CourseStaffRole
from common.djangoapps.student.tests.factories import UserFactory
from lms.djangoapps.ektu_proctoring import services, storage, tasks
from lms.djangoapps.ektu_proctoring.backend import EktuProctoringBackend
from lms.djangoapps.ektu_proctoring.models import ApiClient, Event, Recording, Session

COURSE = "course-v1:EKTU+PR101+2026"
PASSWORD = "Password1234"
WEBM = "video/webm"


class ProctoringTestCase(TestCase):
    """Keeps recordings in a temporary folder."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(folder.cleanup)
        settings = override_settings(EKTU_PROCTORING={"ROOT": folder.name, "MIN_FREE_MB": 0})
        settings.enable()
        self.addCleanup(settings.disable)

    def url(self, name, session):
        return reverse(f"ektu_proctoring:{name}", args=[session.token])

    def send(self, session, seq, data=b"piece", kind="webcam", segment=1):
        return self.client.post(
            self.url("chunk", session) + f"?kind={kind}&segment={segment}&seq={seq}&elapsed=1000",
            data=data, content_type=WEBM,
        )

    def sign_in(self, user):
        assert self.client.login(username=user.username, password=PASSWORD)

    def kinds(self, session):
        return list(session.events.values_list("kind", flat=True))


class CapturePageTest(ProctoringTestCase):
    """The recording page and what it sends."""

    def setUp(self):
        super().setUp()
        self.session = services.create_session(exam_name="Final exam", subject_name="Aigerim")

    def test_page_opens_without_an_account(self):
        response = self.client.get(self.url("capture", self.session))
        assert response.status_code == 200
        assert "Final exam" in response.content.decode()
        assert self.client.get(reverse("ektu_proctoring:capture", args=["nope"])).status_code == 404

    def test_help_page_is_open_to_everyone(self):
        response = self.client.get(reverse("ektu_proctoring:help"))
        assert response.status_code == 200
        assert reverse("ektu_proctoring:resume") in response.content.decode()

    def test_pieces_are_appended_in_order(self):
        assert self.send(self.session, 0).status_code == 409  # nothing is recorded before the start
        assert self.client.post(self.url("start", self.session)).json() == {"status": "recording"}

        assert self.send(self.session, 0, b"one-").json() == {"next": 1, "stop": False}
        # A piece sent twice is stored once; a piece from the future is asked again later.
        assert self.send(self.session, 0, b"one-").json()["next"] == 1
        assert self.send(self.session, 5, b"six-").json()["next"] == 1
        assert self.send(self.session, 1, b"two").json()["next"] == 2

        recording = Recording.objects.get(session=self.session, kind="webcam")
        assert storage.absolute(recording.path).read_bytes() == b"one-two"
        assert recording.size == 7
        assert self.send(self.session, 0, kind="keyboard").status_code == 400

    def test_a_piece_that_is_too_large_leaves_no_trace(self):
        self.client.post(self.url("start", self.session))
        self.send(self.session, 0, b"good")
        with override_settings(EKTU_PROCTORING={"ROOT": storage.conf("ROOT"), "MIN_FREE_MB": 0, "MAX_CHUNK_BYTES": 8}):
            assert self.send(self.session, 1, b"x" * 9).status_code == 413
        recording = Recording.objects.get(session=self.session)
        assert storage.absolute(recording.path).read_bytes() == b"good"
        assert recording.next_seq == 1

    def test_recording_is_refused_when_the_disk_is_full(self):
        with mock.patch.object(storage, "free_mb", return_value=10), \
                override_settings(EKTU_PROCTORING={"ROOT": storage.conf("ROOT"), "MIN_FREE_MB": 5000}):
            assert self.client.post(self.url("start", self.session)).status_code == 507
        self.session.refresh_from_db()
        assert self.session.status == Session.CREATED

    def test_photo_and_events(self):
        self.client.post(self.url("start", self.session))
        photo = self.client.post(self.url("photo", self.session), data=b"\xff\xd8jpeg", content_type="image/jpeg")
        assert photo.status_code == 200
        assert self.client.post(
            self.url("photo", self.session), data=b"<html>", content_type="image/jpeg",
        ).status_code == 400

        def report(kind):
            return self.client.post(
                self.url("event", self.session), data=json.dumps({"kind": kind}), content_type="application/json",
            ).status_code

        assert report("camera_lost") == 200
        assert report("exam_started") == 400  # only the server may say this
        assert self.kinds(self.session) == ["started", "camera_lost"]

    def test_silence_becomes_a_gap(self):
        self.client.post(self.url("start", self.session))
        Session.objects.filter(pk=self.session.pk).update(last_seen=timezone.now() - timedelta(minutes=5))
        self.send(self.session, 0)
        gap = self.session.events.get(kind="gap")
        assert 295 <= gap.details["seconds"] <= 305

    def test_the_page_stops_when_the_exam_ends(self):
        self.client.post(self.url("start", self.session))
        assert self.client.post(self.url("heartbeat", self.session)).json()["stop"] is False
        self.session.refresh_from_db()
        services.request_stop(self.session)
        assert self.send(self.session, 0).json()["stop"] is True
        assert self.client.post(self.url("finish", self.session)).json() == {"status": "finished"}
        assert self.send(self.session, 1).status_code == 409
        assert self.kinds(self.session) == ["started", "exam_finished", "finished"]

    def test_stopping_before_the_exam_ends_is_noted(self):
        self.client.post(self.url("start", self.session))
        self.client.post(self.url("finish", self.session))
        assert "stopped_by_user" in self.kinds(self.session)

    def test_a_closed_page_is_finished_by_the_server(self):
        self.client.post(self.url("start", self.session))
        Session.objects.filter(pk=self.session.pk).update(last_seen=timezone.now() - timedelta(minutes=5))
        self.session.refresh_from_db()
        services.request_stop(self.session)
        assert "page_closed_before_end" in self.kinds(self.session)
        tasks.force_finish(self.session.pk)
        self.session.refresh_from_db()
        assert self.session.status == Session.STOPPING  # the page still has time to hand in the rest
        Session.objects.filter(pk=self.session.pk).update(exam_finished_at=timezone.now() - timedelta(minutes=2))
        tasks.force_finish(self.session.pk)
        self.session.refresh_from_db()
        assert self.session.status == Session.FINISHED


class OpenEdxBackendTest(ProctoringTestCase):
    """The service as the proctoring provider of this Open edX."""

    def setUp(self):
        super().setUp()
        config = apps.get_app_config("edx_proctoring")
        with_backend = mock.patch.dict(config.backends, {"ektu": EktuProctoringBackend()})
        with_backend.start()
        self.addCleanup(with_backend.stop)
        for name, service in (
            ("credit", MockCreditService()), ("instructor", MockInstructorService()),
            ("grades", MockGradesService()), ("certificates", MockCertificateService()),
        ):
            previous = get_runtime_service(name)
            set_runtime_service(name, service)
            self.addCleanup(set_runtime_service, name, previous)

        self.learner = UserFactory(password=PASSWORD)
        self.staff = UserFactory(password=PASSWORD)
        CourseStaffRole(CourseKey.from_string(COURSE)).add_users(self.staff)
        self.exam_id = create_exam(
            course_id=COURSE, content_id="block-v1:EKTU+PR101+2026+type@sequential+block@final",
            exam_name="Final exam", time_limit_mins=60, is_proctored=True, backend="ektu",
        )
        self.attempt_id = create_exam_attempt(self.exam_id, self.learner.id, taking_as_proctored=True)
        self.session = Session.objects.get()

    def status(self):
        return get_exam_attempt_by_id(self.attempt_id)["status"]

    def test_an_attempt_orders_a_session(self):
        attempt = get_exam_attempt_by_id(self.attempt_id)
        assert attempt["external_id"] == str(self.session.key)
        assert self.session.course_id == COURSE
        assert self.session.exam_name == "Final exam"
        assert self.session.external_ref == attempt["attempt_code"]
        data = get_exam_attempt_data(self.exam_id, self.attempt_id, is_learning_mfe=True)
        assert data["software_download_url"].endswith(self.url("capture", self.session))
        # The course turns its tab into the recording page, which then offers the exam in a new tab.
        assert self.session.return_url.endswith(f"/course/{COURSE}/block-v1:EKTU+PR101+2026+type@sequential+block@final")
        page = self.client.get(self.url("capture", self.session)).content.decode()
        assert f'href="{self.session.return_url}" target="_blank"' in page

    def test_the_course_page_explains_what_to_do(self):
        from edx_proctoring.api import get_proctoring_settings_by_exam_id  # pylint: disable=import-outside-toplevel

        shown = get_proctoring_settings_by_exam_id(self.exam_id)
        assert shown["provider_name"] == "EKTU Proctoring"
        assert len(shown["exam_proctoring_backend"]["instructions"]) == 5

    def test_the_whole_exam(self):
        assert self.status() == Status.created
        self.client.post(self.url("start", self.session))
        assert self.status() == Status.ready_to_start

        update_attempt_status(self.attempt_id, Status.started)
        update_attempt_status(self.attempt_id, Status.submitted)
        self.session.refresh_from_db()
        assert self.session.status == Session.STOPPING
        assert self.session.exam_started_at and self.session.exam_finished_at
        self.client.post(self.url("finish", self.session))
        assert self.kinds(self.session) == ["started", "exam_started", "exam_finished", "finished"]

        self.sign_in(self.staff)
        review = reverse("ektu_proctoring:review", args=[self.session.key])
        assert self.client.post(review, {"verdict": "passed", "comment": "All fine"}).status_code == 302
        assert self.status() == Status.verified
        # The reviewer may change their mind.
        self.client.post(review, {"verdict": "failed", "comment": "A second person in the room"})
        assert self.status() == Status.rejected
        self.session.refresh_from_db()
        assert self.session.reviewed_by == self.staff

    def test_no_result_before_the_recording_ends(self):
        self.client.post(self.url("start", self.session))
        self.sign_in(self.staff)
        review = reverse("ektu_proctoring:review", args=[self.session.key])
        assert self.client.post(review, {"verdict": "passed"}).status_code == 403

    def test_a_fault_in_the_service_does_not_block_the_exam(self):
        self.client.post(self.url("start", self.session))
        update_attempt_status(self.attempt_id, Status.started)
        with mock.patch.object(services, "request_stop", side_effect=RuntimeError):
            update_attempt_status(self.attempt_id, Status.submitted)
        assert self.status() == Status.submitted

    def test_who_may_review(self):
        url = reverse("ektu_proctoring:review", args=[self.session.key])
        listing = reverse("ektu_proctoring:review_list")
        assert self.client.get(url).status_code == 302  # to the sign-in page

        self.sign_in(self.learner)
        assert self.client.get(url).status_code == 403
        assert self.client.get(listing).status_code == 403

        other = UserFactory(password=PASSWORD)
        CourseStaffRole(CourseKey.from_string("course-v1:EKTU+OTHER+2026")).add_users(other)
        self.sign_in(other)
        assert self.client.get(url).status_code == 403
        assert "Final exam" not in self.client.get(listing).content.decode()

        self.sign_in(self.staff)
        assert self.client.get(url).status_code == 200
        assert "Final exam" in self.client.get(listing).content.decode()
        found = self.client.get(listing, {"attempt": str(self.session.key)})
        assert found.status_code == 302 and found.url == url

    def test_media_is_served_in_ranges_to_reviewers_only(self):
        self.client.post(self.url("start", self.session))
        self.send(self.session, 0, b"0123456789")
        recording = Recording.objects.get(session=self.session)
        url = reverse("ektu_proctoring:media", args=[self.session.key, recording.pk])
        assert self.client.get(url).status_code == 403

        self.sign_in(self.staff)
        part = self.client.get(url, HTTP_RANGE="bytes=2-5")
        assert part.status_code == 206
        assert part.content == b"2345"
        assert part["Content-Range"] == "bytes 2-5/10"
        assert part["Cache-Control"] == "private, no-store"
        assert b"".join(self.client.get(url).streaming_content) == b"0123456789"
        assert self.client.get(url, HTTP_RANGE="bytes=50-").status_code == 416

    def test_an_abandoned_session_is_closed_when_someone_looks(self):
        self.client.post(self.url("start", self.session))
        Session.objects.filter(pk=self.session.pk).update(last_seen=timezone.now() - timedelta(hours=3))
        self.sign_in(self.staff)
        self.client.get(reverse("ektu_proctoring:review", args=[self.session.key]))
        self.session.refresh_from_db()
        assert self.session.status == Session.FINISHED
        assert "abandoned" in self.kinds(self.session)


class ApiTest(ProctoringTestCase):
    """Sessions ordered by another system."""

    def setUp(self):
        super().setUp()
        self.api_client = ApiClient(name="Moodle of a partner", webhook_url="https://partner.example/hook")
        self.key = self.api_client.issue_key()
        self.api_client.save()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.key}"}

    def order(self, **fields):
        return self.client.post(
            reverse("ektu_proctoring:api_sessions"), data=json.dumps(fields),
            content_type="application/json", **self.auth,
        )

    def test_a_key_is_required(self):
        url = reverse("ektu_proctoring:api_sessions")
        assert self.client.post(url, data="{}", content_type="application/json").status_code == 401
        wrong = {"HTTP_AUTHORIZATION": "Bearer ektu_wrong"}
        assert self.client.post(url, data="{}", content_type="application/json", **wrong).status_code == 401
        self.api_client.is_active = False
        self.api_client.save()
        assert self.order().status_code == 401

    def test_the_key_itself_is_not_stored(self):
        assert self.key not in json.dumps(list(ApiClient.objects.values()), default=str)

    def test_a_session_from_order_to_result(self):
        ordered = self.order(external_ref="quiz-17/user-5", subject_name="Dana", exam_name="Entrance test",
                             return_url="https://partner.example/quiz/17", record_screen=False)
        assert ordered.status_code == 201
        data = ordered.json()
        session = Session.objects.get(key=data["id"])
        assert session.client == self.api_client and not session.record_screen
        assert data["capture_url"].endswith(self.url("capture", session))
        assert self.order(return_url="javascript:alert(1)").status_code == 400

        detail = reverse("ektu_proctoring:api_session", args=[session.key])
        self.client.post(self.url("start", session))
        assert self.send(session, 0, kind="screen").status_code == 400  # the screen was not ordered
        self.send(session, 0)
        assert self.client.get(detail, **self.auth).json()["status"] == "recording"

        def call(name, **body):
            return self.client.post(
                reverse(f"ektu_proctoring:{name}", args=[session.key]), data=json.dumps(body),
                content_type="application/json", **self.auth,
            )

        assert call("api_review", verdict="passed").status_code == 409  # still recording
        assert call("api_exam_started").status_code == 200
        assert call("api_finish").json()["status"] == "stopping"
        self.client.post(self.url("finish", session))
        assert call("api_review", verdict="maybe").status_code == 400
        assert call("api_review", verdict="failed", comment="Phone in hand").json()["verdict"] == "failed"

        data = self.client.get(detail, **self.auth).json()
        assert [event["kind"] for event in data["events"]] == [
            "started", "exam_started", "exam_finished", "finished",
        ]
        assert data["recordings"][0]["kind"] == "webcam"

        # The review link works without an account here, and only for this session.
        link = data["review_url"]
        page = self.client.get(link)
        assert page.status_code == 200 and "Dana" in page.content.decode()
        other = services.create_session(client=self.api_client)
        forged = reverse("ektu_proctoring:review", args=[other.key]) + "?" + link.split("?")[1]
        assert self.client.get(forged).status_code == 302

    def test_clients_do_not_see_each_other(self):
        session = Session.objects.get(key=self.order().json()["id"])
        stranger = ApiClient(name="Someone else")
        auth = {"HTTP_AUTHORIZATION": f"Bearer {stranger.issue_key()}"}
        stranger.save()
        assert self.client.get(reverse("ektu_proctoring:api_session", args=[session.key]), **auth).status_code == 404

    def test_webhook_is_signed(self):
        session = Session.objects.get(key=self.order(exam_name="Entrance test").json()["id"])
        with mock.patch.object(tasks.requests, "post") as post:
            tasks.send_webhook(session.pk, "finished")
        body = post.call_args.kwargs["data"]
        assert json.loads(body)["event"] == "finished"
        expected = hmac.new(self.api_client.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        assert post.call_args.kwargs["headers"]["X-Ektu-Signature"] == f"sha256={expected}"


class StorageTest(ProctoringTestCase):
    """Files on disk."""

    def test_a_half_written_piece_is_dropped_before_the_next_one(self):
        session = services.create_session()
        services.begin(session)
        services.append_chunk(session, "webcam", 1, 0, WEBM, 0, io.BytesIO(b"whole"))
        recording = Recording.objects.get()
        with open(storage.absolute(recording.path), "ab") as target:
            target.write(b"torn")
        services.append_chunk(session, "webcam", 1, 1, WEBM, 0, io.BytesIO(b"-next"))
        assert storage.absolute(recording.path).read_bytes() == b"whole-next"

    def test_cleanup_deletes_files_and_keeps_the_record(self):
        from django.core.management import call_command  # pylint: disable=import-outside-toplevel

        session = services.create_session(exam_name="Old exam")
        services.begin(session)
        services.append_chunk(session, "webcam", 1, 0, WEBM, 0, io.BytesIO(b"video"))
        services.finish(session)
        folder = storage.absolute(storage.relative_dir(session))
        Session.objects.filter(pk=session.pk).update(created=timezone.now() - timedelta(days=400))
        call_command("ektu_proctoring_cleanup", "--dry-run")
        assert folder.exists()
        call_command("ektu_proctoring_cleanup")
        session.refresh_from_db()
        assert not folder.exists() and session.purged_at
        assert Event.objects.filter(session=session).exists()
