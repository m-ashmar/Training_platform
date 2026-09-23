"""What a price COSTS this buyer, in the one currency this buyer trades in.

The owner (2026-09-21): a person chooses SYP or USD and is held to it, and every
service can be bought in either. Those two together mean the catalogue is not
duplicated per currency — a plan keeps the price of record the operator set, and the
CHARGE is converted into the buyer's currency at purchase, at the operator's live rate,
with that rate written onto the payment.

So there are exactly two questions, and this module is both of them:

    what will this person be charged   -> `charge_for`   (the purchase)
    what should this person be shown   -> `price_for`    (the price list)

Both go through `wallet.exchange.convert_at_the_operators_rate`, which is the only
conversion in the codebase, refuses on a missing or stale rate, and hands back the
`ExchangeRate` row to snapshot. A SAME-currency buyer never reaches it: `converted` is
False, `rate` is None, and no rate is consulted — a pound-priced plan keeps selling to a
pound wallet on a day nobody has touched the rate.

THE APP IS NEVER GIVEN TWO NUMBERS. `price_for` answers one amount and one currency,
already the reader's, plus `price_converted` and the rate so a receipt can say
honestly what happened. The client must not convert anything itself; if it ever has
both numbers it will eventually show the wrong one.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wallet.exchange import READ_THE_RATE, convert_at_the_operators_rate


@dataclass(frozen=True)
class Priced:
    """One price, in the reader's or buyer's currency, and how it got there."""

    amount: Decimal
    currency: str
    #: The operator's price of record, untouched.
    price_amount: Decimal
    price_currency: str
    #: The `ExchangeRate` row used, or None when nothing was converted. Callers store
    #: it (the FK and its number) on whatever they priced: a conversion whose rate is
    #: not written down cannot be refunded honestly or explained on a receipt.
    rate: object = None

    @property
    def converted(self) -> bool:
        return self.rate is not None

    def as_payload(self) -> dict:
        """The shape the app reads: one amount, one currency, and the truth about it."""
        return {
            "price": str(self.amount),
            "currency": self.currency,
            "price_converted": self.converted,
            "exchange_rate": (str(self.rate.rate) if self.rate is not None else None),
            "price_of_record": (str(self.price_amount) if self.converted else None),
            "price_of_record_currency": (self.price_currency if self.converted else None),
        }


def charge_for(price_amount, price_currency: str, buyer_currency: str, *, at=None,
               rate=READ_THE_RATE) -> Priced:
    """What to charge a buyer who trades in `buyer_currency` for a price of record.

    Raises `wallet.exchange.ExchangeRateUnavailable` (a `code` on it) when the price
    has to cross and the operator's rate is missing or stale. The caller turns that
    into a refusal the app can read — it must never fall back to the unconverted
    number, which would charge dollars to a pound wallet.

    `rate`: a `wallet.exchange.current_rate()` row already read for this request, so a
    list prices every row at ONE read of the rate (see `convert_at_the_operators_rate`).
    """
    amount, row = convert_at_the_operators_rate(
        price_amount, price_currency, buyer_currency, at=at, rate=rate)
    return Priced(amount, buyer_currency, Decimal(price_amount), price_currency, row)


def price_for(price_amount, price_currency: str, reader_currency: str, *, at=None,
              rate=READ_THE_RATE) -> Priced:
    """The same number for a price LIST, where a refusal must not blank the catalogue.

    A stale rate stops a sale; it must not stop a person browsing. When the price
    cannot be converted the reader is shown the price of record as it stands, marked
    unconverted — honest, and the purchase itself still refuses with its code when they
    tap Buy.
    """
    from wallet.exchange import ExchangeRateUnavailable

    try:
        return charge_for(price_amount, price_currency, reader_currency, at=at, rate=rate)
    except ExchangeRateUnavailable:
        return Priced(Decimal(price_amount), price_currency,
                      Decimal(price_amount), price_currency, None)
