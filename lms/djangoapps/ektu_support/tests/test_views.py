"""Tests for the support form."""
import io
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from common.djangoapps.student.tests.factories import UserFactory

URL = "/help/ask/"
PASSWORD = "Password1234"


def png(name="screen.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def post_data(**overrides):
    data = {
        "role": "student",
        "topic": "student:sign_in",
        "name": "Test Learner",
        "email": "learner@example.com",
        "course": "",
        "message": "I cannot sign in.",
        "website": "",
    }
    data.update(overrides)
    return data


@override_settings(CONTACT_EMAIL="support@example.com", DEFAULT_FROM_EMAIL="noreply@example.com")
@mock.patch("django_ratelimit.decorators.is_ratelimited", return_value=False)
class SupportFormTest(TestCase):

    def test_url(self, _limited):
        assert reverse("ektu_support:ask") == URL

    def test_page_renders(self, _limited):
        response = self.client.get(URL)
        assert response.status_code == 200
        assert b'name="topic"' in response.content

    def test_sends_one_email_to_support(self, _limited):
        response = self.client.post(URL, post_data(screenshots=[png()]))
        assert response.status_code == 302
        assert response["Location"].endswith("?sent=1")
        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == ["support@example.com"]
        assert message.reply_to == ["learner@example.com"]
        assert "I cannot sign in." in message.body
        assert len(message.attachments) == 1

    def test_topic_must_belong_to_role(self, _limited):
        response = self.client.post(URL, post_data(role="teacher"))
        assert response.status_code == 200
        assert not mail.outbox

    def test_rejects_non_images(self, _limited):
        fake = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
        response = self.client.post(URL, post_data(screenshots=[fake]))
        assert response.status_code == 200
        assert not mail.outbox

    def test_rejects_too_many_screenshots(self, _limited):
        files = [png(f"{i}.png") for i in range(4)]
        response = self.client.post(URL, post_data(screenshots=files))
        assert response.status_code == 200
        assert not mail.outbox

    def test_honeypot_sends_nothing(self, _limited):
        response = self.client.post(URL, post_data(website="http://spam"))
        assert response.status_code == 302
        assert not mail.outbox

    def test_signed_in_user_is_named_in_the_email(self, _limited):
        user = UserFactory()
        self.client.login(username=user.username, password=PASSWORD)
        self.client.post(URL, post_data())
        assert user.username in mail.outbox[0].body


@override_settings(CONTACT_EMAIL="support@example.com")
class SupportFormRateLimitTest(TestCase):

    @mock.patch("django_ratelimit.decorators.is_ratelimited", return_value=True)
    def test_limited_requests_send_nothing(self, _limited):
        response = self.client.post(URL, post_data())
        assert response.status_code == 200
        assert not mail.outbox


class SupportPagesTest(TestCase):
    """The help pages point people at the form and show the support address."""

    @override_settings(CONTACT_EMAIL="support@example.com")
    def test_pages_link_to_the_form(self):
        for page in ("/help", "/faq", "/contact"):
            response = self.client.get(page)
            assert response.status_code == 200, page
            assert URL.encode() in response.content, page
            assert b"info.open@edu.ektu.kz" in response.content, page
