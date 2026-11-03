"""Tests for the BigBlueButton API client."""
import hashlib
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests
from django.test import SimpleTestCase

from openedx.features.ektu_bigbluebutton.client import BigBlueButtonClient, BigBlueButtonError

SECRET = "test-secret"
GET = "openedx.features.ektu_bigbluebutton.client.requests.get"

MEETING = """<response><returncode>SUCCESS</returncode><running>true</running>
<participantCount>3</participantCount><createTime>1700000000000</createTime><endTime>0</endTime></response>"""
ENDED = """<response><returncode>SUCCESS</returncode><running>false</running>
<participantCount>0</participantCount><createTime>1700000000000</createTime><endTime>1700000999000</endTime></response>"""
NOT_FOUND = "<response><returncode>FAILED</returncode><messageKey>notFound</messageKey></response>"
REFUSED = "<response><returncode>FAILED</returncode><messageKey>checksumError</messageKey></response>"
RECORDINGS = """<response><returncode>SUCCESS</returncode><recordings>
<recording><startTime>1000</startTime><published>true</published><playback><format><type>presentation</type>
<url>https://bbb/old</url><length>7</length></format></playback></recording>
<recording><startTime>2000</startTime><published>true</published><playback><format><type>presentation</type>
<url>https://bbb/new</url><length>12</length></format></playback></recording>
<recording><startTime>3000</startTime><published>false</published><playback><format><type>presentation</type>
<url>https://bbb/hidden</url><length>1</length></format></playback></recording>
</recordings></response>"""
NO_RECORDINGS = """<response><returncode>SUCCESS</returncode><recordings/>
<messageKey>noRecordings</messageKey></response>"""


def reply(body):
    return mock.Mock(content=body.encode(), raise_for_status=mock.Mock())


class ClientTest(SimpleTestCase):

    def setUp(self):
        super().setUp()
        self.client_ = BigBlueButtonClient("https://bbb.example/bigbluebutton/", SECRET)

    def test_signature_covers_the_exact_query(self):
        url = self.client_.signed_url("join", {"fullName": "Тест Слушатель", "role": "VIEWER"})
        query, checksum = urlparse(url).query.rsplit("&checksum=", 1)
        assert checksum == hashlib.sha256(f"join{query}{SECRET}".encode()).hexdigest()
        assert url.startswith("https://bbb.example/bigbluebutton/api/join?")
        assert parse_qs(query)["fullName"] == ["Тест Слушатель"]
        assert SECRET not in url

    def test_signature_without_parameters(self):
        url = self.client_.signed_url("getMeetings", {})
        checksum = hashlib.sha256(f"getMeetings{SECRET}".encode()).hexdigest()
        assert url == f"https://bbb.example/bigbluebutton/api/getMeetings?checksum={checksum}"

    @mock.patch(GET, return_value=reply(MEETING))
    def test_get_meeting(self, _get):
        meeting = self.client_.get_meeting("m1")
        assert meeting.running is True
        assert meeting.participants == 3
        assert meeting.create_time == "1700000000000"

    @mock.patch(GET, return_value=reply(NOT_FOUND))
    def test_missing_meeting_is_none(self, _get):
        assert self.client_.get_meeting("m1") is None

    @mock.patch(GET, return_value=reply(ENDED))
    def test_ended_meeting_is_none(self, _get):
        assert self.client_.get_meeting("m1") is None

    @mock.patch(GET, return_value=reply(REFUSED))
    def test_refusal_is_an_error_not_a_missing_meeting(self, _get):
        with self.assertRaises(BigBlueButtonError):
            self.client_.get_meeting("m1")

    @mock.patch(GET, side_effect=requests.ConnectionError("https://bbb.example/api?checksum=abc"))
    def test_errors_do_not_carry_the_url(self, _get):
        with self.assertRaises(BigBlueButtonError) as caught:
            self.client_.get_meeting("m1")
        assert "checksum" not in str(caught.exception)

    @mock.patch(GET, return_value=reply("this is not xml"))
    def test_garbage_is_an_error(self, _get):
        with self.assertRaises(BigBlueButtonError):
            self.client_.get_meeting("m1")

    @mock.patch(GET, return_value=reply(RECORDINGS))
    def test_recordings_are_published_only_newest_first(self, _get):
        items = self.client_.get_recordings("m1")
        assert [item.url for item in items] == ["https://bbb/new", "https://bbb/old"]
        assert items[0].minutes == 12

    @mock.patch(GET, return_value=reply(NO_RECORDINGS))
    def test_no_recordings(self, _get):
        assert self.client_.get_recordings("m1") == []
