"""
Bring the payment row in line with the bank's documented contract.

``invoice_id`` becomes a 6-to-15 digit number derived from the row's primary
key, so it is nullable for the moment between the insert and the update that
fills it in. ``secret_hash`` is the per-payment secret ePay echoes back on
postLink.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("halyk_payments", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="invoice_id",
            field=models.CharField(
                blank=True, db_index=True, max_length=15, null=True, unique=True,
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="secret_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
