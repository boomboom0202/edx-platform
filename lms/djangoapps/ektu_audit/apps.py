"""
App configuration.

Registered through the ``lms.djangoapp`` entry point in setup.py, so the LMS
mounts the URLs by itself.
"""
from django.apps import AppConfig


class EktuAuditConfig(AppConfig):
    name = "lms.djangoapps.ektu_audit"
    label = "ektu_audit"
    verbose_name = "EKTU course log"

    plugin_app = {
        "url_config": {
            "lms.djangoapp": {
                "namespace": "ektu_audit",
                "regex": r"^course-log/",
                "relative_path": "urls",
            },
        },
    }
