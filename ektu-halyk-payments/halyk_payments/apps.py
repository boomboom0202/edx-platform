"""
App configuration.

Uses the Open edX Django plugin API: once the package is pip-installed, the LMS
discovers it through the ``lms.djangoapp`` entry point and wires up both the
URLs and the settings on its own. Nothing has to be patched into edx-platform.
"""
import logging

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured

log = logging.getLogger(__name__)


class HalykPaymentsConfig(AppConfig):
    name = "halyk_payments"
    verbose_name = "Halyk ePay payments"
    default_auto_field = "django.db.models.BigAutoField"

    plugin_app = {
        "url_config": {
            "lms.djangoapp": {
                "namespace": "halyk_payments",
                "regex": r"^halyk/",
                "relative_path": "urls",
            },
        },
        "settings_config": {
            "lms.djangoapp": {
                "common": {"relative_path": "settings"},
                "production": {"relative_path": "settings"},
                "test": {"relative_path": "settings"},
            },
        },
    }

    def ready(self):
        from django.conf import settings

        # The fake gateway grants course access without any money changing
        # hands. Refuse to start rather than let it reach a real deployment.
        if getattr(settings, "HALYK_FAKE_GATEWAY", False) and not settings.DEBUG:
            raise ImproperlyConfigured(
                "HALYK_FAKE_GATEWAY is on but DEBUG is off. The fake gateway "
                "enrolls learners without payment and must never run in "
                "production."
            )

        if getattr(settings, "HALYK_ENABLED", False) \
                and not getattr(settings, "HALYK_TEST_MODE", True) \
                and not getattr(settings, "HALYK_CLIENT_SECRET", ""):
            log.error(
                "Halyk payments are enabled in production mode but no client "
                "secret is configured; checkout will fail."
            )
