"""A coaching month carries the currency it was charged in.

The platform sells in Syrian pounds AND US dollars (the owner, 2026-09-21), and the
payout for a month has to know which: escrow and the platform's revenue are one ledger
wallet PER CURRENCY now (SIDE_BUGS S-001), so `pay_out` resolves both from the month's
own unit rather than from a default that used to be whichever currency arrived first.

`price` had no unit at all. Backfilled from the hold transaction the month names
(`hold_reference` -> `wallet.Transaction.currency`), which is the money that was
actually moved and therefore the only honest answer; a month whose hold row is gone
falls back to the platform's base currency, which is what it would have been charged
in when it was written.
"""
from django.db import migrations, models


def a_month_is_priced_in_the_currency_it_was_held_in(apps, schema_editor):
    CoachingPeriod = apps.get_model("users", "CoachingPeriod")
    Transaction = apps.get_model("wallet", "Transaction")

    holds = dict(Transaction.objects.filter(
        reference_id__in=CoachingPeriod.objects.exclude(hold_reference="")
        .values_list("hold_reference", flat=True)
    ).values_list("reference_id", "currency"))
    for period in CoachingPeriod.objects.exclude(hold_reference="").iterator():
        currency = holds.get(period.hold_reference)
        if currency and currency != period.currency:
            CoachingPeriod.objects.filter(pk=period.pk).update(currency=currency)


def noop(apps, schema_editor):
    """No reverse work: dropping the column drops the value with it."""


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0043_coaching_frozen_and_activity_answered"),
        ("wallet", "0016_two_currencies_a_ledger_wallet_for_each"),
    ]

    operations = [
        migrations.AddField(
            model_name="coachingperiod",
            name="currency",
            field=models.CharField(default="SYP", max_length=8),
        ),
        migrations.RunPython(a_month_is_priced_in_the_currency_it_was_held_in, noop),
    ]
