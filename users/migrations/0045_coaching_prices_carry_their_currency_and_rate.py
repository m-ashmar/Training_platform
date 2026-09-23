"""A coaching price carries its currency, a converted month its rate, and a trainer's
share can be owed.

1. **`TrainerClientRelation.agreed_currency`** (SIDE_BUGS S-083). `agreed_rate` was a
   bare number, read at hold time in whatever currency the trainer's wallet had by
   then — so a trainer who invited at 500000 (pounds) and switched to dollars before
   the client accepted charged 500000 dollars. EXISTING ROWS with an `agreed_rate` are
   backfilled with the trainer's CURRENT wallet currency (the platform's base currency
   when the trainer has no wallet row), which is the currency the code has been reading
   that number in all along — so nothing any existing row would be charged changes.
   Rows with no `agreed_rate` stay blank.

2. **`CoachingPeriod.price_amount`, `price_currency`, `exchange_rate`,
   `exchange_rate_value`** (SIDE_BUGS S-151): the price of record a month was charged
   for and the rate that converted it, the snapshot `subscription.Payment` already
   keeps. EXISTING MONTHS are left NULL/blank: which of them crossed a currency, and at
   what rate, was never recorded, and a guessed snapshot is worse than none.

3. **`CoachingPeriod.payout_owed_since`** (SIDE_BUGS S-082): the trainer's share is
   still in escrow because it had to cross a currency and the operator's rate was
   missing or stale. NULL on every existing month — every one of them was either paid
   out or refused whole.

Reversible: the reverse drops the columns; the backfill has nothing to undo.
"""
import django.db.models.deletion
from django.db import migrations, models


def quote_existing_agreements_in_the_trainers_currency(apps, schema_editor):
    from django.conf import settings as s

    TrainerClientRelation = apps.get_model("users", "TrainerClientRelation")
    Wallet = apps.get_model("wallet", "Wallet")

    base = getattr(s, "PLATFORM_CURRENCY", "SYP")
    rows = TrainerClientRelation.objects.filter(agreed_rate__isnull=False, agreed_currency="")
    trainers = set(rows.values_list("trainer_id", flat=True))
    currencies = dict(Wallet.objects.filter(owner_id__in=trainers)
                      .values_list("owner_id", "currency"))
    for trainer_id in trainers:
        rows.filter(trainer_id=trainer_id).update(
            agreed_currency=currencies.get(trainer_id, base))


def noop(apps, schema_editor):
    """No reverse work: dropping the column drops the value with it."""


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0044_a_coaching_month_is_priced_in_a_currency"),
        ("wallet", "0017_agent_topup_caps_per_currency"),
    ]

    operations = [
        migrations.AddField(
            model_name="trainerclientrelation",
            name="agreed_currency",
            field=models.CharField(blank=True, default="", max_length=3),
        ),
        migrations.AddField(
            model_name="coachingperiod",
            name="price_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12,
                                      null=True),
        ),
        migrations.AddField(
            model_name="coachingperiod",
            name="price_currency",
            field=models.CharField(blank=True, default="", max_length=3),
        ),
        migrations.AddField(
            model_name="coachingperiod",
            name="exchange_rate",
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.PROTECT,
                                    related_name="coaching_periods",
                                    to="wallet.exchangerate"),
        ),
        migrations.AddField(
            model_name="coachingperiod",
            name="exchange_rate_value",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=18,
                                      null=True),
        ),
        migrations.AddField(
            model_name="coachingperiod",
            name="payout_owed_since",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(quote_existing_agreements_in_the_trainers_currency, noop),
    ]
