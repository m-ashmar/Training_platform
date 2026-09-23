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
class UpgradeQuote:
    """One upgrade, priced at one moment, for one buyer."""

    from_plan: object
    to_plan: object
    #: The buyer's currency: every amount below is in it.
    currency: str
    #: The new plan's price for this buyer (its price of record, converted if need be).
    new_price: Decimal
    #: What is left of the running plan, credited: paid × days left ÷ period length.
    credit: Decimal
    #: `new_price - credit`: what the checkout charges.
    to_pay: Decimal
    #: WHOLE days of cover left when quoted (the day under way counts as used), and the
    #: period the credit is read against — the one the latest payment of this run bought.
    days_left: Decimal
    period_days: int
    #: What that payment was, in its own currency (the credit's source).
    paid_amount: Decimal | None
    paid_currency: str | None
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
            'period_days': self.period_days,
            'starts': 'when_paid',
        }

    def as_record(self) -> dict:
        """What the checkout keeps on its payment (`metadata['upgrade']`), in plain JSON:
        enough for completion to tell whether the credit is still the customer's, for a
        refund to undo the switch, and for the receipt to explain the amount."""
        from .models import plan_snapshot

        return {
            'from_plan': plan_snapshot(self.from_plan),
            'currency': self.currency,
            'new_price': str(self.new_price),
            'credit': str(self.credit),
            'to_pay': str(self.to_pay),
            'days_left': int(self.days_left),
            'period_days': self.period_days,
            'paid_amount': str(self.paid_amount) if self.paid_amount is not None else None,
            'paid_currency': self.paid_currency,
            'quoted_at': self.quoted_at.isoformat(),
        }


def quote_upgrade(subscription, to_plan, *, buyer_currency, gateway_name=None, now=None):
    """Price moving `subscription` onto `to_plan` now. Raises an `UpgradeRefused`, or
    `wallet.exchange.ExchangeRateUnavailable` when a price has to cross currencies and
    the operator's rate is missing or stale.

    The credit is read from the LATEST payment of the current run on the current plan
    (`Subscription.current_run_payments`): what it cost, and the period it bought. Days
    left are the row's own (its `effective_end`), so stacked renewals, a trial's end and
    a refund that shortened the cover are all already in them. The credit never exceeds
    what the running payments of this run were — cover nobody paid for (a trial, an
    operator's grant) is credited at nothing.
    """
    now = now or timezone.now()
    if not subscription.is_active:
        raise NoRunningCover(f"Subscription {subscription.id} has no running cover")
    from_plan = subscription.plan
    if to_plan.pk == from_plan.pk:
        raise NotAnUpgrade(f"Subscription {subscription.id} is already on plan {to_plan.pk}")

    new = charge_for(to_plan.price, to_plan.currency, buyer_currency)
    current = charge_for(from_plan.price, from_plan.currency, buyer_currency)
    if new.amount <= current.amount:
        raise NotAnUpgrade(f"Plan {to_plan.pk} ({new.amount} {buyer_currency}) is not "
                           f"dearer than plan {from_plan.pk} ({current.amount})")

    # WHOLE days left: the day under way counts as used. Credited by the second, what
    # is left to pay moved every second or so, and a customer who paid the figure they
    # were shown was refused as `price_changed`. By whole days it holds for a day.
    seconds_left = max(0, int((subscription.effective_end - now).total_seconds()))
    days_left = Decimal(seconds_left // 86400)

    running = list(subscription.current_run_payments()
                   .filter(covers_until__gt=now)
                   .select_related('subscription__plan')
                   .order_by('-completed_at', '-id'))
    on_this_plan = [p for p in running if p.plan_bought()['plan_id'] == from_plan.pk]
    anchor = on_this_plan[0] if on_this_plan else None

    credit = Decimal('0.00')
    period_days = from_plan.duration_days
    paid_amount = paid_currency = None
    if anchor is not None:
        period_days = int(anchor.plan_bought()['duration_days'] or from_plan.duration_days)
        paid_amount, paid_currency = anchor.amount, anchor.currency
        raw = (paid_amount * days_left / Decimal(max(period_days, 1)))
        ceiling = sum((p.amount for p in running if p.currency == paid_currency),
                      Decimal('0'))
        native = min(raw, ceiling).quantize(CENT, rounding=ROUND_HALF_UP)
        credit = (native if paid_currency == buyer_currency
                  else charge_for(native, paid_currency, buyer_currency).amount)

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
        period_days=period_days, paid_amount=paid_amount, paid_currency=paid_currency,
        priced=new, quoted_at=now)
