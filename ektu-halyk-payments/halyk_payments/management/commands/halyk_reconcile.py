"""
Settle payments the callback never resolved.

A postLink can fail to arrive — the bank could not reach us, the server was
restarting, or the checkout happened on a host the bank cannot call back at
all. The money is taken either way, so a lost callback means a learner who paid
and got nothing. This command asks the bank about every pending payment and
finishes the ones it confirms.

    ./manage.py lms halyk_reconcile
    ./manage.py lms halyk_reconcile --invoice 1000001
    ./manage.py lms halyk_reconcile --dry-run

Nothing here trusts anything but the bank: it grants access through exactly the
same check the callback uses, so running it cannot open a course that was not
paid for. Safe to run from cron.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from halyk_payments.models import Payment, PaymentStatus
from halyk_payments.services import confirm_with_bank, mark_failed, mark_paid_and_enroll


class Command(BaseCommand):
    help = "Ask the bank about pending Halyk payments and settle the confirmed ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--invoice",
            help="Settle one invoice instead of every pending one.",
        )
        parser.add_argument(
            "--max-age-hours", type=int, default=72,
            help="Ignore payments older than this. Default: 72.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would happen without enrolling anybody.",
        )

    def handle(self, *args, **options):
        payments = Payment.objects.filter(
            status=PaymentStatus.PENDING, enrolled=False,
        ).exclude(invoice_id=None)

        if options["invoice"]:
            payments = payments.filter(invoice_id=options["invoice"])
        else:
            cutoff = timezone.now() - timedelta(hours=options["max_age_hours"])
            payments = payments.filter(created__gte=cutoff)

        settled = refused = unknown = 0

        for payment in payments.order_by("created"):
            verdict, detail, transaction = confirm_with_bank(payment)

            if verdict is None:
                # Still in flight, or the bank did not answer. Leave it alone.
                unknown += 1
                self.stdout.write(f"{payment.invoice_id}: no answer yet — {detail}")
                continue

            if verdict is False:
                refused += 1
                self.stdout.write(f"{payment.invoice_id}: not paid — {detail}")
                if not options["dry_run"]:
                    mark_failed(payment.pk, reason=f"Bank says: {detail}")
                continue

            settled += 1
            self.stdout.write(self.style.SUCCESS(
                f"{payment.invoice_id}: paid, enrolling {payment.user} "
                f"in {payment.course_id}"
            ))
            if not options["dry_run"]:
                mark_paid_and_enroll(
                    payment.pk,
                    reference=transaction.reference[:128],
                    card_mask=transaction.card_mask[:32],
                    payload=transaction.body,
                )

        summary = f"{settled} settled, {refused} refused, {unknown} still open"
        if options["dry_run"]:
            summary += " (dry run, nothing was changed)"
        self.stdout.write(self.style.NOTICE(summary))
