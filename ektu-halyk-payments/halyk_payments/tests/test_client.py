"""
Tests for the parts of the bank's contract we have to get exactly right.

Invoice numbering and the status response are the two places where a small
misreading of the documentation turns into a payment that silently fails or a
course that opens without money.
"""
import pytest

from halyk_payments.client import (
    TransactionStatus,
    invoice_number,
    new_secret_hash,
    truncate_description,
)


def test_invoice_numbers_are_unique_on_their_last_six_digits(settings):
    """
    ePay requires uniqueness across the last six digits, not just overall.

    This is why the number comes from the primary key: a random one collides
    here after a few thousand orders.
    """
    settings.HALYK_INVOICE_BASE = 1_000_000
    tails = {invoice_number(pk)[-6:] for pk in range(1, 5000)}
    assert len(tails) == 4999


def test_invoice_numbers_stay_inside_the_banks_length_limits(settings):
    settings.HALYK_INVOICE_BASE = 1_000_000
    for pk in (1, 999, 123456, 8_000_000):
        assert 6 <= len(invoice_number(pk)) <= 15
        assert invoice_number(pk).isdigit()


def test_an_impossible_base_is_refused(settings):
    from halyk_payments.client import HalykError

    settings.HALYK_INVOICE_BASE = 10 ** 16
    with pytest.raises(HalykError):
        invoice_number(1)


def test_secret_hashes_differ():
    assert new_secret_hash() != new_secret_hash()


def test_a_long_description_is_cut_to_what_the_bank_accepts():
    """Exceeding 125 bytes is reasonCode 3298, a hard error, not a truncation."""
    assert len(truncate_description("a" * 400).encode()) <= 125
    # Cyrillic costs two bytes a character, and the cut must land on a
    # character boundary rather than half way through one.
    cut = truncate_description("я" * 200)
    assert len(cut.encode()) <= 125
    assert set(cut) == {"я"}


def test_a_short_description_is_left_alone():
    assert truncate_description("Hydrology") == "Hydrology"


# -- the status API ----------------------------------------------------------

def _status(result_code="100", status_name="CHARGE", amount=50000, amount_bonus=0):
    return TransactionStatus({
        "resultCode": result_code,
        "resultMessage": "SUCCESS",
        "transaction": {
            "invoiceID": "1000001",
            "amount": amount,
            "amountBonus": amount_bonus,
            "statusName": status_name,
            "terminalID": "67e34d63-102f-4bd1-898e-370781d0074d",
            "reference": "411111111117",
        },
    })


def test_a_charged_transaction_reads_as_finished_and_successful():
    status = _status()
    assert status.ok
    assert not status.in_progress
    assert status.status_name == "CHARGE"
    assert int(status.amount) == 50000
    assert status.terminal == "67e34d63-102f-4bd1-898e-370781d0074d"


def test_result_code_100_does_not_mean_the_payment_succeeded():
    """resultCode says the *request* worked; statusName says what happened."""
    status = _status(status_name="FAILED")
    assert status.ok
    assert status.status_name == "FAILED"


@pytest.mark.parametrize("result_code", ["107", "102", "103"])
def test_unfinished_result_codes_read_as_in_progress(result_code):
    assert _status(result_code=result_code).in_progress


@pytest.mark.parametrize("status_name", ["NEW", "FINGERPRINT"])
def test_unfinished_statuses_read_as_in_progress(status_name):
    assert _status(status_name=status_name).in_progress


def test_a_rejected_request_carries_no_transaction():
    status = TransactionStatus({"resultCode": "101", "resultMessage": "reject",
                                "transaction": None})
    assert not status.ok
    assert status.amount is None
    assert status.status_name == ""


def test_a_fractional_amount_survives_as_a_decimal():
    """The bank returns amounts as JSON numbers, so float rounding must not creep in."""
    from decimal import Decimal
    assert _status(amount=12.22).amount == Decimal("12.22")


# -- loyalty bonuses ---------------------------------------------------------

def test_bonuses_count_towards_what_was_settled():
    """
    A cardholder can cover part of an order with Halyk bonuses; the merchant is
    paid the whole of it. Reading only the card part refuses a paid order.
    """
    from decimal import Decimal
    from halyk_payments.client import total_paid

    # The callback that was refused in production: 50 tenge as 15 + 35 bonuses.
    assert total_paid({"amount": 15, "amount_bonus": 35}) == Decimal(50)
    # The status API spells the same field differently.
    assert total_paid({"amount": 12.22, "amountBonus": 10}) == Decimal("22.22")
    assert _status(amount=15, amount_bonus=35).total == Decimal(50)


def test_a_short_payment_is_still_short():
    from decimal import Decimal
    from halyk_payments.client import total_paid

    assert total_paid({"amount": 15, "amount_bonus": 5}) == Decimal(20)


def test_an_amount_the_bank_did_not_send_reads_as_unknown():
    """Absent is not zero: nothing to compare is different from nothing paid."""
    from halyk_payments.client import total_paid

    assert total_paid({}) is None
    assert total_paid({"amount": "nonsense"}) is None
