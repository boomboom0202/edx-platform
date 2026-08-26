"""
Record what the money operations need.

The bank addresses cancellations and refunds by its own transaction id, which
arrives in the postLink; without it a payment can only be undone by hand in the
merchant portal. ``refunded_amount`` keeps a second refund from exceeding what
is left.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("halyk_payments", "0002_invoice_number_and_secret_hash"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="transaction_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="payment",
            name="refunded_amount",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
