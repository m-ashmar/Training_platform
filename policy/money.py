"""The four kinds of money. Every payment, wallet movement and checkout carries one.

The type is what a store rule attaches to: a plan or a routine sold to many buyers is a
digital good (Apple and Google both require their own billing for it), a trainer's fee
for coaching one client is a person-to-person service, and a top-up or a transfer is
real money moving between people. Code that has to decide what a build may sell reads
this value and nothing else — never the endpoint it arrived on, never a description.

Deliberately free of Django model imports: `wallet` and `subscription` both import it
from their models.
"""
from django.db import models


class MoneyType(models.TextChoices):
    PLATFORM_SUB = "PLATFORM_SUB", "Platform subscription"
    COACHING_1TO1 = "COACHING_1TO1", "One-to-one coaching fee"
    ROUTINE_PRODUCT = "ROUTINE_PRODUCT", "Routine product"
    WALLET_MOVE = "WALLET_MOVE", "Wallet top-up or transfer"


#: Digital goods. Both stores require their own billing for these, which this platform
#: cannot offer, so a store build never sells them — see `policy.services`.
STORE_BILLED_TYPES = frozenset({MoneyType.PLATFORM_SUB, MoneyType.ROUTINE_PRODUCT})


class EntitlementSource(models.TextChoices):
    """Where the money behind an entitlement came from.

    Access never depends on it: what was bought anywhere works everywhere. It exists so
    that a new way of paying is one new verification adapter, not a redesign — which is
    why `apple_iap` and `google_play` are reserved now, before anything writes them.
    """
    SHAMCASH = "shamcash", "ShamCash"
    AGENT_WALLET = "agent_wallet", "Agent wallet"
    OPERATOR_GRANT = "operator_grant", "Operator grant"
    TRIAL = "trial", "Trial"
    APPLE_IAP = "apple_iap", "Apple in-app purchase (reserved)"
    GOOGLE_PLAY = "google_play", "Google Play billing (reserved)"


#: `Payment.payment_method` -> the source an entitlement bought with it carries. A method
#: not listed was recorded by hand, which is what an operator grant is.
SOURCE_OF_PAYMENT_METHOD = {
    "shamcash": EntitlementSource.SHAMCASH,
    "manual": EntitlementSource.OPERATOR_GRANT,
    # A plan paid from the wallet (S2.13): money a person put in their wallet, through an
    # agent's cash top-up today.
    "wallet": EntitlementSource.AGENT_WALLET,
}


def source_of_payment_method(method: str) -> str:
    return SOURCE_OF_PAYMENT_METHOD.get(method, EntitlementSource.OPERATOR_GRANT)
