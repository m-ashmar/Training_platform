"""What a plan costs, and what this buyer was actually charged.

The owner (2026-09-21): a person trades in ONE currency, theirs, and every service is
buyable in either. So a plan keeps the price of record the operator set it in, and the
CHARGE is that price converted into the buyer's own currency at the operator's live
rate (`subscription.pricing.charge_for`).

`Payment.amount`/`currency` therefore mean "what was charged, in the buyer's currency",
and these four columns are the evidence of how that number came about: the price of
record and its unit, and the append-only `wallet.ExchangeRate` row it was struck at
with its value copied beside it.

Snapshotted, not looked up. The pound moves — that is why the rate is operator-set —
so a refund reverses at what was paid and a receipt reprinted next month still says
what happened this month. NULL on every row written before the platform sold in two
currencies: nothing was converted for those, and `amount`/`currency` are the whole
truth about them.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("subscription", "0013_receipt_date_and_the_featured_plan"),
        ("wallet", "0016_two_currencies_a_ledger_wallet_for_each"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="price_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10,
                                      null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="price_currency",
            field=models.CharField(blank=True, choices=[("SYP", "Syrian Pound"),
                                                        ("USD", "US Dollar")],
                                   default="", max_length=3),
        ),
        migrations.AddField(
            model_name="payment",
            name="exchange_rate",
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.PROTECT,
                                    related_name="payments", to="wallet.exchangerate"),
        ),
        migrations.AddField(
            model_name="payment",
            name="exchange_rate_value",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=18,
                                      null=True),
        ),
    ]
