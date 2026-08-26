"""
Release money the bank is holding on a card.

On a two-step terminal a payment that is not captured sits in ``AUTH`` — the
learner's money is blocked, unavailable to them, and nothing was earned by it.
That happens here whenever a payment was refused after the card was authorised.
Releasing it is the decent thing to do rather than waiting for the hold to
lapse.

    ./manage.py lms halyk_cancel --invoice 1000004
    ./manage.py lms halyk_cancel --invoice 1000004 --yes

Only works while the transaction is in AUTH. A payment that was charged has to
be refunded instead — see ``halyk_refund``.
"""
from django.core.management.base import BaseCommand, CommandError

from halyk_payments.client import HalykError
from halyk_payments.models import Payment
from halyk_payments.services import PaymentError, cancel_payment


class Command(BaseCommand):
    help = "Release money held on a card for a Halyk payment that came to nothing."

    def add_arguments(self, parser):
        parser.add_argument("--invoice", required=True, help="The invoice number.")
        parser.add_argument("--yes", action="store_true", help="Actually do it.")

    def handle(self, *args, **options):
        try:
            payment = Payment.objects.get(invoice_id=options["invoice"])
        except Payment.DoesNotExist:
            raise CommandError(f"No payment with invoice {options['invoice']}.")

        self.stdout.write(
            f"Invoice {payment.invoice_id}: {payment.amount} {payment.currency} "
            f"held for {payment.user}, currently {payment.status}"
        )
        if payment.failure_reason:
            self.stdout.write(f"  refused because: {payment.failure_reason}")

        if not options["yes"]:
            self.stdout.write(self.style.WARNING(
                "\nNothing done. Add --yes to release the hold."
            ))
            return

        try:
            payment = cancel_payment(payment.pk)
        except (PaymentError, HalykError) as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f"\nReleased. Invoice {payment.invoice_id} is now {payment.status}."
        ))
