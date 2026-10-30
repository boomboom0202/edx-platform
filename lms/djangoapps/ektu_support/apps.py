"""
App configuration.

Registered through the ``lms.djangoapp`` entry point in setup.py, the same way
the platform's own plugin apps are, so the LMS mounts the URLs by itself.
"""
from django.apps import AppConfig


class EktuSupportConfig(AppConfig):
    name = "lms.djangoapps.ektu_support"
    label = "ektu_support"
    verbose_name = "EKTU support form"

    plugin_app = {
        "url_config": {
            "lms.djangoapp": {
                "namespace": "ektu_support",
                "regex": r"^help/ask/",
                "relative_path": "urls",
            },
        },
    }
