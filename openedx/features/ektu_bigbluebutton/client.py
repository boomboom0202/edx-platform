"""
A small client for the BigBlueButton API.

Every call is a GET request signed with the server's shared secret, which never
leaves the LMS: browsers only ever receive a signed join URL.

https://docs.bigbluebutton.org/development/api
"""
import hashlib
import logging
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote, urlencode

import requests
from defusedxml import ElementTree

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = (3, 8)


class BigBlueButtonError(Exception):
    """The server could not be reached or refused a call."""


@dataclass
class Meeting:
    running: bool
    participants: int
    create_time: str


@dataclass
class Recording:
    start_ms: int
    minutes: int
    url: str


class BigBlueButtonClient:
    """Signs and sends API calls."""

    def __init__(self, base_url, secret):
        self.api_url = base_url.rstrip("/") + "/api/"
        self.secret = secret

    def signed_url(self, call, params):
        """
        The URL of an API call. The checksum covers the query string exactly as
        it is sent, so the string is built once and never re-encoded.
        """
        query = urlencode(params, quote_via=quote)
        checksum = hashlib.sha256(f"{call}{query}{self.secret}".encode()).hexdigest()
        separator = "&" if query else ""
        return f"{self.api_url}{call}?{query}{separator}checksum={checksum}"

    def _call(self, call, params):
        """The parsed response. Errors never carry the URL: it holds a checksum."""
        try:
            response = requests.get(self.signed_url(call, params), timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            return ElementTree.fromstring(response.content)
        except (requests.RequestException, ElementTree.ParseError) as error:
            raise BigBlueButtonError(f"{call}: {type(error).__name__}") from None

    def get_meeting(self, meeting_id) -> Optional[Meeting]:
        """The meeting, or None if it does not exist or has already ended."""
        root = self._call("getMeetingInfo", {"meetingID": meeting_id})
        if root.findtext("returncode") != "SUCCESS":
            if root.findtext("messageKey") == "notFound":
                return None
            raise BigBlueButtonError(f"getMeetingInfo: {root.findtext('messageKey')}")
        if root.findtext("endTime", "0") != "0":
            return None
        return Meeting(
            running=root.findtext("running") == "true",
            participants=int(root.findtext("participantCount", "0")),
            create_time=root.findtext("createTime", ""),
        )

    def create_meeting(self, meeting_id, name, **options) -> str:
        """
        Create the meeting and return its createTime. Calling this for a meeting
        that already exists is safe when the parameters are the same.
        """
        params = {"meetingID": meeting_id, "name": name, **options}
        root = self._call("create", params)
        if root.findtext("returncode") != "SUCCESS":
            raise BigBlueButtonError(f"create: {root.findtext('messageKey')}")
        return root.findtext("createTime", "")

    def join_url(self, meeting_id, full_name, role, user_id, create_time, **options):
        """The URL that takes a browser into the meeting. Nothing is sent to the server yet."""
        params = {
            "meetingID": meeting_id,
            "fullName": full_name,
            "role": role,
            "userID": user_id,
            "createTime": create_time,
            **options,
        }
        return self.signed_url("join", params)

    def get_recordings(self, meeting_id) -> List[Recording]:
        """Published recordings of the meeting, newest first."""
        root = self._call("getRecordings", {"meetingID": meeting_id})
        if root.findtext("returncode") != "SUCCESS":
            raise BigBlueButtonError(f"getRecordings: {root.findtext('messageKey')}")
        recordings = []
        for item in root.iter("recording"):
            if item.findtext("published") != "true":
                continue
            playback = item.find("playback/format")
            if playback is None or not playback.findtext("url"):
                continue
            recordings.append(Recording(
                start_ms=int(item.findtext("startTime", "0")),
                minutes=int(playback.findtext("length", "0") or 0),
                url=playback.findtext("url"),
            ))
        return sorted(recordings, key=lambda recording: recording.start_ms, reverse=True)
