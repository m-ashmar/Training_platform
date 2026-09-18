"""What each payment bought, and whether its trainer payout is still owed.

`covers_from`/`covers_until` let a refund take back exactly the period the refunded
payment granted, instead of one plan period whenever any other completed payment
existed. NULL on rows completed before this; the refund treats those as one period.

`payout_settled_at` NULL on a completed payment means the payout is OWED, and the retry
paths and the sweep send it again. Every payment already completed when this runs is
marked settled at its own `updated_at`: the receiver pays whoever is the client's trainer
AT PAYOUT TIME, so leaving history unmarked would let the first sweep pay today's trainer
for every old payment of a client who had no trainer then. A payout lost before this
migration is found by the ledger query — completed payments with no `transfer` leg —
and settled by hand with the idempotent receiver.
"""
from django.db import migrations, models
from django.db.models import F


def mark_history_settled(apps, schema_editor):
    Payment = apps.get_model("subscription", "Payment")
    Payment.objects.filter(status="completed", payout_settled_at__isnull=True).update(
        payout_settled_at=F("updated_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("subscription", "0010_backfill_plan_translation_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="covers_from",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="covers_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="payout_settled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                condition=models.Q(
                    ("payout_settled_at__isnull", True), ("status", "completed")
                ),
                fields=["updated_at"],
                name="payment_payout_owed_idx",
            ),
        ),
        migrations.RunPython(mark_history_settled, migrations.RunPython.noop),
    ]
