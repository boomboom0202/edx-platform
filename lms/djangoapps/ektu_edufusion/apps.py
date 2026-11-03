"""
App configuration.

Registered through the ``lms.djangoapp`` entry point in setup.py, so the LMS
mounts the URLs by itself.
"""
from django.apps import AppConfig


class EktuEdufusionConfig(AppConfig):
    name = "lms.djangoapps.ektu_edufusion"
    label = "ektu_edufusion"
    verbose_name = "EKTU Edufusion catalogue"

    plugin_app = {
        "url_config": {
            "lms.djangoapp": {
                "namespace": "ektu_edufusion",
                "regex": r"^edufusion/",
                "relative_path": "urls",
            },
        },
    }
