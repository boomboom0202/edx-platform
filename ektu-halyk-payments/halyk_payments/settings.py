"""
Settings for the Halyk ePay integration.

Everything here is overridable from Tutor config. Nothing secret has a value in
this file: client id, client secret and the terminal are supplied by the
deployment and are the only things that need to change when the university
receives its production credentials.
"""


def plugin_settings(settings):
    """Inject defaults into the LMS settings (called by the Tutor plugin)."""

    # -- switches ---------------------------------------------------------
    # Off until the credentials are in place, so the button never appears on a
    # deployment that cannot actually take money.
    settings.HALYK_ENABLED = False
    # Point at the bank's test environment.
    settings.HALYK_TEST_MODE = True
    # Run the whole flow without contacting the bank at all. Intended for local
    # testing before the credentials arrive; refuses to start when DEBUG is off
    # (see apps.py) so it can never be switched on in production by accident.
    settings.HALYK_FAKE_GATEWAY = False

    # -- credentials (set these from Tutor config) ------------------------
    settings.HALYK_CLIENT_ID = ""
    settings.HALYK_CLIENT_SECRET = ""
    settings.HALYK_TERMINAL_ID = ""

    # -- endpoints --------------------------------------------------------
    # NOTE: these are the addresses published for Epay 2.0. They are kept in
    # settings precisely so they can be corrected from Tutor config without a
    # code change if the bank's documentation says otherwise.
    settings.HALYK_OAUTH_URL_TEST = "https://testoauth.homebank.kz/epay2/oauth2/token"
    settings.HALYK_OAUTH_URL_PROD = "https://epay-oauth.homebank.kz/oauth2/token"
    settings.HALYK_API_URL_TEST = "https://testepay.homebank.kz/api"
    settings.HALYK_API_URL_PROD = "https://epay-api.homebank.kz"
    settings.HALYK_WIDGET_JS_TEST = "https://test-epay.homebank.kz/payform/payment-api.js"
    settings.HALYK_WIDGET_JS_PROD = "https://epay.homebank.kz/payform/payment-api.js"

    # -- behaviour --------------------------------------------------------
    settings.HALYK_CURRENCY = "KZT"
    settings.HALYK_OAUTH_SCOPE = "webpay usermanagement transfer"
    settings.HALYK_REQUEST_TIMEOUT = 20

    # Course mode that a successful payment grants. Must exist on the course
    # with a price, otherwise checkout refuses to start.
    settings.HALYK_COURSE_MODE = "verified"

    # Optional hardening for the server-to-server callback. If the bank cannot
    # sign its callbacks, restrict them by source address instead: an empty list
    # means "accept from anywhere", which is only safe behind a trusted proxy.
    settings.HALYK_POSTLINK_IP_ALLOWLIST = []
    # If a shared secret is agreed with the bank, the callback must carry it.
    settings.HALYK_POSTLINK_SECRET = ""

    # Always re-check the amount and status against the bank's status API
    # before granting access, instead of trusting the callback body alone.
    settings.HALYK_VERIFY_WITH_STATUS_API = True
