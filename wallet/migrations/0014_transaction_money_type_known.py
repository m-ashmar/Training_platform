"""The ledger refuses a kind of money it does not know.

Its own migration, after the backfill's: Postgres will not ALTER a table that still has
row changes pending in the same transaction.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0013_transaction_money_type"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="transaction",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("money_type__in",
                     ["PLATFORM_SUB", "COACHING_1TO1", "ROUTINE_PRODUCT", "WALLET_MOVE"])
                ),
                name="tx_money_type_known",
            ),
        ),
    ]
