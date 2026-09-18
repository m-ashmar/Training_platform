"""Every payment says what kind of money it is; every entitlement says where its money came from.

`Payment.money_type`: a row in this table pays for a plan, so history is PLATFORM_SUB,
which is the column's default.

`Subscription.source`, for rows that already entitle someone:
  * a trial is `trial`;
  * otherwise the most recent completed payment decides — ShamCash is `shamcash`, a
    payment recorded by hand is `operator_grant`;
  * active with no completed payment at all was switched on by an operator:
    `operator_grant`.
A row that never entitled anyone (a checkout opened and never paid) stays blank.
"""

from django.db import migrations, models


def source_history(apps, schema_editor):
    Subscription = apps.get_model("subscription", "Subscription")
    Payment = apps.get_model("subscription", "Payment")

    Subscription.objects.filter(status="trial").update(source="trial")
    for sub in Subscription.objects.exclude(status="trial").iterator():
        method = (Payment.objects.filter(subscription=sub, status="completed")
                  .order_by("-updated_at", "-id").values_list("payment_method", flat=True).first())
        if method is not None:
            source = "shamcash" if method == "shamcash" else "operator_grant"
        elif sub.status == "active":
            source = "operator_grant"
        else:
            continue
        Subscription.objects.filter(pk=sub.pk).update(source=source)


class Migration(migrations.Migration):

    dependencies = [
        ("subscription", "0011_payment_cover_window_and_payout_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="money_type",
            field=models.CharField(
                choices=[
                    ("PLATFORM_SUB", "Platform subscription"),
                    ("COACHING_1TO1", "One-to-one coaching fee"),
                    ("ROUTINE_PRODUCT", "Routine product"),
                    ("WALLET_MOVE", "Wallet top-up or transfer"),
                ],
                db_index=True,
                default="PLATFORM_SUB",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="subscription",
            name="source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("shamcash", "ShamCash"),
                    ("agent_wallet", "Agent wallet"),
                    ("operator_grant", "Operator grant"),
                    ("trial", "Trial"),
                    ("apple_iap", "Apple in-app purchase (reserved)"),
                    ("google_play", "Google Play billing (reserved)"),
                ],
                db_index=True,
                default="",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "money_type__in",
                        [
                            "PLATFORM_SUB",
                            "COACHING_1TO1",
                            "ROUTINE_PRODUCT",
                            "WALLET_MOVE",
                        ],
                    )
                ),
                name="payment_money_type_known",
            ),
        ),
        # Last, so no ALTER follows the row updates in this transaction.
        migrations.RunPython(source_history, migrations.RunPython.noop),
    ]
