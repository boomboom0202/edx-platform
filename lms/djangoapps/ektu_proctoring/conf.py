"""Settings of the service: ``EKTU_PROCTORING`` in Django settings overrides these."""
from django.conf import settings

DEFAULTS = {
    # Where recordings are kept. Must not be inside MEDIA_ROOT: that is served publicly.
    "ROOT": "/openedx/proctoring",
    # A new recording is refused when less than this is free on the disk.
    "MIN_FREE_MB": 5000,
    # The LMS proxy accepts 4 MB per request; a chunk is far smaller.
    "MAX_CHUNK_BYTES": 3_500_000,
    "MAX_PHOTO_BYTES": 1_000_000,
    # How often the browser sends a piece of the recording.
    "TIMESLICE_MS": 15000,
    "WEBCAM_BITS": 150_000,
    "SCREEN_BITS": 300_000,
    "AUDIO_BITS": 32_000,
    # Silence from the recording page longer than this is logged as a gap.
    "GAP_SECONDS": 60,
    # How long the page has to hand in the last pieces after the exam ends.
    "STOP_GRACE_SECONDS": 90,
    "RETENTION_DAYS": 180,
    # Lifetime of the review link given to API clients.
    "REVIEW_LINK_HOURS": 12,
    # The player asks for the video piece by piece, so a web worker is never held for long.
    "MEDIA_RESPONSE_BYTES": 2_000_000,
}


def conf(name):
    return getattr(settings, "EKTU_PROCTORING", {}).get(name, DEFAULTS[name])
