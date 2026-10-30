"""Tests for the course log."""
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest import mock

from bson import ObjectId
from django.test import TestCase

from common.djangoapps.student.tests.factories import UserFactory
from lms.djangoapps.ektu_audit import history, uploads

PASSWORD = "Password1234"


def at(seconds):
    return datetime.fromtimestamp(1_700_000_000 + seconds, tz=timezone.utc)


class AssignVersionsTest(TestCase):

    def test_walks_each_branch_back_from_its_head(self):
        a, b, c, p = ObjectId(), ObjectId(), ObjectId(), ObjectId()
        origin = ObjectId()
        docs = [
            {"_id": a, "previous_version": None, "original_version": origin, "edited_by": 5, "edited_on": at(1)},
            {"_id": b, "previous_version": a, "original_version": origin, "edited_by": 5, "edited_on": at(2)},
            {"_id": c, "previous_version": b, "original_version": origin, "edited_by": 6, "edited_on": at(3)},
            {"_id": p, "previous_version": None, "original_version": origin, "edited_by": 6, "edited_on": at(4)},
        ]
        heads = {c: ("course-v1:A+B+C", history.DRAFT), p: ("course-v1:A+B+C", history.PUBLISHED)}
        versions = history.assign_versions(docs, heads)
        assert [v.id for v in versions] == [p, c, b, a]
        assert {v.id: v.branch for v in versions} == {
            a: history.DRAFT, b: history.DRAFT, c: history.DRAFT, p: history.PUBLISHED}
        assert all(v.course_key == "course-v1:A+B+C" for v in versions)

    def test_orphan_gets_course_from_its_lineage(self):
        head, orphan, origin = ObjectId(), ObjectId(), ObjectId()
        docs = [
            {"_id": head, "previous_version": None, "original_version": origin, "edited_by": 1, "edited_on": at(2)},
            {"_id": orphan, "previous_version": None, "original_version": origin, "edited_by": 1, "edited_on": at(1)},
        ]
        versions = history.assign_versions(docs, {head: ("course-v1:X+Y+Z", history.DRAFT)})
        lost = [v for v in versions if v.id == orphan][0]
        assert lost.course_key == "course-v1:X+Y+Z"
        assert lost.branch is None

    def test_system_users(self):
        version = history.Version(ObjectId(), None, -1, at(0), None, None)
        assert version.is_system


class DiffTest(TestCase):

    def block(self, block_type, block_id, version, name=""):
        return {"block_type": block_type, "block_id": block_id,
                "fields": {"display_name": name}, "edit_info": {"update_version": version}}

    def test_created_changed_deleted(self):
        now, before = ObjectId(), ObjectId()
        current = {
            ("html", "a"): self.block("html", "a", now, "Intro"),
            ("html", "b"): self.block("html", "b", before),
            ("video", "c"): self.block("video", "c", now, "Lecture"),
        }
        previous = {
            ("html", "a"): self.block("html", "a", before, "Intro"),
            ("html", "b"): self.block("html", "b", before),
            ("problem", "d"): self.block("problem", "d", before, "Quiz"),
        }
        changes = history.diff_blocks(now, current, previous)
        assert [(c.kind, c.block_id) for c in changes] == [
            (history.CREATED, "c"), (history.CHANGED, "a"), (history.DELETED, "d")]

    def test_field_diff(self):
        changes = history.diff_fields(
            {"display_name": "Old", "data": "line one\nline two"},
            {"display_name": "New", "data": "line one\nline 2"},
        )
        by_name = {c.name: c for c in changes}
        assert by_name["display_name"].before == "Old"
        assert by_name["display_name"].after == "New"
        assert "-line two" in by_name["data"].diff
        assert "+line 2" in by_name["data"].diff


class UploadRequestsTest(TestCase):

    def line(self, path, user_id, moment, params=None):
        event = {
            "event_type": path, "event_source": "server", "context": {"user_id": user_id},
            "event": params if isinstance(params, dict) and params.get("raw") else json.dumps(params or {"GET": {}, "POST": {}}),
            "time": moment.isoformat(),
        }
        return "2026-10-29 10:52:26,000 INFO 1 [tracking] logger.py:41 - " + json.dumps(event) + "\n"

    def test_matches_upload_and_ignores_listing(self):
        course = "course-v1:ACD+IBT+2021"
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as log:
            log.write(self.line(f"/assets/{course}/", 7, at(-30), {"GET": {"page": ["0"]}, "POST": {}}))
            log.write(self.line(f"/assets/{course}/", 8, at(-1)))
            log.write(self.line("/course/" + course, 9, at(0)))
            log.write(self.line(f"/assets/{course}/", 11, at(-500), {"raw": True, "GET": {}}))
        try:
            reader = uploads.UploadRequests(log.name)
            assert reader.uploader(course, at(0)) == 8
            assert reader.uploader(course, at(-30)) is None
            assert reader.uploader("course-v1:O+T+H", at(0)) is None
            assert reader.uploader(course, at(-500)) == 11
            with open(log.name, "a") as more:
                more.write(self.line(f"/assets/{course}/", 10, at(100)))
            assert reader.uploader(course, at(101)) == 10
        finally:
            os.unlink(log.name)

    def test_course_of(self):
        assert uploads.course_of("asset-v1:A+B+C+type@asset+block@x.pdf") == ("course-v1:A+B+C", "x.pdf")
        assert uploads.course_of({"org": "A", "course": "B", "name": "y.png"}) == ("A/B", "y.png")


class AccessTest(TestCase):

    def get(self, user, url="/course-log/"):
        self.client.login(username=user.username, password=PASSWORD)
        return self.client.get(url)

    def test_anonymous_is_sent_to_sign_in(self):
        response = self.client.get("/course-log/")
        assert response.status_code == 302

    def test_learners_are_refused(self):
        for url in ("/course-log/", "/course-log/uploads/", "/course-log/people/"):
            assert self.get(UserFactory(), url).status_code == 403

    @mock.patch.object(history, "all_versions", return_value=[])
    def test_staff_can_see_it(self, _versions):
        assert self.get(UserFactory(is_staff=True)).status_code == 200
        assert self.get(UserFactory(is_staff=True), "/course-log/people/").status_code == 200

    @mock.patch.object(history, "all_versions")
    @mock.patch.object(history, "summarize", return_value=[
        history.BlockChange(history.CHANGED, "video", "v1", "Lecture 1")])
    def test_feed_lists_changes(self, _summarize, versions):
        teacher = UserFactory()
        versions.return_value = [history.Version(
            ObjectId(), ObjectId(), teacher.id, at(0), "course-v1:A+B+C", history.DRAFT)]
        response = self.get(UserFactory(is_staff=True))
        assert response.status_code == 200
        assert b"Lecture 1" in response.content
        assert teacher.username.encode() in response.content
