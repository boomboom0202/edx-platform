"""
Files uploaded to courses, and who uploaded them.

Studio keeps uploaded files in the contentstore (GridFS) with their name, type,
size and upload time, but not the uploader. Studio's tracking log does record
every request to a course's Files page with the user, though: an upload is a
request to ``/assets/<course>/`` without the ``page`` parameter that the file
list sends. Matching the two on course and time names the uploader. The log
only starts in November 2025, so older uploads stay unattributed.

The log belongs to Studio. The LMS reads it from a read-only mount (see
EKTU_STUDIO_TRACKING_LOG and the Tutor docker-compose override).
"""
import bisect
import json
import os
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.conf import settings

ASSETS_PATH = re.compile(r"^/assets/(course-v1:[^/]+)/$")
ASSET_ID = re.compile(r"^asset-v1:(?P<course>[^+]+\+[^+]+\+[^+]+)\+type@asset\+block@(?P<name>.+)$")
MARKER = '"/assets/course-v1:'

# A re-run copies the source course's files right after creating the course.
RERUN_COPY_SECONDS = 15 * 60

# How far apart the upload request and the stored upload time may be.
MATCH_BEFORE_SECONDS = 10
MATCH_AFTER_SECONDS = 3

READ_CHUNK_BYTES = 8 * 1024 * 1024


@dataclass
class Upload:
    asset_id: str
    course_key: Optional[str]
    name: str
    content_type: str
    size: int
    uploaded_at: datetime
    uploader_id: Optional[int] = None
    # Set when the file came with the course on a re-run rather than being
    # uploaded: the source course. uploader_id is then whoever ran the re-run.
    copied_from: Optional[str] = None


def log_path():
    return getattr(settings, "EKTU_STUDIO_TRACKING_LOG", "/openedx/cms-logs/tracking.log")


class UploadRequests:
    """
    Upload requests from Studio's tracking log, per course, read incrementally.

    The log only grows, so each call reads just the lines added since the last
    one. If the file is replaced or shrinks, it is read again from the start.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.inode = None
        self.offset = 0
        self.requests = defaultdict(list)  # course key -> sorted [(timestamp, user id)]

    def refresh(self):
        try:
            stat = os.stat(self.path)
        except OSError:
            return
        if stat.st_ino != self.inode or stat.st_size < self.offset:
            self.inode, self.offset = stat.st_ino, 0
            self.requests = defaultdict(list)
        marker = MARKER.encode()
        with open(self.path, "rb") as log:
            log.seek(self.offset)
            while self.offset < stat.st_size:
                chunk = log.read(min(READ_CHUNK_BYTES, stat.st_size - self.offset))
                end = chunk.rfind(b"\n")
                if end < 0:
                    return  # a line still being written
                self.offset += end + 1
                log.seek(self.offset)
                for line in chunk[:end].split(b"\n"):
                    if marker not in line:
                        continue
                    parsed = parse_upload_request(line.decode("utf-8", "replace"))
                    if parsed:
                        course_key, timestamp, user_id = parsed
                        bisect.insort(self.requests[course_key], (timestamp, user_id))

    def uploader(self, course_key, uploaded_at):
        with self.lock:
            self.refresh()
            requests = self.requests.get(course_key, [])
            stamp = uploaded_at.timestamp()
            start = bisect.bisect_left(requests, (stamp - MATCH_BEFORE_SECONDS,))
            best = None
            for timestamp, user_id in requests[start:]:
                if timestamp > stamp + MATCH_AFTER_SECONDS:
                    break
                if best is None or abs(timestamp - stamp) < abs(best[0] - stamp):
                    best = (timestamp, user_id)
            return best[1] if best else None


def parse_upload_request(line):
    """(course key, unix time, user id) for an upload request line, else None."""
    if MARKER not in line or '"event_source": "server"' not in line:
        return None
    try:
        event = json.loads(line[line.index("{"):])
    except ValueError:
        return None
    match = ASSETS_PATH.match(event.get("event_type", ""))
    user_id = (event.get("context") or {}).get("user_id")
    if not match or not user_id:
        return None
    params = event.get("event") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = {}
    if not isinstance(params, dict):
        params = {}
    if (params.get("GET") or {}).get("page") is not None:
        return None  # the file list paging, not an upload
    try:
        stamp = datetime.fromisoformat(event["time"]).timestamp()
    except (KeyError, ValueError):
        return None
    return match.group(1), stamp, user_id


_requests = None
_requests_lock = threading.Lock()


def upload_requests():
    global _requests  # pylint: disable=global-statement
    with _requests_lock:
        if _requests is None or _requests.path != log_path():
            _requests = UploadRequests(log_path())
        return _requests


def course_of(asset_id):
    """Course key and file name from a GridFS id; old-style ids give no run."""
    if isinstance(asset_id, str):
        match = ASSET_ID.match(asset_id)
        if match:
            return f"course-v1:{match.group('course')}", match.group("name")
        return None, asset_id
    if isinstance(asset_id, dict):
        return f"{asset_id.get('org')}/{asset_id.get('course')}", asset_id.get("name", "")
    return None, str(asset_id)


def _reruns():
    """{new course key: re-run record} for courses created by re-running another."""
    from common.djangoapps.course_action_state.models import CourseRerunState

    rows = CourseRerunState.objects.values("course_key", "source_course_key", "created_time", "created_user_id")
    return {str(row["course_key"]): row for row in rows}


def _files():
    from xmodule.contentstore.django import contentstore

    return contentstore().fs_files


def query(course_key=None, date_from=None, date_to=None):
    conditions = [{"_id": {"$not": re.compile(r"type@thumbnail")}}]
    if course_key:
        prefix = "asset-v1:" + course_key[len("course-v1:"):] + "+type@asset+block@"
        conditions.append({"_id": {"$regex": "^" + re.escape(prefix)}})
    if date_from or date_to:
        span = {}
        if date_from:
            span["$gte"] = date_from
        if date_to:
            span["$lt"] = date_to
        conditions.append({"uploadDate": span})
    return {"$and": conditions}


def list_uploads(course_key=None, date_from=None, date_to=None, offset=0, limit=30):
    """(uploads newest first, total count)."""
    files = _files()
    spec = query(course_key, date_from, date_to)
    total = files.count_documents(spec)
    docs = files.find(
        spec, {"displayname": 1, "contentType": 1, "length": 1, "uploadDate": 1},
    ).sort("uploadDate", -1).skip(offset).limit(limit)

    requests = upload_requests()
    reruns = _reruns()
    uploads = []
    for doc in docs:
        course, name = course_of(doc["_id"])
        upload = Upload(
            asset_id=str(doc["_id"]),
            course_key=course,
            name=doc.get("displayname") or name,
            content_type=doc.get("contentType") or "",
            size=int(doc.get("length") or 0),
            uploaded_at=doc["uploadDate"],
        )
        if course and course.startswith("course-v1:"):
            upload.uploader_id = requests.uploader(course, upload.uploaded_at)
            rerun = reruns.get(course)
            if upload.uploader_id is None and rerun is not None:
                started = rerun["created_time"].timestamp()
                if started - 5 <= upload.uploaded_at.timestamp() <= started + RERUN_COPY_SECONDS:
                    upload.copied_from = str(rerun["source_course_key"])
                    upload.uploader_id = rerun["created_user_id"]
        uploads.append(upload)
    return uploads, total
