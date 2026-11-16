"""Data kept by the proctoring service."""
import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def make_token():
    return secrets.token_urlsafe(32)


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


class ApiClient(models.Model):
    """Another system (an LMS, an exam portal) allowed to order proctoring sessions."""

    name = models.CharField(max_length=120, unique=True)
    key_prefix = models.CharField(max_length=12, editable=False)
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    webhook_url = models.URLField(blank=True, help_text="Receives a POST when a session changes.")
    webhook_secret = models.CharField(
        max_length=64, blank=True, default=make_token,
        help_text="Signs webhooks: X-Ektu-Signature is HMAC-SHA256 of the body.",
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "API client"

    def __str__(self):
        return self.name

    def issue_key(self):
        """Replace the key. The plain key is returned once and never stored."""
        key = "ektu_" + secrets.token_urlsafe(32)
        self.key_prefix = key[:12]
        self.key_hash = hash_key(key)
        return key

    @classmethod
    def by_key(cls, key):
        return cls.objects.filter(key_hash=hash_key(key), is_active=True).first()


class Session(models.Model):
    """One person taking one exam under recording."""

    CREATED, RECORDING, STOPPING, FINISHED = "created", "recording", "stopping", "finished"
    STATUSES = [(CREATED, "Created"), (RECORDING, "Recording"), (STOPPING, "Stopping"), (FINISHED, "Finished")]
    PENDING, PASSED, FAILED = "pending", "passed", "failed"
    VERDICTS = [(PENDING, "Pending"), (PASSED, "Passed"), (FAILED, "Failed")]

    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # Opens the recording page; whoever holds it can only add to this session.
    token = models.CharField(max_length=64, unique=True, default=make_token, editable=False)
    # Empty for sessions ordered by this Open edX itself.
    client = models.ForeignKey(ApiClient, null=True, blank=True, on_delete=models.PROTECT)
    # The orderer's own id: the attempt code in Open edX.
    external_ref = models.CharField(max_length=255, blank=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    subject_name = models.CharField(max_length=255, blank=True)
    subject_ref = models.CharField(max_length=255, blank=True)
    course_id = models.CharField(max_length=255, blank=True, db_index=True)
    exam_name = models.CharField(max_length=255, blank=True)
    return_url = models.URLField(max_length=500, blank=True)

    record_webcam = models.BooleanField(default=True)
    record_audio = models.BooleanField(default=True)
    record_screen = models.BooleanField(default=True)

    status = models.CharField(max_length=16, choices=STATUSES, default=CREATED, db_index=True)
    verdict = models.CharField(max_length=16, choices=VERDICTS, default=PENDING, db_index=True)
    review_comment = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    reviewer_label = models.CharField(max_length=255, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    consent_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    exam_started_at = models.DateTimeField(null=True, blank=True)
    exam_finished_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    # Set when the files were deleted by the retention clean-up.
    purged_at = models.DateTimeField(null=True, blank=True)

    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"{self.key} {self.exam_name}"

    @property
    def is_active(self):
        return self.status in (self.RECORDING, self.STOPPING)


class Recording(models.Model):
    """One uninterrupted file: a new segment starts whenever the page restarts a recorder."""

    WEBCAM, SCREEN, PHOTO = "webcam", "screen", "photo"
    KINDS = [(WEBCAM, "Webcam"), (SCREEN, "Screen"), (PHOTO, "Photo")]

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="recordings")
    kind = models.CharField(max_length=10, choices=KINDS)
    segment = models.PositiveIntegerField(default=1)
    # Relative to the storage root.
    path = models.CharField(max_length=255)
    mime = models.CharField(max_length=80, blank=True)
    size = models.BigIntegerField(default=0)
    # The piece the server expects next: pieces are appended strictly in order.
    next_seq = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
    duration_ms = models.BigIntegerField(null=True, blank=True)
    # True once the file was rewritten with an index, so a player can jump around in it.
    seekable = models.BooleanField(default=False)

    class Meta:
        unique_together = [("session", "kind", "segment")]
        ordering = ["segment", "kind"]


class Event(models.Model):
    """Something a reviewer should know about: a lost camera, a gap, the exam start."""

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=40)
    at = models.DateTimeField(default=timezone.now)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at", "id"]
