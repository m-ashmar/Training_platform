"""The date on the receipt, and the plan the Plans screen badges.

`Payment.completed_at` is left NULL for rows completed before it existed rather than
backfilled from `updated_at`: `updated_at` moves on a refund, so for an old refunded
payment it is not the date the money arrived, and writing a guess into a column that
is meant to be evidence is worse than admitting it is unknown. `subscription.receipts`
falls back to `updated_at` for exactly those rows and says so.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("subscription", "0012_money_type_and_entitlement_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="completed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="subscriptionplan",
            name="is_featured",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
