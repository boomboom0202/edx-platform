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
      --set HALYK_CLIENT_ID=test \
      --set HALYK_CLIENT_SECRET=<the sandbox secret from epayment.kz/docs> \
      --set HALYK_TERMINAL_ID=67e34d63-102f-4bd1-898e-370781d0074d
    tutor images build openedx
    tutor local start -d

The values above are the bank's published sandbox terminal. Replace all three
with the university's own when they arrive and set HALYK_TEST_MODE=false.
"""
from tutor import hooks

__version__ = "0.1.0"

# Where pip should get the app from. It lives in a subdirectory of the platform
# repository, which pip supports through the "#subdirectory=" fragment.
#
# Pin a commit instead of a branch when it matters that a rebuild produces the
# same image, and remember that pip caches by URL: a rebuild after pushing to
# the same branch needs `tutor images build --no-cache openedx`.
PACKAGE_DEFAULT = (
    "git+https://github.com/boomboom0202/edx-platform@release/teak"
    "#subdirectory=ektu-halyk-payments"
)


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
# The bank's sandbox credentials go here too — they are public, but they are
# still credentials and still belong in config rather than in the code.
hooks.Filters.CONFIG_UNIQUE.add_items([
    ("HALYK_CLIENT_ID", ""),
    ("HALYK_CLIENT_SECRET", ""),
    ("HALYK_TERMINAL_ID", ""),
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
HALYK_CURRENCY = "{{ HALYK_CURRENCY }}"
HALYK_COURSE_MODE = "{{ HALYK_COURSE_MODE }}"
HALYK_POSTLINK_IP_ALLOWLIST = {{ HALYK_POSTLINK_IP_ALLOWLIST }}
"""

hooks.Filters.ENV_PATCHES.add_item(("openedx-lms-production-settings", _SETTINGS))
hooks.Filters.ENV_PATCHES.add_item(("openedx-lms-development-settings", _SETTINGS))
