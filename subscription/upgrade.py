"""What moving to a dearer plan in the middle of a period costs (the owner, 2026-09-23).

The owner's rule, as decided:

* An UPGRADE is allowed while a plan runs. A DOWNGRADE is not: it waits until the
  period ends, and then `change-plan` moves the row with nothing to pay.
* What is left of the current plan is credited by DAYS, not by features used:
  the price paid × the days left ÷ the length of the period that price bought.
* The new plan's price minus that credit is what is left to pay, and it is shown
  BEFORE paying. When that payment clears, the new plan starts and runs its full length.

Nothing here writes anything. `quote_upgrade` is a pure reading of the subscription
and its payments; `PaymentService.start_upgrade` opens the checkout from it under the
subscription lock, and `PaymentService.complete_payment` switches the plan when the
money clears. Every amount is in the BUYER's currency (`wallet.currency_for`), the one
currency this person trades in, and every conversion goes through `pricing.charge_for`
— the sanctioned door to the operator's rate — so a missing or stale rate refuses with
its own `code` instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from .pricing import Priced, charge_for

CENT = Decimal('0.01')


class UpgradeRefused(Exception):
    """Base for the reasons an upgrade cannot be quoted. Each carries its `code`."""
    code = 'upgrade_refused'


class NoRunningCover(UpgradeRefused):
    """Nothing is running, so there is nothing to credit: `change-plan` moves the row
    and the Pay button charges the new plan in full."""
    code = 'no_running_cover'


class NotAnUpgrade(UpgradeRefused):
    """The plan does not cost this buyer more than the one running. A downgrade (or a
    plan at the same price) waits until the period ends (the owner, 2026-09-23)."""
    code = 'not_an_upgrade'


class UpgradeNotPayable(UpgradeRefused):
    """What is left to pay is nothing, or less than the gateway can take. The credit
    covers the new plan: the owner declined both extra days and wallet credit for the
    remainder, so the switch waits until less of the period is left."""
    code = 'upgrade_not_payable'

    def __init__(self, message, *, to_pay, minimum=None):
        super().__init__(message)
        self.to_pay = to_pay
        self.minimum = minimum


@dataclass(frozen=True)
class CreditPart:
    """What one running payment of the current run contributes to a credit."""

    payment_id: object
    #: What that payment's period was worth, in its own currency: its amount, plus the
    #: credit an upgrade switched into it (`value_of`).
    value: Decimal
    currency: str
    #: The whole days left that fall in this payment's window, and the window's own
    #: length, both in seconds (whole days).
    seconds: int
    window_seconds: int
    #: value × seconds ÷ window, converted into the buyer's currency.
    credit: Decimal


def value_of(payment) -> Decimal:
    """What a completed payment's period was worth, in its own currency.

    Its amount — except an upgrade that switched the plan, whose period was bought by
    its amount AND the credit that came off it (`metadata['upgrade']['switch']`). A
    second upgrade credits the first one's days at the plan price they were worth, not
    at the cash it happened to charge (the review of 2026-09-23, U3).
    """
    switch = ((payment.metadata or {}).get('upgrade') or {}).get('switch') or {}
    extra = Decimal(switch.get('credit_applied') or '0')
    return Decimal(payment.amount) + extra


def credit_for_run(subscription, *, buyer_currency, now=None):
    """The credit for what is left of `subscription`'s current run, in `buyer_currency`.

    Returns `(credit, days_left, parts)`. WHOLE days left (the day under way is used):
    by the second, what is left to pay moved every second or so and a customer who paid
    the figure shown was refused. Those whole days are shared out over the run's running
    payments in date order — each window takes what falls in it — and each payment is
    credited at ITS own worth and currency: its value × its share ÷ its window. So
    stacked renewals bought at different prices, or in a currency the buyer has since
    left, are each credited at what they cost (the review of 2026-09-23, U8). Days no
    payment covers — a trial, an operator's extension — are credited at nothing.

    Payments whose value an upgrade already consumed are not the run's any more
    (`Subscription.current_run_payments`), so nothing is credited twice.
    """
    now = now or timezone.now()
    seconds_left = max(0, int((subscription.effective_end - now).total_seconds()))
    days_left = seconds_left // 86400
    budget = days_left

    running = list(subscription.current_run_payments()
                   .filter(covers_until__gt=now)
                   .order_by('covers_from', 'id'))
    parts = []
    total = Decimal('0.00')
    for payment in running:
        if budget <= 0:
            break
        if not payment.covers_from or not payment.covers_until:
            continue
        # In WHOLE days, each rounded: a window is a whole number of days opened a few
        # seconds after the one before it, and counting seconds lost a cent here and
        # there between stacked renewals.
        window = round((payment.covers_until - payment.covers_from).total_seconds() / 86400)
        if window <= 0:
            continue
        start = max(payment.covers_from, now)
        capacity = max(0, round((payment.covers_until - start).total_seconds() / 86400))
        share = min(budget, capacity)
        if share <= 0:
            continue
        budget -= share
        value = value_of(payment)
        native = (value * Decimal(share) / Decimal(window)).quantize(CENT,
                                                                     rounding=ROUND_HALF_UP)
        credit = (native if payment.currency == buyer_currency
                  else charge_for(native, payment.currency, buyer_currency).amount)
        parts.append(CreditPart(payment.pk, value, payment.currency, share * 86400,
                                window * 86400, credit))
        total += credit
    return total.quantize(CENT, rounding=ROUND_HALF_UP), Decimal(days_left), parts


@dataclass(frozen=True)
class UpgradeQuote:
    """One upgrade, priced at one moment, for one buyer."""

    from_plan: object
    to_plan: object
    #: The buyer's currency: every amount below is in it.
    currency: str
    #: The new plan's price for this buyer (its price of record, converted if need be).
    new_price: Decimal
    #: What is left of the running plan, credited (`credit_for_run`).
    credit: Decimal
    #: `new_price - credit`: what the checkout charges.
    to_pay: Decimal
    #: WHOLE days of cover left when quoted (the day under way counts as used).
    days_left: Decimal
    #: The new plan's pricing, for the payment's price-of-record and rate snapshot.
    priced: Priced
    quoted_at: datetime

    def as_payload(self) -> dict:
        """The shape the app draws before the customer pays."""
        return {
            'from_plan': {'id': self.from_plan.id, 'name': self.from_plan.name},
            'to_plan': {'id': self.to_plan.id, 'name': self.to_plan.name,
                        'duration_days': self.to_plan.duration_days},
            'currency': self.currency,
            'new_price': str(self.new_price),
            'credit': str(self.credit),
            'to_pay': str(self.to_pay),
            'days_left': int(self.days_left),
            'starts': 'when_paid',
        }

    def as_record(self) -> dict:
        """What the checkout keeps on its payment (`metadata['upgrade']`), in plain JSON:
        what the customer was shown. What the switch actually credits is decided when
        the money clears and written beside it (`PaymentService._apply_upgrade`)."""
        from .models import plan_snapshot

        return {
            'from_plan': plan_snapshot(self.from_plan),
            'currency': self.currency,
            'new_price': str(self.new_price),
            'credit': str(self.credit),
            'to_pay': str(self.to_pay),
            'days_left': int(self.days_left),
            'quoted_at': self.quoted_at.isoformat(),
        }


def is_dearer(to_plan, from_plan, buyer_currency) -> bool:
    """Whether `to_plan` costs this buyer more than `from_plan`, both priced now in the
    buyer's currency (raises `ExchangeRateUnavailable` when a price has to cross)."""
    new = charge_for(to_plan.price, to_plan.currency, buyer_currency)
    current = charge_for(from_plan.price, from_plan.currency, buyer_currency)
    return new.amount > current.amount


def quote_upgrade(subscription, to_plan, *, buyer_currency, gateway_name=None, now=None):
    """Price moving `subscription` onto `to_plan` now. Raises an `UpgradeRefused`, or
    `wallet.exchange.ExchangeRateUnavailable` when a price has to cross currencies and
    the operator's rate is missing or stale. The credit is `credit_for_run`.
    """
    now = now or timezone.now()
    if not subscription.is_active:
        raise NoRunningCover(f"Subscription {subscription.id} has no running cover")
    from_plan = subscription.plan
    if to_plan.pk == from_plan.pk or not is_dearer(to_plan, from_plan, buyer_currency):
        raise NotAnUpgrade(f"Plan {to_plan.pk} is not dearer than plan {from_plan.pk} "
                           f"for a {buyer_currency} buyer")

    new = charge_for(to_plan.price, to_plan.currency, buyer_currency)
    credit, days_left, _parts = credit_for_run(subscription, buyer_currency=buyer_currency,
                                               now=now)
    to_pay = (new.amount - credit).quantize(CENT, rounding=ROUND_HALF_UP)
    minimum = None
    if gateway_name:
        from .settings.gateway_config import get_amount_limits

        limits = get_amount_limits(gateway_name, buyer_currency) or {}
        minimum = limits.get('min')
    if to_pay <= 0 or (minimum is not None and to_pay < minimum):
        raise UpgradeNotPayable(
            f"Upgrade of {subscription.id} to plan {to_plan.pk} leaves {to_pay} "
            f"{buyer_currency} to pay (minimum {minimum})", to_pay=to_pay, minimum=minimum)

    return UpgradeQuote(
        from_plan=from_plan, to_plan=to_plan, currency=buyer_currency,
        new_price=new.amount, credit=credit, to_pay=to_pay, days_left=days_left,
        priced=new, quoted_at=now)
