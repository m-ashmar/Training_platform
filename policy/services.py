"""Reading the policy, and refusing what a build may not do.

The flags are not cosmetic: every endpoint that opens a checkout or moves money asks
here first, so a purchase screen that reaches a store build by mistake still cannot buy
anything. Two layers, on purpose:

1. `STORE_BILLED_TYPES` on a store channel is refused IN CODE, before any row is read.
   No database edit, flag or version can change that answer.
2. Everything else follows the row for the request's channel and version, and
   `policy.defaults` where there is no row.

A request that declares no channel gets `STRICTEST` — the server never guesses which
build is calling.
"""
from __future__ import annotations

from .channels import STORE_CHANNELS, channel_of, version_of
from .exceptions import AppChannelRequired, NotAvailableOnChannel
from .money import STORE_BILLED_TYPES, MoneyType

#: What a caller that declares no known channel may do: look, never buy.
STRICTEST = {
    "can_purchase_platform_sub": False,
    "can_purchase_routine_product": False,
    "can_pay_coaching_in_app": False,
    "can_show_prices": False,
    "can_show_coaching_prices": False,
    "can_show_web_hub_link": False,
    "can_view_entitlements": True,
    "can_view_wallet": True,
    "can_transfer_wallet": False,
}

#: The flag that lets a channel open a checkout for each kind of money.
PURCHASE_FLAG = {
    MoneyType.PLATFORM_SUB: "can_purchase_platform_sub",
    MoneyType.ROUTINE_PRODUCT: "can_purchase_routine_product",
    MoneyType.COACHING_1TO1: "can_pay_coaching_in_app",
    MoneyType.WALLET_MOVE: "can_transfer_wallet",
}


def flags_for(channel: str | None, version: tuple | None = None) -> dict:
    """The flags for a build: the row with the highest `min_version` it has reached.

    Takes a channel and a version and NOTHING else — no user, no request. That signature
    is the guarantee that two users of one build cannot be shown different behaviour.
    A build that sends no version gets the channel's base row, and a channel with no
    row the build has reached gets `policy.defaults` — the rows override the spec's table,
    they are not what makes the platform work.
    """
    from .defaults import DEFAULT_FLAGS
    from .models import STORE_LOCKED_FLAGS, ChannelCapabilities

    if channel is None:
        return dict(STRICTEST)
    version = version or (0, 0, 0)
    reached = [row for row in ChannelCapabilities.objects.filter(channel=channel)
               if row.version_tuple <= version]
    if reached:
        flags = max(reached, key=lambda row: row.version_tuple).flags()
    else:
        flags = dict(DEFAULT_FLAGS[channel])
    if channel in STORE_CHANNELS:
        # Masked on read as well as refused on save, so a row written around the model
        # (SQL, `update()`, a bad fixture) still cannot switch these on.
        for name in STORE_LOCKED_FLAGS:
            flags[name] = False
    return flags


def flags_for_request(request) -> dict:
    return flags_for(channel_of(request), version_of(request))


def require_purchase_allowed(request, money_type: str) -> None:
    """Raise unless this build may open a checkout, or move money, of this kind.

    Call it first in every endpoint that does either. Raises `AppChannelRequired` when
    the request declares no channel and `NotAvailableOnChannel` when its build may not.
    """
    channel = channel_of(request)
    if channel is None:
        raise AppChannelRequired()
    if channel in STORE_CHANNELS and money_type in STORE_BILLED_TYPES:
        raise NotAvailableOnChannel()
    if not flags_for(channel, version_of(request))[PURCHASE_FLAG[MoneyType(money_type)]]:
        raise NotAvailableOnChannel()


def can_show_price(request, money_type: str) -> bool:
    """Whether a response to this build may carry the price of this kind of thing."""
    flags = flags_for_request(request)
    if money_type == MoneyType.COACHING_1TO1:
        return flags["can_show_coaching_prices"]
    return flags["can_show_prices"]
