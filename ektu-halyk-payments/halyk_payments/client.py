"""
Every call to Halyk lives in this module.

The rest of the app talks to :class:`HalykClient` and never builds a request of
its own, so when the bank's documentation is available the whole integration is
corrected in one file.

Each method marked "CONTRACT" carries an assumption about the bank's API that
must be checked against the official documentation at https://epayment.kz/docs
before going live. They are deliberately collected here rather than scattered
through the views.
"""
import logging
import uuid

import requests
from django.conf import settings

log = logging.getLogger(__name__)


class HalykError(Exception):
    """Any failure while talking to the bank."""


def new_invoice_id():
    """
    A fresh invoice number.

    Epay invoice numbers are short and numeric-ish; a random 12 digit value is
    unique enough in practice and carries no information about the buyer.
    """
    return str(uuid.uuid4().int)[:12]


class HalykClient:
    """Thin wrapper over the Halyk ePay HTTP API."""

    def __init__(self):
        self.test_mode = bool(getattr(settings, "HALYK_TEST_MODE", True))
        self.client_id = getattr(settings, "HALYK_CLIENT_ID", "")
        self.client_secret = getattr(settings, "HALYK_CLIENT_SECRET", "")
        self.terminal = getattr(settings, "HALYK_TERMINAL_ID", "")
        self.timeout = getattr(settings, "HALYK_REQUEST_TIMEOUT", 20)

    # -- addresses --------------------------------------------------------

    @property
    def oauth_url(self):
        return (settings.HALYK_OAUTH_URL_TEST if self.test_mode
                else settings.HALYK_OAUTH_URL_PROD)

    @property
    def api_url(self):
        return (settings.HALYK_API_URL_TEST if self.test_mode
                else settings.HALYK_API_URL_PROD).rstrip("/")

    @property
    def widget_js_url(self):
        return (settings.HALYK_WIDGET_JS_TEST if self.test_mode
                else settings.HALYK_WIDGET_JS_PROD)

    def is_configured(self):
        return all([self.client_id, self.client_secret, self.terminal])

    # -- calls ------------------------------------------------------------

    def get_payment_token(self, invoice_id, amount, currency, account_id=""):
        """
        Obtain the token the payment widget needs.

        CONTRACT: Epay 2.0 scopes its token to a single invoice, so the amount
        and invoice number are part of the token request rather than of a later
        call. Confirm the exact field names before going live — if they differ,
        this dict is the only thing that changes.
        """
        payload = {
            "grant_type": "client_credentials",
            "scope": getattr(settings, "HALYK_OAUTH_SCOPE", "webpay"),
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "invoiceID": str(invoice_id),
            "amount": int(amount),
            "currency": currency,
            "terminal": self.terminal,
        }
        if account_id:
            payload["accountId"] = str(account_id)

        data = self._post(self.oauth_url, payload, what="token request")
        token = data.get("access_token")
        if not token:
            raise HalykError("The token response did not contain an access_token")
        return data

    def get_payment_status(self, invoice_id, access_token):
        """
        Ask the bank what actually happened to an invoice.

        This is what decides whether access is granted, so that a forged
        callback cannot enroll anybody.

        CONTRACT: verify the path and the shape of the response.
        """
        url = f"{self.api_url}/check-status/payment/transaction/{invoice_id}"
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise HalykError(f"Status check failed: {exc}") from exc
        except ValueError as exc:
            raise HalykError("Status check returned a non-JSON body") from exc

    # -- plumbing ---------------------------------------------------------

    def _post(self, url, payload, what):
        try:
            response = requests.post(url, data=payload, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            # Never log the payload: it carries the client secret.
            log.error("Halyk %s failed: %s", what, exc)
            raise HalykError(f"{what} failed") from exc
        except ValueError as exc:
            log.error("Halyk %s returned a non-JSON body", what)
            raise HalykError(f"{what} returned a non-JSON body") from exc
