"""
App configuration.

Registered through the ``lms.djangoapp`` entry point in setup.py, the same way
the platform's own plugin apps are, so the LMS mounts the URLs by itself.
"""
from django.apps import AppConfig


class EktuProctoringConfig(AppConfig):
    name = "lms.djangoapps.ektu_proctoring"
    label = "ektu_proctoring"
    verbose_name = "EKTU proctoring"
    default_auto_field = "django.db.models.BigAutoField"

    plugin_app = {
        "url_config": {
            "lms.djangoapp": {
                "namespace": "ektu_proctoring",
                "regex": r"^proctoring/",
                "relative_path": "urls",
            },
        },
    }
