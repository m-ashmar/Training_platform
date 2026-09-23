"""Agent top-up caps per currency (SIDE_BUGS S-059).

An agent top-up carried no currency and its caps were one number for both currencies,
so a cap set in pounds let the same NUMBER of dollars through. The existing
`daily_limit` / `monthly_limit` keep capping Syrian pounds — every wallet they were
ever minted into was SYP — and this adds `daily_limit_usd` / `monthly_limit_usd`.

EXISTING ROWS get 0 for the new pair, which means "no dollar top-ups" (the caps fail
closed): an agent tops up dollars only once an operator has set a number. Nothing else
on any row changes.

Reversible: the reverse drops the two columns.
"""
from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0016_two_currencies_a_ledger_wallet_for_each"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentprofile",
            name="daily_limit_usd",
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=12,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))]),
        ),
        migrations.AddField(
            model_name="agentprofile",
            name="monthly_limit_usd",
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=12,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))]),
        ),
    ]
