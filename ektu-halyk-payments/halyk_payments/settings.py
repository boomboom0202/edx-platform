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
    # testing; refuses to start when DEBUG is off (see apps.py) so it can never
    # be switched on in production by accident.
    settings.HALYK_FAKE_GATEWAY = False

    # -- credentials (set these from Tutor config) ------------------------
    # The bank's public sandbox values are ClientID "test", TerminalID
    # 67e34d63-102f-4bd1-898e-370781d0074d and the secret published in the
    # documentation. They belong in Tutor config like any other credential.
    settings.HALYK_CLIENT_ID = ""
    settings.HALYK_CLIENT_SECRET = ""
    settings.HALYK_TERMINAL_ID = ""

    # -- endpoints --------------------------------------------------------
    # From https://epayment.kz/docs. Kept in settings so they can be corrected
    # from Tutor config without a code change.
    settings.HALYK_OAUTH_URL_TEST = "https://test-epay-oauth.epayment.kz/oauth2/token"
    settings.HALYK_OAUTH_URL_PROD = "https://epay-oauth.homebank.kz/oauth2/token"
    settings.HALYK_API_URL_TEST = "https://test-epay-api.epayment.kz"
    settings.HALYK_API_URL_PROD = "https://epay-api.homebank.kz"
    settings.HALYK_WIDGET_JS_TEST = "https://test-epay.epayment.kz/payform/payment-api.js"
    settings.HALYK_WIDGET_JS_PROD = "https://epay.homebank.kz/payform/payment-api.js"

    # -- behaviour --------------------------------------------------------
    settings.HALYK_CURRENCY = "KZT"
    # The exact scope string from the documentation. A shorter one is rejected.
    settings.HALYK_OAUTH_SCOPE = (
        "webapi usermanagement email_send verification statement statistics payment"
    )
    settings.HALYK_REQUEST_TIMEOUT = 20

    # Invoice numbers are this base plus the payment's primary key, which keeps
    # them inside the bank's 6-to-15 digit range and unique on their last six
    # digits. Raise it if invoice numbers must not collide with an older system.
    settings.HALYK_INVOICE_BASE = 1_000_000

    # Transaction outcomes that mean the learner has paid. Only CHARGE — money
    # off the card — counts. AUTH means a two-step (DMS) terminal is merely
    # holding the money, which is not the same as having been paid.
    settings.HALYK_ACCEPTED_STATUSES = ["CHARGE"]

    # On a two-step terminal, take the money as soon as the transaction is
    # verified instead of leaving it blocked. A course opens the instant it is
    # paid for, so there is nothing to wait for. Switching this off means
    # capturing by hand in the merchant portal, and no course opens until then.
    settings.HALYK_AUTO_CAPTURE = True

    # Course mode that a successful payment grants. Must exist on the course
    # with a price, otherwise checkout refuses to start.
    settings.HALYK_COURSE_MODE = "verified"

    # Optional hardening for the server-to-server callback. Authenticity is
    # already carried by the per-payment secret_hash the bank echoes back; this
    # narrows the callback to the bank's own addresses as well. Empty means
    # "accept from anywhere".
    settings.HALYK_POSTLINK_IP_ALLOWLIST = []

    # Always re-check the amount and status against the bank's status API
    # before granting access, instead of trusting the callback body alone.
    settings.HALYK_VERIFY_WITH_STATUS_API = True

    # -- wiring -----------------------------------------------------------
    # Open edX's track-selection page ends at identity verification, because
    # payment is meant to be the ecommerce service's job — and there is no
    # ecommerce service here. Without this, a learner clicking "Enroll now" on a
    # course we sell is walked through verification and never asked to pay.
    #
    # Appended, so it runs after authentication middleware and can read
    # request.user. Guarded because plugin settings are applied once per
    # settings module (common, production, test).
    middleware = "halyk_payments.middleware.PaidCourseCheckoutMiddleware"
    if middleware not in settings.MIDDLEWARE:
        settings.MIDDLEWARE = list(settings.MIDDLEWARE) + [middleware]
