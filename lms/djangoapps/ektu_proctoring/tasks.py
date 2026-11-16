"""Background work: closing sessions, indexing recordings, telling API clients."""
import hashlib
import hmac
import json
import logging

import requests
from celery import shared_task
from django.utils import timezone
from edx_django_utils.monitoring import set_code_owner_attribute

from . import services, storage
from .conf import conf
from .models import Recording, Session

log = logging.getLogger(__name__)


@shared_task
@set_code_owner_attribute
def force_finish(session_id):
    """Close a session whose page never handed in the rest, e.g. the tab was closed."""
    session = Session.objects.filter(pk=session_id, status=Session.STOPPING).first()
    if not session:
        return
    # Where tasks run at once instead of being queued, the page still gets its time.
    waited = (timezone.now() - session.exam_finished_at).total_seconds()
    if waited >= conf("STOP_GRACE_SECONDS") - 5:
        services.finish(session)


@shared_task
@set_code_owner_attribute
def finalize_session(session_id):
    for recording in Recording.objects.filter(session_id=session_id).exclude(kind=Recording.PHOTO):
        storage.finalize(recording)


@shared_task(bind=True, max_retries=5, default_retry_delay=60)
@set_code_owner_attribute
def send_webhook(self, session_id, what):
    session = Session.objects.select_related("client").get(pk=session_id)
    client = session.client
    body = json.dumps({"event": what, "session": services.as_dict(session)}).encode()
    signature = hmac.new(client.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        response = requests.post(
            client.webhook_url, data=body, timeout=10,
            headers={"Content-Type": "application/json", "X-Ektu-Signature": f"sha256={signature}"},
        )
        response.raise_for_status()
    except requests.RequestException as error:
        log.warning("Webhook %s for session %s failed: %s", what, session.key, error)
        raise self.retry(exc=error)
