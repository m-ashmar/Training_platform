"""A payment pays for a plan OR for a routine product bought through a checkout (the web
payment hub, the payments spec section 5: `api/v1/checkout/sessions/`).

Additive for every existing row: each has a subscription and neither new column, which
is exactly the first arm of `payment_pays_for_one_thing`. A product's checkout has no
subscription (a buyer need not have one), so `subscription` becomes nullable and the
constraint says when it may be empty. `uniq_open_product_checkout` backs "one open
session per user per item". Two review reasons are added for money that arrived for a
program that cannot be delivered (`routine.products.checkout_refusal`).
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("routine", "0032_exercise_catalogue_key"),
        ("subscription", "0016_every_payment_transition_is_an_immutable_event"),
        ("wallet", "0021_the_ledger_is_append_only_at_the_database"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="buyer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="product_payments",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="routine_product",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="checkouts",
                to="routine.routineproduct",
            ),
        ),
        migrations.AlterField(
            model_name="payment",
            name="subscription",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="subscription.subscription",
            ),
        ),
        migrations.AlterField(
            model_name="transferreview",
            name="reason",
            field=models.CharField(
                choices=[
                    ("amount_differs", "The reference, another amount"),
                    ("currency_differs", "The reference, another currency"),
                    (
                        "reference_missing",
                        "The amount of one open checkout, no reference",
                    ),
                    (
                        "several_candidates",
                        "The amount of several open checkouts, no reference",
                    ),
                    ("no_candidate", "No open checkout matches"),
                    ("checkout_closed", "The reference of a checkout no longer open"),
                    ("duplicate_transfer", "The reference of a checkout already paid"),
                    ("already_owned", "A program the buyer already owns"),
                    (
                        "product_unavailable",
                        "A program that can no longer be delivered",
                    ),
                ],
                max_length=30,
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("buyer__isnull", True),
                        ("routine_product__isnull", True),
                        ("subscription__isnull", False),
                    ),
                    models.Q(
                        ("buyer__isnull", False),
                        ("money_type", "ROUTINE_PRODUCT"),
                        ("routine_product__isnull", False),
                        ("subscription__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="payment_pays_for_one_thing",
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("routine_product__isnull", False),
                    (
                        "status__in",
                        ("pending", "processing", "authorized", "needs_review"),
                    ),
                ),
                fields=("buyer", "routine_product"),
                name="uniq_open_product_checkout",
            ),
        ),
    ]
