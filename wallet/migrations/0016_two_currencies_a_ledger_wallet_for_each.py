"""Two currencies, both sold, and one platform ledger wallet for each.

The owner (2026-09-21): Yalla Gym trades in Syrian pounds AND US dollars, both first
class, with an operator-set rate between them. Three things follow, and this is all
three:

1. **`ExchangeRate`** — the append-only record of what an operator set the USD/SYP rate
   to, and when. A new rate is a new row; nothing edits or deletes one, because every
   purchase struck at a rate depends on it still saying what it said.

2. **The rate, snapshotted onto the ledger** — `Transaction.exchange_rate` (PROTECT),
   its value, and the reference that ties the two legs of one exchange together. NULL
   on every ordinary row, which crosses nothing.

3. **`Transaction.currency` stops defaulting to the literal "USD"** and takes the
   platform's base currency from settings, like `Wallet.currency` beside it. A literal
   here is the exact hardcoding whose twin on `Wallet.currency` minted 5,000 SYP into a
   wallet labelled USD (SIDE_BUGS, money build).

And the data move: **the platform's ledger owners are now addressed by currency.**
`get_platform_revenue_wallet()` used to return ONE wallet, so the first payment ever
taken decided the platform's currency for ever and a payment in the other one was
minted into the customer's wallet and left there (SIDE_BUGS S-001). There is now one
disabled owner row per ledger account per currency —
`platform_revenue_syp@local`, `platform_revenue_usd@local` and the escrow pair.

THE EXISTING WALLET BECOMES ITS OWN CURRENCY'S, and nothing else happens to it. The
wallet ROW is not touched: same primary key, same balance, same `Transaction` foreign
keys pointing at it. Only its OWNER's `email` and `username` are rewritten, from
`platform_escrow@local` to `platform_escrow_<ccy>@local`, where `<ccy>` is read off the
wallet's own `currency` column — never guessed from a setting, because a deployment
that ran with `PLATFORM_CURRENCY=USD` holds dollars in it. Money stays exactly where it
is and stays attributable.

Reversible. The reverse renames the owner holding `settings.PLATFORM_CURRENCY` back to
the bare address and leaves any other-currency ledger owner alone: the old code cannot
address it, and moving its balance anywhere would be inventing a conversion nobody
asked for. That is a stranded wallet, not a lost one, and rolling forward again adopts
it.
"""
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from decimal import Decimal

from django.conf import settings
from django.db import migrations, models

import wallet.models


def _bases():
    from django.conf import settings as s

    return [(getattr(s, "PLATFORM_ESCROW_EMAIL", "platform_escrow@local"),
             getattr(s, "PLATFORM_ESCROW_USERNAME", "platform_escrow")),
            (getattr(s, "PLATFORM_REVENUE_EMAIL", "platform_revenue@local"),
             getattr(s, "PLATFORM_REVENUE_USERNAME", "platform_revenue"))]


def _address(base_email, base_username, currency):
    local, at, domain = base_email.partition("@")
    suffix = currency.lower()
    email = f"{local}_{suffix}{at}{domain}" if at else f"{base_email}_{suffix}"
    return email, f"{base_username}_{suffix}"


def name_the_ledger_owners_by_currency(apps, schema_editor):
    CustomUser = apps.get_model("users", "CustomUser")
    Wallet = apps.get_model("wallet", "Wallet")
    from django.conf import settings as s

    for base_email, base_username in _bases():
        owner = CustomUser.objects.filter(email=base_email).first()
        if owner is None:
            continue    # a fresh database: the pair is created on first use
        wallet = Wallet.objects.filter(owner_id=owner.pk).first()
        # The wallet's OWN currency, so a balance is never relabelled. With no wallet
        # yet there is no money to misname and the base currency is what the next one
        # would have taken anyway.
        currency = (wallet.currency if wallet else
                    getattr(s, "PLATFORM_CURRENCY", "SYP"))
        email, username = _address(base_email, base_username, currency)
        if CustomUser.objects.filter(email=email).exclude(pk=owner.pk).exists():
            continue    # already migrated, or an operator made it by hand
        if CustomUser.objects.filter(username=username).exclude(pk=owner.pk).exists():
            # A PERSON registered this username before it was reserved (SIDE_BUGS
            # S-086). The address is what finds a ledger owner; the username is only a
            # label, so the owner keeps its old one rather than aborting the deploy.
            CustomUser.objects.filter(pk=owner.pk).update(email=email)
            continue
        CustomUser.objects.filter(pk=owner.pk).update(email=email, username=username)


def name_the_ledger_owners_without_a_currency(apps, schema_editor):
    """Put back the bare address the old code knows how to find.

    Symmetric with the forward: whatever currency the one ledger owner holds, it is the
    one that goes back to `platform_escrow@local`. If a SECOND currency's owner has
    since appeared — money the old code cannot address at all — the one matching
    `settings.PLATFORM_CURRENCY` is restored and the other is left exactly where it is,
    with its balance. That is a wallet the old code cannot see, not a wallet that lost
    its money; rolling forward again adopts it.
    """
    CustomUser = apps.get_model("users", "CustomUser")
    from django.conf import settings as s

    from wallet.currencies import CURRENCY_CODES

    base_currency = getattr(s, "PLATFORM_CURRENCY", "SYP")
    for base_email, base_username in _bases():
        if CustomUser.objects.filter(email=base_email).exists():
            continue    # already reversed, or never migrated
        found = {}
        for code in CURRENCY_CODES:
            email, _username = _address(base_email, base_username, code)
            owner = CustomUser.objects.filter(email=email).first()
            if owner is not None:
                found[code] = owner
        if not found:
            continue
        code = base_currency if base_currency in found else next(iter(found))
        CustomUser.objects.filter(pk=found[code].pk).update(email=base_email,
                                                            username=base_username)


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0015_the_agent_listing_on_the_topup_screen"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ExchangeRate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("base", models.CharField(choices=[("SYP", "Syrian Pound"),
                                                   ("USD", "US Dollar")], max_length=3)),
                ("quote", models.CharField(choices=[("SYP", "Syrian Pound"),
                                                    ("USD", "US Dollar")], max_length=3)),
                ("rate", models.DecimalField(
                    decimal_places=6, max_digits=18,
                    validators=[django.core.validators.MinValueValidator(
                        Decimal("0.000001"))])),
                ("effective_from", models.DateTimeField(
                    db_index=True, default=django.utils.timezone.now)),
                ("note", models.CharField(
                    blank=True, default="", max_length=200,
                    help_text="Why this rate, in the operator's words.")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("set_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="exchange_rates", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Exchange rate",
                "verbose_name_plural": "Exchange rates",
                "ordering": ["-effective_from", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="exchangerate",
            index=models.Index(fields=["base", "quote", "-effective_from", "-id"],
                               name="fx_pair_latest_idx"),
        ),
        migrations.AddConstraint(
            model_name="exchangerate",
            constraint=models.CheckConstraint(condition=models.Q(("rate__gt", 0)),
                                              name="fx_rate_positive"),
        ),
        migrations.AddConstraint(
            model_name="exchangerate",
            constraint=models.CheckConstraint(
                condition=models.Q(("base", models.F("quote")), _negated=True),
                name="fx_pair_is_two_currencies"),
        ),
        migrations.AddField(
            model_name="wallet",
            name="currency_chosen_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="transaction",
            name="currency",
            field=models.CharField(default=wallet.models._platform_currency,
                                   max_length=8),
        ),
        migrations.AddField(
            model_name="transaction",
            name="exchange_rate",
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.PROTECT,
                                    related_name="legs", to="wallet.exchangerate"),
        ),
        migrations.AddField(
            model_name="transaction",
            name="exchange_rate_value",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=18,
                                      null=True),
        ),
        migrations.AddField(
            model_name="transaction",
            name="exchange_reference",
            field=models.CharField(blank=True, db_index=True, default="", max_length=36),
        ),
        migrations.RunPython(name_the_ledger_owners_by_currency,
                             name_the_ledger_owners_without_a_currency),
    ]
