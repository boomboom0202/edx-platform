"""
Every call to Halyk lives in this module.

Written against the ePay 2.0 documentation at https://epayment.kz/docs —
sections "Платежный виджет" (token request and payment object), "Статус
транзакции" (status API, resultCode and statusName) and "Коды ошибок"
(reasonCode). The rest of the app talks to :class:`HalykClient` and never
builds a request of its own.
"""
import logging
import secrets
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests
from django.conf import settings

log = logging.getLogger(__name__)


class HalykError(Exception):
    """Any failure while talking to the bank."""


# -- what the bank calls things ------------------------------------------------

#: ``resultCode`` of the status API when the request itself succeeded. It says
#: nothing about the payment — that is ``statusName``.
RESULT_OK = "100"
#: ``resultCode`` meaning the operation has not finished yet; ask again later.
RESULT_IN_PROGRESS = "107"
#: ``resultCode`` meaning the invoice is not known to ePay yet.
RESULT_NOT_FOUND = "102"
#: ``resultCode`` meaning "repeat the request or contact support".
RESULT_RETRY = "103"

#: Money has actually left the card. This is the outcome of a one-step (SMS)
#: payment, which is how a terminal is set up by default.
STATUS_CHARGED = "CHARGE"
#: Money is only blocked on the card, awaiting a capture. A two-step (DMS)
#: terminal ends here, and this plugin does not issue captures — see the README.
STATUS_AUTHORISED = "AUTH"
#: Outcomes that are not final: the transaction is still moving.
STATUS_IN_PROGRESS = frozenset({"NEW", "FINGERPRINT"})

#: ``reasonCode`` values documented as "не финальный, необходимо запросить
#: статус оплаты" — the payment may still succeed, so a callback carrying one of
#: these must not be recorded as a failure. Transfer-only codes are left out;
#: this plugin never makes transfers.
RETRYABLE_REASON_CODES = frozenset({
    -31, 100, 293, 454, 690, 1267, 1268, 1269,
    1563, 1564, 2358, 2656, 3014, 3225, 3240,
})

#: The bank requires 6 to 15 digits.
INVOICE_MIN_DIGITS = 6
INVOICE_MAX_DIGITS = 15

#: "description" is capped at 125 bytes, and the bank counts a Cyrillic
#: character as two (reasonCode 3298 is "too long").
DESCRIPTION_MAX_BYTES = 125

#: The bank refuses a refund smaller than this.
MIN_REFUND = Decimal(10)


def invoice_number(sequence):
    """
    Turn a payment's primary key into an invoice number the bank will accept.

    ePay wants the number unique per order *and* unique across its last six
    digits. A random number cannot promise the second part — twelve random
    digits start colliding on their last six after a few thousand orders — so
    the number is derived from the payment row's own primary key, which is
    monotonic and never reused.
    """
    base = int(getattr(settings, "HALYK_INVOICE_BASE", 1_000_000))
    number = str(base + int(sequence))
    if not INVOICE_MIN_DIGITS <= len(number) <= INVOICE_MAX_DIGITS:
        raise HalykError(
            f"Invoice number {number} is not between {INVOICE_MIN_DIGITS} and "
            f"{INVOICE_MAX_DIGITS} digits; check HALYK_INVOICE_BASE."
        )
    return number


def new_secret_hash():
    """
    The per-payment secret that ties a callback to a checkout we started.

    ePay takes ``secret_hash`` in the token request and returns it on postLink,
    so a callback that carries the right value can only have come from a payment
    this server opened. It is generated here, stored on the payment row and
    never sent to the browser.
    """
    return secrets.token_hex(16)


def _decimal(value):
    """A JSON number as a Decimal, or None if it is not one."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, InvalidOperation):
        return None


def total_paid(payload):
    """
    What an order was actually settled with, or None if the bank did not say.

    A Halyk cardholder can pay part of an order with loyalty bonuses. The bank
    then reports the card part as ``amount`` and the rest as ``amount_bonus``
    (``amountBonus`` in the status API), and pays the merchant the whole of it.
    Reading ``amount`` alone rejects a fully paid order — a 50 tenge course
    settled as 15 in cash and 35 in bonuses looks like an underpayment of 35.
    """
    amount = _decimal(payload.get("amount"))
    if amount is None:
        return None
    bonus = _decimal(payload.get("amount_bonus"))
    if bonus is None:
        bonus = _decimal(payload.get("amountBonus"))
    return amount + (bonus or Decimal(0))


def truncate_description(text):
    """Cut a description down to what the bank will accept."""
    encoded = text.encode("utf-8")
    if len(encoded) <= DESCRIPTION_MAX_BYTES:
        return text
    # Cut on a character boundary, not in the middle of a multi-byte one.
    return encoded[:DESCRIPTION_MAX_BYTES].decode("utf-8", "ignore")


class TransactionStatus:
    """
    The status API's answer, read the way the documentation describes it.

    Three questions matter and each has its own property: did the request work
    (``ok``), is the transaction still moving (``in_progress``), and what
    happened to the money (``status_name``).
    """

    def __init__(self, body):
        self.body = body or {}
        self.result_code = str(self.body.get("resultCode", ""))
        self.result_message = str(self.body.get("resultMessage", ""))
        self.transaction = self.body.get("transaction") or {}

    @property
    def ok(self):
        return self.result_code == RESULT_OK

    @property
    def in_progress(self):
        """The bank has not decided yet, so neither should we."""
        if self.result_code in (RESULT_IN_PROGRESS, RESULT_NOT_FOUND, RESULT_RETRY):
            return True
        return self.status_name in STATUS_IN_PROGRESS

    @property
    def status_name(self):
        return str(self.transaction.get("statusName", "")).upper()

    @property
    def amount(self):
        """The part of the order settled with the card, as a Decimal."""
        return _decimal(self.transaction.get("amount"))

    @property
    def amount_bonus(self):
        """The part settled with Halyk loyalty bonuses."""
        return _decimal(self.transaction.get("amountBonus")) or Decimal(0)

    @property
    def total(self):
        """
        What the order was actually settled with, or None if the bank did not
        say. Card and bonuses both count: the merchant is paid the whole of it.
        """
        amount = self.amount
        return None if amount is None else amount + self.amount_bonus

    @property
    def terminal(self):
        return str(self.transaction.get("terminalID", ""))

    @property
    def transaction_id(self):
        """The bank's id for this transaction — what cancel and refund need."""
        return str(self.transaction.get("id", ""))

    @property
    def reference(self):
        return str(self.transaction.get("reference", ""))

    @property
    def card_mask(self):
        return str(self.transaction.get("cardMask", ""))

    def __str__(self):
        return f"resultCode={self.result_code} statusName={self.status_name}"


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

    @property
    def widget_origin(self):
        """
        Where the widget and its payment form come from.

        Worth knowing so the checkout page can open the connection early: the
        card form is an iframe from this host, and it is only requested once the
        learner clicks, so the whole handshake happens while they wait.
        """
        parts = urlsplit(self.widget_js_url)
        return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""

    def is_configured(self):
        return all([self.client_id, self.client_secret, self.terminal])

    # -- calls ------------------------------------------------------------

    def get_payment_token(self, invoice_id, amount, currency, secret_hash):
        """
        The token the payment widget needs.

        The documentation is explicit that a token is per-operation: "для каждой
        операции необходимо получать и использовать оригинальный токен". So
        nothing here is cached, and the token is scoped to this invoice and this
        amount — which is also why a learner cannot replay someone else's.

        The whole response is returned, not just ``access_token``: the widget
        wants the entire object as its ``auth`` field.
        """
        return self._token({
            "invoiceID": str(invoice_id),
            "secret_hash": secret_hash,
            "amount": int(amount),
            "currency": currency,
        })

    def get_api_token(self):
        """
        A token for the status API, which is not tied to an invoice.

        The status-check section documents the token request without
        ``invoiceID``/``amount``, so those are deliberately absent.
        """
        return self._token()

    def get_payment_status(self, invoice_id, access_token):
        """
        Ask the bank what actually happened to an invoice.

        This is what decides whether access is granted, so a forged callback
        cannot enroll anybody.
        """
        url = f"{self.api_url}/check-status/payment/transaction/{invoice_id}"
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return TransactionStatus(response.json())
        except requests.RequestException as exc:
            raise HalykError(f"Status check failed: {exc}") from exc
        except ValueError as exc:
            raise HalykError("Status check returned a non-JSON body") from exc

    def cancel_operation(self, transaction_id, access_token):
        """
        Release money that is only blocked on the card.

        Works solely on a transaction still in ``AUTH``; once it is charged the
        way back is a refund, not a cancellation.
        """
        return self._operation(transaction_id, "cancel", access_token)

    def refund_operation(self, transaction_id, access_token,
                         amount=None, external_id=None):
        """
        Give money back on a transaction that was actually charged.

        ``amount`` refunds part of it; left out, the whole of it. The bank
        refuses anything under ten tenge.
        """
        params = {}
        if amount is not None:
            params["amount"] = int(amount)
        if external_id:
            params["externalID"] = str(external_id)
        return self._operation(transaction_id, "refund", access_token, params)

    # -- plumbing ---------------------------------------------------------

    def _token(self, extra=None):
        payload = {
            "grant_type": "client_credentials",
            "scope": getattr(settings, "HALYK_OAUTH_SCOPE", ""),
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "terminal": self.terminal,
        }
        payload.update(extra or {})

        data = self._post(self.oauth_url, payload, what="token request")
        if not data.get("access_token"):
            raise HalykError("The token response did not contain an access_token")
        return data

    def _operation(self, transaction_id, action, access_token, params=None):
        """
        One of the bank's operations on an existing transaction.

        These are addressed by the bank's own transaction id — the ``id`` field
        of the postLink, not our invoice number. Success is an empty HTTP 200;
        a refusal is a 400 carrying a code and a message, and that message is
        the only explanation anyone will get, so it is preserved.
        """
        if not transaction_id:
            raise HalykError(f"Cannot {action}: the bank's transaction id is unknown")

        url = f"{self.api_url}/operation/{transaction_id}/{action}"
        try:
            response = requests.post(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            log.error("Halyk %s of %s failed: %s", action, transaction_id, exc)
            raise HalykError(f"{action} failed: {exc}") from exc

        if response.status_code == 200:
            log.info("Halyk %s of %s accepted", action, transaction_id)
            return True

        try:
            body = response.json()
            detail = f"code {body.get('code')} — {body.get('message') or '(no message)'}"
        except ValueError:
            detail = f"HTTP {response.status_code}"
        log.error("Halyk %s of %s refused: %s", action, transaction_id, detail)
        raise HalykError(f"{action} refused: {detail}")

    def _post(self, url, payload, what):
        try:
            # form-data, as the documentation specifies for the token endpoint.
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
