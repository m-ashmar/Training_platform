"""Money arrived in a person's wallet (BACKEND_BUGS B-115).

An agent took cash from a client and credited their wallet (`wallet/views.py`, both
top-up endpoints). The design promises the client hears of it at once: AgentReceipt
«وصل رصيده فوراً، ووصله إشعار», and the agent README "the client gets a push at the same
moment".

A top-up is WALLET_MOVE — money handed to a person in a shop, not a digital good — so
the NOTICE reaches the client whatever their build (the spec, section 3: "top-up is
done with an agent in person, so there is nothing to hide"). Whether the top-up can be
made is the AGENT's build, not the client's: the agent's app is refused where
`can_transfer_wallet` is off, the first `ios` build by the spec's table (BACKEND_BUGS
B-199). The text still states the fact only and names no amount,
like every other notice in this package: the amount, the currency and the reference ride
in `data`, for the app to show beside its own balance.
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class WalletToppedUpEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Your balance was topped up"),
        body=_("A cash top-up from %(agent)s was added to your balance."),
    )
    recipient_id: int = 0
    reference_id: str = ""
    amount: str = ""
    currency: str = ""
    agent: str = ""
