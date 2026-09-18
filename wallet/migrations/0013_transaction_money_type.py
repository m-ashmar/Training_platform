"""Which of the four kinds of money each ledger row moved.

New rows are typed by the code that writes them. Rows written before this are typed from
what they already say about themselves, in this order:

* `metadata.original_payment_id` — a subscription payment's mint, payout or refund leg:
  PLATFORM_SUB.
* `metadata.purpose` or `metadata.hold_reference` — a coaching hold, its release or its
  settlement (`users/trainer_relations.py` is the only writer of either key):
  COACHING_1TO1.
* a reversal of a row typed above takes that row's kind.
* everything else is a plain top-up or transfer, which is the column's default.
"""
from django.conf import settings
from django.db import migrations, models


def type_history(apps, schema_editor):
    Transaction = apps.get_model("wallet", "Transaction")
    rows = Transaction.objects.all()
    rows.filter(metadata__has_key="original_payment_id").update(money_type="PLATFORM_SUB")
    (rows.filter(models.Q(metadata__has_key="purpose") | models.Q(metadata__has_key="hold_reference"))
         .exclude(metadata__has_key="original_payment_id")
         .update(money_type="COACHING_1TO1"))
    for kind in ("PLATFORM_SUB", "COACHING_1TO1"):
        rows.filter(reverses__money_type=kind).update(money_type=kind)


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0012_audit_log_refuses_delete_at_the_database"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="money_type",
            field=models.CharField(
                choices=[
                    ("PLATFORM_SUB", "Platform subscription"),
                    ("COACHING_1TO1", "One-to-one coaching fee"),
                    ("ROUTINE_PRODUCT", "Routine product"),
                    ("WALLET_MOVE", "Wallet top-up or transfer"),
                ],
                db_index=True,
                default="WALLET_MOVE",
                max_length=16,
            ),
        ),
        migrations.RunPython(type_history, migrations.RunPython.noop),
    ]
