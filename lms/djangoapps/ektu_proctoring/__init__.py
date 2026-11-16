"""
EKTU proctoring: records a learner's webcam, microphone and screen in the
browser while they take an exam, keeps the recordings on this server and lets
staff review them.

The service itself knows nothing about Open edX: a session is created either by
the edx-proctoring backend in ``backend.py`` or by another system through the
REST API in ``views_api.py``.
"""
