"""Road to 10 S2.13 (the owner's D19): a plan paid from the wallet — the `wallet` method
and the app's idempotency key for it."""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("routine", "0035_routinetemplate_goal_closed_list"),
        ("subscription", "0020_a_saved_payer_address_is_proven"),
        ("wallet", "0022_coaching_transfer"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="idempotency_key",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="payment",
            name="payment_method",
            field=models.CharField(
                choices=[
                    ("shamcash", "ShamCash"),
                    ("manual", "Manual"),
                    ("wallet", "Wallet"),
                    ("syriatel_cash", "Syriatel Cash"),
                    ("baraka_bank", "Al-Baraka Bank"),
                    ("bemo_bank", "BEMO Bank"),
                    ("stripe", "Stripe"),
                    ("paypal", "PayPal"),
                    ("apple_pay", "Apple Pay"),
                    ("google_pay", "Google Pay"),
                ],
                db_index=True,
                default="shamcash",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="payment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("idempotency_key__isnull", False)),
                fields=("subscription", "idempotency_key"),
                name="uniq_payment_wallet_idempotency_key",
            ),
        ),
    ]
