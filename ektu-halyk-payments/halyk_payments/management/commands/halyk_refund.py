"""
Give money back on a payment, and take the course away with it.

Refunding through the merchant portal moves the money but leaves the learner
enrolled and the payment recorded as paid, so the platform and the bank stop
agreeing with each other. Doing it here keeps them in step.

    ./manage.py lms halyk_refund --invoice 1000004            # the whole of it
    ./manage.py lms halyk_refund --invoice 1000004 --amount 20  # part of it
    ./manage.py lms halyk_refund --invoice 1000004 --yes

Without --yes it only says what it would do. A full refund withdraws access; a
partial one leaves it, on the assumption that the learner keeps a course they
partly paid for. --keep-access and --withdraw-access overrule that.
"""
from django.core.management.base import BaseCommand, CommandError

from halyk_payments.client import HalykError
from halyk_payments.models import Payment
from halyk_payments.services import PaymentError, refund_payment


class Command(BaseCommand):
    help = "Refund a Halyk payment and withdraw the access it bought."

    def add_arguments(self, parser):
        parser.add_argument("--invoice", required=True, help="The invoice number.")
        parser.add_argument(
            "--amount", type=int,
            help="Refund only this much, in whole tenge. Default: everything left.",
        )
        parser.add_argument(
            "--external-id",
            help="Your own reference for this refund, passed to the bank.",
        )
        parser.add_argument("--yes", action="store_true", help="Actually do it.")
        access = parser.add_mutually_exclusive_group()
        access.add_argument("--keep-access", action="store_true",
                            help="Leave the learner enrolled.")
        access.add_argument("--withdraw-access", action="store_true",
                            help="Unenroll the learner even on a partial refund.")

    def handle(self, *args, **options):
        try:
            payment = Payment.objects.get(invoice_id=options["invoice"])
        except Payment.DoesNotExist:
            raise CommandError(f"No payment with invoice {options['invoice']}.")

        outstanding = payment.amount - payment.refunded_amount
        amount = options["amount"] or outstanding

        unenroll = None
        if options["keep_access"]:
            unenroll = False
        elif options["withdraw_access"]:
            unenroll = True

        self.stdout.write(
            f"Invoice {payment.invoice_id}: {payment.user} paid {payment.amount} "
            f"{payment.currency} for {payment.course_id}"
        )
        if payment.refunded_amount:
            self.stdout.write(f"  already refunded: {payment.refunded_amount}")
        self.stdout.write(f"  about to refund:  {amount} {payment.currency}")

        will_unenroll = unenroll if unenroll is not None else amount >= outstanding
        self.stdout.write(
            "  access:           " +
            ("withdrawn" if will_unenroll else "left as it is")
        )

        if not options["yes"]:
            self.stdout.write(self.style.WARNING(
                "\nNothing done. Add --yes to carry it out."
            ))
            return

        try:
            payment = refund_payment(
                payment.pk, amount=options["amount"],
                external_id=options["external_id"], unenroll=unenroll,
            )
        except (PaymentError, HalykError) as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(
            f"\nRefunded. Invoice {payment.invoice_id} is now {payment.status}, "
            f"{payment.refunded_amount} of {payment.amount} given back, "
            f"enrolled={payment.enrolled}."
        ))
