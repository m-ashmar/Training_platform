"""THE conversion boundary: the one place a number changes currency.

The owner (2026-09-21) linked the dollar to the pound and put the rate in the operator's
hands: "let's link usd to syp, like 1 usd = x sp, and we link this to admin dashboard so
admin set the rate". A rate that a person maintains is a rate that can be forgotten, so
everything about converting has to be in one function that can refuse.

WHAT MAY CALL `convert_at_the_operators_rate`, and nothing else may:

| Caller | Why |
|---|---|
| `subscription/pricing.py` | the purchase: a price of record in one currency charged to a buyer who holds the other, and the same price shown to that reader |
| `wallet/models.py exchange_funds_atomic` | the only movement of money that crosses currencies, in two legs |

`tests/test_two_currencies.py` scans the source and fails a third caller. This is not
bureaucracy: a conversion scattered across views is how a rate gets applied twice, or
applied and then not snapshotted, and neither shows up as an error — only as money.

THE RULES

* **Direction.** Rates are stored for one pair in one direction, `1 USD = rate SYP`
  (`wallet.currencies.RATE_BASE/RATE_QUOTE`). USD -> SYP multiplies, SYP -> USD divides
  by the same row. One number to maintain, one place a sign can be wrong.
* **Rounding.** ONE rule everywhere: quantize to two decimal places, `ROUND_HALF_UP` —
  the same `CENT`/`ROUND_HALF_UP` `users.coaching.split` uses for the commission, so the
  two never disagree about a half-unit. Two places because that is what every money
  column in the schema stores (`decimal_places=2`); rounding anywhere else would write a
  number the database would round again, differently.
* **Snapshot.** Every caller gets the `ExchangeRate` ROW back with the number and must
  store it on whatever it priced. A conversion whose rate is not written down cannot be
  refunded honestly and cannot be explained on a receipt.
* **Staleness refuses.** `EXCHANGE_RATE_MAX_AGE_DAYS` (default 7) past `effective_from`
  and a conversion raises instead of guessing: the pound moves, and selling at last
  week's rate is a loss nobody chose. A SAME-currency purchase never reaches this
  module, so a stale rate can never stop a pound-priced plan being sold to a pound
  wallet. That distinction is real in the code: `convert_at_the_operators_rate` returns
  immediately when the two currencies are equal, before any rate is looked up.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .currencies import RATE_BASE, RATE_QUOTE, is_supported

#: The precision of every money column in the schema, and so of every conversion.
CENT = Decimal('0.01')

#: How old the operator's rate may be before a conversion refuses. Seven days is a
#: guess that wants the owner's confirmation: long enough that a quiet week does not
#: stop sales, short enough that nobody sells a month of dollars at a fortnight-old
#: pound. `settings.EXCHANGE_RATE_MAX_AGE_DAYS` overrides it.
DEFAULT_MAX_AGE_DAYS = 7


class ExchangeRateUnavailable(Exception):
    """A conversion could not be priced. Carries a `code` the app branches on."""

    code = "exchange_rate_unavailable"

    def __init__(self, message, *, age_days=None):
        super().__init__(message)
        self.message = message
        self.age_days = age_days


class NoExchangeRate(ExchangeRateUnavailable):
    """Nobody has ever set a rate for this pair, so there is nothing to convert at."""

    code = "exchange_rate_missing"


class StaleExchangeRate(ExchangeRateUnavailable):
    """The newest rate is older than the platform is willing to sell at."""

    code = "exchange_rate_stale"


def max_age_days() -> int:
    return int(getattr(settings, "EXCHANGE_RATE_MAX_AGE_DAYS", DEFAULT_MAX_AGE_DAYS))


def current_rate(at=None):
    """The `ExchangeRate` row in force for the platform's one pair, or None.

    One pair (USD/SYP), so no arguments: asking for a pair that does not exist is a
    programming error, not a runtime answer.
    """
    from .models import ExchangeRate

    return ExchangeRate.current(RATE_BASE, RATE_QUOTE, at=at)


def rate_age_days(row, at=None) -> float | None:
    """How many days ago `row` took effect, or None when there is no row."""
    if row is None:
        return None
    return max(0.0, ((at or timezone.now()) - row.effective_from).total_seconds() / 86400)


def is_stale(row, at=None) -> bool:
    """True when there is no rate, or the one there is has gone past its window."""
    age = rate_age_days(row, at=at)
    return age is None or age > max_age_days()


def rate_health(at=None) -> dict:
    """What the operator console shows: the rate, how old it is, and whether it still
    sells. Read-only, and it never raises — a console that cannot render because the
    thing it exists to fix is broken is worse than useless.
    """
    row = current_rate(at=at)
    age = rate_age_days(row, at=at)
    return {
        "rate": row,
        "pair": f"1 {RATE_BASE} = ? {RATE_QUOTE}",
        "value": row.rate if row else None,
        "effective_from": row.effective_from if row else None,
        "set_by": getattr(row, "set_by", None),
        "age_days": None if age is None else round(age, 2),
        "max_age_days": max_age_days(),
        "is_stale": is_stale(row, at=at),
    }


#: `convert_at_the_operators_rate(rate=...)` not given: it reads the rate itself.
READ_THE_RATE = object()


def convert_at_the_operators_rate(amount, from_currency: str, to_currency: str, *,
                                  at=None, rate=READ_THE_RATE):
    """`(converted_amount, rate_row)` for `amount`, at the rate an operator set.

    THE ONLY conversion in the codebase. See this module's docstring for who may call
    it and for the rounding and staleness rules.

    `rate`: the `current_rate(at)` row (or None) the caller has ALREADY read for this
    same moment, so pricing a list of N things reads the rate once rather than N times
    (SIDE_BUGS S-130). Every rule below still applies to it — missing and stale still
    refuse. Omitted, the rate is read here.

    Returns `(Decimal, None)` unchanged when the two currencies are the same: a
    same-currency purchase is not a conversion, consults no rate, and is therefore
    never refused because a rate is old.

    Raises `NoExchangeRate` or `StaleExchangeRate` — both `ExchangeRateUnavailable`,
    both carrying a `code` — rather than falling back to any number at all.
    """
    amount = Decimal(amount)
    if from_currency == to_currency:
        return amount.quantize(CENT, rounding=ROUND_HALF_UP), None
    for code in (from_currency, to_currency):
        if not is_supported(code):
            raise NoExchangeRate(f"{code!r} is not a currency this platform trades in")

    row = current_rate(at=at) if rate is READ_THE_RATE else rate
    if row is None:
        raise NoExchangeRate(
            f"no operator has set a {RATE_BASE}/{RATE_QUOTE} rate, so "
            f"{from_currency} cannot be priced in {to_currency}")
    age = rate_age_days(row, at=at)
    if age > max_age_days():
        raise StaleExchangeRate(
            f"the {RATE_BASE}/{RATE_QUOTE} rate is {age:.1f} days old "
            f"(limit {max_age_days()}); set a new one before selling across currencies",
            age_days=age)

    if (from_currency, to_currency) == (RATE_BASE, RATE_QUOTE):
        converted = amount * row.rate
    elif (from_currency, to_currency) == (RATE_QUOTE, RATE_BASE):
        converted = amount / row.rate
    else:  # pragma: no cover — unreachable while the platform trades one pair
        raise NoExchangeRate(f"no rate links {from_currency} to {to_currency}")
    return converted.quantize(CENT, rounding=ROUND_HALF_UP), row


#: What a refusal says to a person, once, in one place. The app branches on the `code`
#: beside it and never on this text.
REFUSAL_MESSAGES = {
    NoExchangeRate.code: _("Paying in this currency is not available right now."),
    StaleExchangeRate.code: _("Paying in this currency is not available right now."),
}


def refusal_body(exc: ExchangeRateUnavailable) -> dict:
    """The response body for a conversion that was refused: `{error, code}`."""
    return {"error": REFUSAL_MESSAGES.get(exc.code, REFUSAL_MESSAGES[NoExchangeRate.code]),
            "code": exc.code}
