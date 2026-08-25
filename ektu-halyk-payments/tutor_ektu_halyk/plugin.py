"""
Tutor plugin: installs the Halyk payment app into the LMS image and supplies
its configuration.

The app itself registers its URLs and settings through the Open edX Django
plugin API, so this file only has to install the package and pass the values
the bank gave you. Moving from the test terminal to production is a config
change plus an image rebuild — no code edits.

    tutor plugins enable ektu-halyk
    tutor config save \
      --set HALYK_ENABLED=true \
      --set HALYK_TEST_MODE=true \
      --set HALYK_CLIENT_ID=... \
      --set HALYK_CLIENT_SECRET=... \
      --set HALYK_TERMINAL_ID=...
    tutor images build openedx
    tutor local start -d
"""
from tutor import hooks

__version__ = "0.1.0"

# Where pip should get the app from. Override with a local path while developing:
#   tutor config save --set HALYK_APP_SOURCE=/openedx/ektu-halyk-payments
PACKAGE_DEFAULT = "git+https://github.com/boomboom0202/ektu-halyk-payments@main"


########################################
# CONFIGURATION
########################################

hooks.Filters.CONFIG_DEFAULTS.add_items([
    ("EKTU_HALYK_VERSION", __version__),
    ("HALYK_APP_SOURCE", PACKAGE_DEFAULT),

    # Off by default: a deployment without credentials must not offer to take money.
    ("HALYK_ENABLED", False),
    ("HALYK_TEST_MODE", True),
    # Local end-to-end testing without the bank. Guarded by DEBUG in the app.
    ("HALYK_FAKE_GATEWAY", False),

    ("HALYK_CURRENCY", "KZT"),
    ("HALYK_COURSE_MODE", "verified"),
    ("HALYK_VERIFY_WITH_STATUS_API", True),
    ("HALYK_POSTLINK_IP_ALLOWLIST", []),
])

# Written into the Tutor config once and never committed to the repository.
hooks.Filters.CONFIG_UNIQUE.add_items([
    ("HALYK_CLIENT_ID", ""),
    ("HALYK_CLIENT_SECRET", ""),
    ("HALYK_TERMINAL_ID", ""),
    ("HALYK_POSTLINK_SECRET", ""),
])


########################################
# INSTALL THE APP INTO THE IMAGE
########################################

hooks.Filters.ENV_PATCHES.add_item((
    "openedx-dockerfile-post-python-requirements",
    'RUN pip install "{{ HALYK_APP_SOURCE }}"',
))


########################################
# SETTINGS
########################################

# The app ships its own defaults; these lines only carry the deployment's
# values across. Booleans and lists are rendered as Python literals by Tutor.
_SETTINGS = """
# ---- Halyk ePay ----
HALYK_ENABLED = {{ "True" if HALYK_ENABLED else "False" }}
HALYK_TEST_MODE = {{ "True" if HALYK_TEST_MODE else "False" }}
HALYK_FAKE_GATEWAY = {{ "True" if HALYK_FAKE_GATEWAY else "False" }}
HALYK_VERIFY_WITH_STATUS_API = {{ "True" if HALYK_VERIFY_WITH_STATUS_API else "False" }}
HALYK_CLIENT_ID = "{{ HALYK_CLIENT_ID }}"
HALYK_CLIENT_SECRET = "{{ HALYK_CLIENT_SECRET }}"
HALYK_TERMINAL_ID = "{{ HALYK_TERMINAL_ID }}"
HALYK_POSTLINK_SECRET = "{{ HALYK_POSTLINK_SECRET }}"
HALYK_CURRENCY = "{{ HALYK_CURRENCY }}"
HALYK_COURSE_MODE = "{{ HALYK_COURSE_MODE }}"
HALYK_POSTLINK_IP_ALLOWLIST = {{ HALYK_POSTLINK_IP_ALLOWLIST }}
"""

hooks.Filters.ENV_PATCHES.add_item(("openedx-lms-production-settings", _SETTINGS))
hooks.Filters.ENV_PATCHES.add_item(("openedx-lms-development-settings", _SETTINGS))
