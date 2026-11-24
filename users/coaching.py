"""One-to-one coaching, sold by the month, with the platform's commission cut from every
month (owner, 2026-09-19).

    accept         month 1 is held: client -> escrow            (`take_hold`)
    first program  month 1 is paid out and starts               (`settle_hold`)
    3 days before  every client is told the month renews        (`remind_upcoming`)
    month ends     month N+1 is charged and paid out at once    (`renew`)
    can't pay      the coaching FREEZES (the owner, 2026-09-19): the relation stays, the
                   client sees the trainer's routines and cannot start them
                   (`training_refusal`), and is reminded on each day of
                   COACHING_FROZEN_REMINDER_DAYS. The hourly run charges again as soon as
                   the wallet covers the month, and that month starts when it is paid.
    leave early    an unpaid month 1 goes back to the client    (`release_hold`)
    do not renew   the client's «لا تجدّد بعد هذا الشهر» (`set_renewal`, Road to 10
                   S2.7, the owner's D12): when the paid month ends the coaching ends
                   instead of renewing — nothing charged, nothing held, the month already
                   paid stays the trainer's. The 3-day reminder says it ends
                   (`coaching_ending_soon`) instead of that it renews. Withdrawn by the
                   client at any time before the renewal run reaches the month's end
    it ends        the trainer's routines stay readable and cannot be started, like a
                   freeze; asking for the trainer again starts a new coaching

Every payout splits the price in one transaction: escrow -> trainer
(`price - commission`) and escrow -> platform revenue (`commission`), with `commission`
rounded to the cent and the trainer taking the remainder, so the two always add up to
the price. The rate is the one copied onto the month when it was charged
(`CoachingPeriod.commission_rate`), never today's.

A renewal is the SERVER moving money the client agreed to when they asked for the
trainer, at the price of the month before it (`CoachingPeriod.price`, which month 1 took
from `agreed_rate`); a trainer who changes their price changes it for new clients only.
Nothing is charged, and the coaching ends, once either person's account is disabled.

No request, build or channel is involved in a renewal, so nothing here consults
`policy.services`: the build that opened the coaching already did
(`require_purchase_allowed` on request and on accepting an invitation).

Relations paid out before months existed (users/0041) were sold as one fee: they have no
month, are never renewed, and read as `active`.

Lock order, as everywhere (`users/trainer_relations.py`): the relation row, then the
period rows, then user rows (FOR KEY SHARE on the actor, FOR NO KEY UPDATE on a client
whose trainer is cleared), then every wallet the movement touches, all at once in
ascending id. Taking the wallets one `move_funds_atomic` at a time would hold escrow
while waiting for a trainer's wallet that a concurrent settlement holds while waiting
for escrow.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import connection, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from policy.money import MoneyType

logger = logging.getLogger(__name__)

CENT = Decimal('0.01')


def commission_rate() -> Decimal:
    return Decimal(str(getattr(settings, 'COACHING_COMMISSION_RATE', '0.20')))


def period_length() -> timedelta:
    return timedelta(days=int(getattr(settings, 'COACHING_PERIOD_DAYS', 30)))


def reminder_lead() -> timedelta:
    return timedelta(days=int(getattr(settings, 'COACHING_RENEWAL_REMINDER_DAYS', 3)))


def frozen_reminder_days() -> tuple:
    return tuple(sorted(getattr(settings, 'COACHING_FROZEN_REMINDER_DAYS', (3, 7, 14, 30))))


def split(price: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """(commission, trainer_share) for `price`; they always add up to `price`."""
    commission = (Decimal(price) * Decimal(rate)).quantize(CENT, rounding=ROUND_HALF_UP)
    return commission, Decimal(price) - commission


def lock_wallets(actor_id, wallets) -> dict:
    """The actor's user row, then every wallet in ascending id. Returns {id: wallet}."""
    from wallet.models import Wallet

    if actor_id is not None and connection.vendor == 'postgresql':
        from django.contrib.auth import get_user_model

        meta = get_user_model()._meta
        with connection.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {connection.ops.quote_name(meta.db_table)} "
                        f"WHERE {connection.ops.quote_name(meta.pk.column)} = %s FOR KEY SHARE",
                        [actor_id])
    ids = sorted({w.pk for w in wallets})
    return {w.pk: w for w in Wallet.objects.select_for_update().filter(pk__in=ids).order_by('pk')}


def notify_fee(kind, period, *, client=True, trainer=True) -> None:
    """Tell each side, on commit, what happened to a month's money: `kind` is 'held'
    (month 1 into escrow), 'released' (out of escrow to the trainer) or 'refunded' (a
    held month back to the client). The spec's table (section 7): "Trainer fee held,
    released, refunded — push yes, both sides; in-app yes; email no".

    Who hears which, decided by the callers: every fact goes to both people, except that
    a trainer hears "released" only when their share has actually landed — a share left
    owed for a stale rate is announced when `settle_owed_payouts` pays it. A RENEWAL is
    not announced through here: the month is charged and paid out in one step, so it is
    never "held", and its one event (`CoachingRenewedEvent`) tells the client
    (`coaching_renewed`) and the trainer (`coaching_fee_released`) from its listeners.
    Each notice is written once per month and person (the listeners key it on the month).

    On commit and `robust`: a notice that cannot be queued is logged and never undoes,
    delays or blocks the money it describes. No amount in it (`money_events`).
    """
    from notifications.domain import money_events
    from notifications.domain.dispatcher import emit_event

    Event = {'held': money_events.CoachingFeeHeldEvent,
             'released': money_events.CoachingFeeReleasedEvent,
             'refunded': money_events.CoachingFeeRefundedEvent}[kind]
    events = []
    if client:
        events.append(Event(recipient_id=period.client_id, actor_id=period.trainer_id,
                            other_name=period.trainer.full_name, period_id=period.pk,
                            month=period.number, role='client'))
    if trainer:
        events.append(Event(recipient_id=period.trainer_id, actor_id=period.client_id,
                            other_name=period.client.full_name, period_id=period.pk,
                            month=period.number, role='trainer'))
    for event in events:
        transaction.on_commit(lambda e=event: emit_event(e), robust=True)


def open_period(relation, hold_tx, *, priced=None, inherit=None):
    """Record month N for money just moved client -> escrow. Caller holds the relation.

    The price of record and the rate that turned it into `price` are copied on
    (SIDE_BUGS S-151): from `priced` (a `subscription.pricing.Priced`) when this month
    was just priced, or from `inherit` — the month before — for a renewal, which is
    charged at that month's client-currency price and so at its conversion.
    """
    from django.db.models import Max

    from .models import CoachingPeriod

    snapshot = {}
    if priced is not None:
        snapshot = {'price_amount': priced.price_amount,
                    'price_currency': priced.price_currency,
                    'exchange_rate': priced.rate,
                    'exchange_rate_value': getattr(priced.rate, 'rate', None)}
    elif inherit is not None:
        snapshot = {'price_amount': inherit.price_amount,
                    'price_currency': inherit.price_currency,
                    'exchange_rate_id': inherit.exchange_rate_id,
                    'exchange_rate_value': inherit.exchange_rate_value}
    last = relation.periods.aggregate(n=Max('number'))['n'] or 0
    return CoachingPeriod.objects.create(
        trainer_id=relation.trainer_id, client_id=relation.client_id, relation=relation,
        # The month's price AND its unit, both read off the money that was actually
        # held. A price without a currency is not a price, and the payout has to know
        # which escrow holds this month and which platform wallet its commission goes
        # to — both are per currency (`wallet.utils`).
        number=last + 1, price=hold_tx.amount, currency=hold_tx.currency,
        commission_rate=commission_rate(),
        hold_reference=hold_tx.reference_id, **snapshot)


def pay_out(period, *, actor_id, starts_at, trainer_purpose):
    """Escrow -> trainer and escrow -> platform for `period`, which starts at `starts_at`.

    Inside the caller's transaction, with the relation and the period locked. Returns the
    trainer's ledger row (the commission's when the trainer's share is nothing, or when
    the share is owed). Raises `HoldFailed` when escrow cannot cover the month, leaving
    everything as it was.

    The trainer is paid in THEIR currency, never in the month's: their wallet is read
    without a currency (SIDE_BUGS S-058). Passing the month's let an untouched trainer
    wallet take the client's currency on the first payout, after which the trainer's
    rate — a bare number in their wallet's currency — was read in the other unit.

    A share that has to cross a currency when the operator's rate is missing or stale
    stays in escrow, OWED (`payout_owed_since`), and the month is still paid and
    started (SIDE_BUGS S-082): the client's charge is in their own currency and the
    commission stays in it too, so neither depends on the rate, and refusing them froze
    month 1 at 'held' and rolled back every renewal while the coaching kept running.
    `settle_owed_payouts` pays the share once the rate is fresh.
    """
    from wallet.models import move_funds_atomic
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    from .trainer_relations import HoldFailed, _wallet_for

    from wallet.exchange import ExchangeRateUnavailable
    from wallet.models import exchange_funds_atomic

    # EVERYTHING IN THE MONTH'S OWN CURRENCY, which is the client's: that is what was
    # charged and what escrow is holding. One ledger wallet per currency
    # (SIDE_BUGS S-001), resolved by the money rather than by a default.
    money = period.currency
    escrow = get_escrow_wallet(money)
    # No currency: arriving money never chooses the trainer's (see the docstring).
    trainer_wallet = _wallet_for(period.trainer, 'trainer')
    platform = get_platform_revenue_wallet(money)
    locked = lock_wallets(actor_id, [escrow, trainer_wallet, platform])
    if locked[escrow.pk].balance < period.price:
        logger.error("Escrow cannot cover coaching period %s", period.pk)
        raise HoldFailed("escrow cannot cover this month")

    commission, share = split(period.price, period.commission_rate)
    common = {'period_id': period.pk, 'hold_reference': period.hold_reference,
              'trainer_id': period.trainer_id, 'client_id': period.client_id,
              'month': period.number}
    payout = fee = None
    owed = False
    if share > 0:
        if trainer_wallet.currency == money:
            payout = move_funds_atomic(escrow, trainer_wallet, share, actor_id=actor_id,
                                       currency=money, tx_type='transfer',
                                       money_type=MoneyType.COACHING_1TO1,
                                       metadata={'purpose': trainer_purpose, **common})
        else:
            # THE ONE PLACE COACHING MONEY CROSSES A CURRENCY. The client paid in
            # theirs and the trainer is owed in theirs; escrow cannot hold both, and a
            # person's wallet cannot change unit. So this is an explicit exchange: a
            # debit out of escrow in the month's currency and a credit into the
            # trainer's in theirs, both stamped with the rate row used, so a refund
            # and an audit read what was actually done rather than today's number.
            # THE ESCROW SIDE IS THE GIVEN ONE: exactly `share` leaves escrow, and
            # what the trainer receives is derived from it. The other way round —
            # converting the trainer's figure and debiting whatever that came back as
            # — leaves a rounded remainder in a shared escrow wallet on every month,
            # so `commission + share` would stop adding up to `price` and the month
            # would no longer reconcile against the money that actually moved.
            try:
                _debit, payout = exchange_funds_atomic(
                    escrow, trainer_wallet, share, amount_in=money,
                    actor_id=actor_id, money_type=MoneyType.COACHING_1TO1,
                    metadata={'purpose': trainer_purpose, **common})
            except ExchangeRateUnavailable as exc:
                # Nothing moved: the rate is looked up before either leg. The share
                # stays in escrow, owed, and the month goes ahead (see the docstring).
                logger.error("Coaching period %s: trainer's share owed until the rate "
                             "is fresh: %s", period.pk, exc)
                owed = True
    if commission > 0:
        # The platform's cut stays in the currency the month was sold in. It is the
        # platform's own money in its own books; converting it would be an exchange
        # nobody asked for, and it would put a rate between a commission and the price
        # it is a percentage of.
        fee = move_funds_atomic(escrow, platform, commission, actor_id=actor_id,
                                currency=money, tx_type='transfer',
                                money_type=MoneyType.COACHING_1TO1,
                                metadata={'purpose': 'coaching_commission',
                                          'rate': str(period.commission_rate), **common})
    period.status = 'settled'
    period.commission, period.trainer_share = commission, share
    period.payout_reference = payout.reference_id if payout else ''
    period.commission_reference = fee.reference_id if fee else ''
    period.starts_at, period.ends_at = starts_at, starts_at + period_length()
    period.payout_owed_since = timezone.now() if owed else None
    period.save(update_fields=['status', 'commission', 'trainer_share', 'payout_reference',
                               'commission_reference', 'starts_at', 'ends_at',
                               'payout_owed_since', 'updated_at'])
    return payout or fee


def settle_owed_payouts(now=None) -> dict:
    """Pay every trainer's share that `pay_out` left owed in escrow, now that it can be.

    One period at a time, each in its own transaction with the period row locked
    first and then the two wallets (the order everything else uses after the relation;
    this touches no relation). Stops at the first rate refusal: the rate is the same
    for all of them, so the rest would be refused too and are tried next hour. Returns
    `{'paid': n, 'failed': [period ids]}`.
    """
    from wallet.exchange import ExchangeRateUnavailable
    from wallet.models import exchange_funds_atomic, move_funds_atomic
    from wallet.utils import get_escrow_wallet

    from .models import CoachingPeriod
    from .trainer_relations import _wallet_for

    paid, failed = 0, []
    ids = list(CoachingPeriod.objects.filter(payout_owed_since__isnull=False)
               .order_by('payout_owed_since', 'pk').values_list('pk', flat=True))
    for period_id in ids:
        try:
            with transaction.atomic():
                period = (CoachingPeriod.objects.select_for_update(of=('self',))
                          .select_related('trainer')
                          .filter(pk=period_id, payout_owed_since__isnull=False).first())
                if period is None:
                    continue
                money, share = period.currency, period.trainer_share or Decimal('0')
                escrow = get_escrow_wallet(money)
                trainer_wallet = _wallet_for(period.trainer, 'trainer')
                locked = lock_wallets(None, [escrow, trainer_wallet])
                if share <= 0:
                    payout = None
                elif locked[escrow.pk].balance < share:
                    logger.error("Escrow cannot cover the owed share of period %s", period.pk)
                    failed.append(period.pk)
                    continue
                else:
                    common = {'period_id': period.pk, 'hold_reference': period.hold_reference,
                              'trainer_id': period.trainer_id, 'client_id': period.client_id,
                              'month': period.number, 'purpose': 'coaching_payout',
                              'owed_since': period.payout_owed_since.isoformat()}
                    trainer_wallet = locked[trainer_wallet.pk]
                    if trainer_wallet.currency == money:
                        payout = move_funds_atomic(
                            escrow, trainer_wallet, share, actor_id=None, currency=money,
                            tx_type='transfer', money_type=MoneyType.COACHING_1TO1,
                            metadata=common)
                    else:
                        _debit, payout = exchange_funds_atomic(
                            escrow, trainer_wallet, share, amount_in=money, actor_id=None,
                            money_type=MoneyType.COACHING_1TO1, metadata=common)
                period.payout_reference = payout.reference_id if payout else ''
                period.payout_owed_since = None
                period.save(update_fields=['payout_reference', 'payout_owed_since',
                                           'updated_at'])
                if payout is not None:
                    # The share landed now: the trainer hears it. The client was told
                    # when the month was paid out (`notify_fee`).
                    notify_fee('released', period, client=False)
                paid += 1
        except ExchangeRateUnavailable as exc:
            logger.warning("Owed coaching payouts wait for a fresh rate: %s", exc)
            break
        except Exception:
            logger.exception("Owed coaching payout failed for period %s", period_id)
            failed.append(period_id)
    return {'paid': paid, 'failed': failed}


# ------------------------------------------------------------------------ renewal
def due_relation_ids(now=None) -> list[int]:
    """Approved relations whose latest month is paid out and over, on-time ones first.

    A frozen one is included only when there is something to do: the client's wallet
    now covers the month, a reminder step has come round, the client asked not to
    renew (S2.7), or the trainer scheduled its end (SIDE_BUGS S-266): either of the last
    two is ended at once by `renew`, before any money is read, so nothing is charged
    and nothing held, a top-up that landed meanwhile included. Frozen coachings never end
    by themselves, and locking every one of them every hour grew without bound until the
    run's time limit starved the on-time renewals behind them. Decided on one unlocked
    read; `renew` re-checks everything under its locks.
    """
    on_time, waking = _due(now)
    return on_time + waking


def _due(now=None) -> tuple[list[int], list[int]]:
    """`due_relation_ids` as its two queues, each in id order: `(on_time, waking)`."""
    from wallet.models import Wallet

    from .models import CoachingPeriod, TrainerClientRelation

    now = now or timezone.now()
    latest = CoachingPeriod.objects.filter(relation=OuterRef('pk')).order_by('-number')
    rows = list(TrainerClientRelation.objects
                .filter(status='approved')
                .annotate(last_status=Subquery(latest.values('status')[:1]),
                          last_end=Subquery(latest.values('ends_at')[:1]),
                          last_frozen=Subquery(latest.values('frozen_at')[:1]),
                          last_notices=Subquery(latest.values('frozen_notices')[:1]),
                          last_price=Subquery(latest.values('price')[:1]))
                .filter(last_status='settled', last_end__lte=now)
                .order_by('pk')
                .values_list('pk', 'client_id', 'last_frozen', 'last_notices', 'last_price',
                             'client_stops_renewal', 'ends_at_period_end'))
    on_time = [pk for pk, _client, frozen, _n, _p, _stop, _end in rows if frozen is None]
    frozen = [row for row in rows if row[2] is not None]
    if not frozen:
        return on_time, []
    balances = dict(Wallet.objects.filter(owner_id__in={row[1] for row in frozen})
                    .values_list('owner_id', 'balance'))
    steps = frozen_reminder_days()
    waking = []
    for pk, client_id, frozen_at, notices, price, stops, trainer_ends in frozen:
        reached = sum(1 for d in steps if d <= (now - frozen_at).days)
        # Either side's end wakes it (the trainer's since S-266: it stayed frozen for
        # days, ending only on a reminder step or a top-up).
        if (stops or trainer_ends or balances.get(client_id, Decimal('0')) >= price
                or reached > (notices or 0)):
            waking.append(pk)
    return on_time, waking


def renew(relation_id, now=None) -> str:
    """Charge and pay out the next month for one relation, if it is due.

    Returns what happened: 'renewed'; 'frozen' (the wallet cannot cover the month: the
    coaching is paused, newly or still); 'reminded' (still paused, and a reminder step
    was reached); 'ended' (an account disabled, the trainer's scheduled end, or the
    client's «do not renew after this month»); or
    'not_due' / 'gone' / 'not_started' when there is nothing to do.
    Idempotent: a second call for the same month finds it already charged.
    """
    from notifications.domain.dispatcher import emit_event
    from notifications.domain.trainer_client_events import (CoachingPaymentDueEvent,
                                                            CoachingRenewedEvent)
    from wallet.models import move_funds_atomic
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    from .models import TrainerClientRelation
    from .trainer_relations import _wallet_for

    now = now or timezone.now()
    with transaction.atomic():
        relation = (TrainerClientRelation.objects.select_for_update(of=('self',))
                    .select_related('trainer', 'client')
                    .filter(pk=relation_id, status='approved').first())
        if relation is None:
            return 'gone'
        last = relation.periods.select_for_update().order_by('-number').first()
        if last is None or last.status != 'settled' or last.ends_at is None:
            return 'not_started'
        if last.ends_at > now:
            return 'not_due'
        # A suspended trainer cannot coach and a suspended client cannot leave: charging
        # either would take a month nobody can deliver or refuse.
        trainer, client = relation.trainer, relation.client
        if not (trainer.is_active and trainer.is_trainer and client.is_active):
            end_coaching(relation, reason='account_disabled')
            return 'ended'
        if relation.ends_at_period_end:
            # The trainer asked to stop when this month ended; it has. Checked before the
            # money, so a scheduled end also ends a frozen coaching.
            end_coaching(relation, reason='ended_by_trainer')
            return 'ended'
        if relation.client_stops_renewal:
            # The client asked not to renew after this month (S2.7, the owner's D12).
            # Checked before the money, like the trainer's end, and read under the
            # relation's lock, so a withdrawal that committed first is seen and one
            # still waiting finds the row gone: nothing is charged and nothing is held,
            # and the month that just ended stays paid out to the trainer — no money
            # moves at all. The operators' record of endings gets its row now, not when
            # the flag was set: the client could still have withdrawn it then.
            from .models import CoachingEnding
            CoachingEnding.record(trainer=trainer, client=client, ended_by='client',
                                  reason=None, scheduled=True)
            end_coaching(relation, reason='stopped_by_client')
            return 'ended'
        # The month's own price, never the trainer's current rate.
        price = last.price

        client_wallet = _wallet_for(relation.client, 'client', last.currency)
        trainer_name = relation.trainer.full_name or relation.trainer.username
        # Decided on an unlocked read: nothing below the freeze takes a wallet, and a
        # top-up that lands meanwhile is charged on the next run; the paying branch
        # re-checks under the wallet locks.
        if client_wallet.balance < price:
            if last.frozen_at is None:
                last.frozen_at, last.frozen_notices = now, 0
                last.save(update_fields=['frozen_at', 'frozen_notices', 'updated_at'])
                step = 0
            else:
                # "Keep asking": one reminder per step of COACHING_FROZEN_REMINDER_DAYS.
                days = (now - last.frozen_at).days
                reached = sum(1 for d in frozen_reminder_days() if d <= days)
                if reached <= last.frozen_notices:
                    return 'frozen'
                last.frozen_notices = reached
                last.save(update_fields=['frozen_notices', 'updated_at'])
                step = reached
            event = CoachingPaymentDueEvent(actor_id=relation.trainer_id,
                                            recipient_id=relation.client_id,
                                            trainer_name=trainer_name, period_id=last.pk,
                                            step=step)
            transaction.on_commit(lambda: emit_event(event))
            return 'frozen' if step == 0 else 'reminded'

        # The month renews at the price of the month before it, which is already in
        # the client's currency, so the CHARGE converts nothing. The trainer's share
        # may still cross a currency on the way out; if the rate is stale then, the
        # share is left owed in escrow and the month renews anyway (`pay_out`), so a
        # stale rate never freezes — or silently extends — a running coaching.
        money = last.currency
        escrow = get_escrow_wallet(money)
        locked = lock_wallets(None, [client_wallet, escrow,
                                     _wallet_for(relation.trainer, 'trainer'),
                                     get_platform_revenue_wallet(money)])
        if locked[client_wallet.pk].balance < price:
            return 'due'    # spent between the read and the lock; the next run decides
        hold = move_funds_atomic(client_wallet, escrow, price, actor_id=None,
                                 currency=money,
                                 tx_type='transfer', money_type=MoneyType.COACHING_1TO1,
                                 metadata={'purpose': 'coaching_renewal_charge',
                                           'trainer_id': relation.trainer_id,
                                           'client_id': relation.client_id,
                                           'relation_id': relation.pk,
                                           'month': last.number + 1})
        period = open_period(relation, hold, inherit=last)
        # On time, the new month starts where the last ended (a missed hourly run is
        # caught up month by month). After a freeze it starts when it is paid: the days
        # spent paused were not coached.
        pay_out(period, actor_id=None, starts_at=now if last.frozen_at else last.ends_at,
                trainer_purpose='coaching_renewal_payout')
        # ONE event for the renewal. Its listeners tell the client (`coaching_renewed`)
        # and, once the share has landed, the trainer (`coaching_fee_released`,
        # `notifications/listeners/money_listeners.py`).
        event = CoachingRenewedEvent(actor_id=relation.trainer_id,
                                     recipient_id=relation.client_id,
                                     trainer_name=trainer_name, period_id=period.pk)
        transaction.on_commit(lambda: emit_event(event))
        return 'renewed'


def remind_upcoming(now=None, *, batch=None, after=None, relation_ids=None) -> int:
    """Tell every client whose paid month ends within COACHING_RENEWAL_REMINDER_DAYS what
    happens then (the owner, 2026-09-19: every client, whether or not the wallet covers
    it): that it renews (`coaching_renewal_soon`), or — when the client asked not to
    renew (S2.7) — that the coaching ends and nothing is charged (`coaching_ending_soon`).
    Which one is read under the relation's lock. Once per month
    (`renewal_reminder_sent_at`, either notice): a client who switches the choice after
    being told is not told again, the answer to the switch says it. Not when the trainer
    is ending the coaching. Returns how many were told.

    Kept out of `renew_due`, whose counts the beat task returns unchanged.

    With a `batch` (`training_platform.batching.Batch`, the beat task's), only one page
    of relations with an id after `after`; `batch.cut` then says more remain.

    `relation_ids` limits it to those relations; None (the beat task's call) is every
    relation, as before. For a caller that must touch only its own rows
    (a development tool that resets its own accounts on a shared database).
    """
    from notifications.domain.dispatcher import emit_event
    from notifications.domain.trainer_client_events import (CoachingEndingSoonEvent,
                                                            CoachingRenewalSoonEvent)

    from .models import CoachingPeriod, TrainerClientRelation

    now = now or timezone.now()
    lead = reminder_lead()
    latest = CoachingPeriod.objects.filter(relation=OuterRef('pk')).order_by('-number')
    due = (TrainerClientRelation.objects
           .filter(status='approved', ends_at_period_end=False)
           .annotate(last_id=Subquery(latest.values('pk')[:1]))
           .filter(last_id__in=CoachingPeriod.objects.filter(
               status='settled', renewal_reminder_sent_at__isnull=True,
               ends_at__gt=now, ends_at__lte=now + lead).values('pk')))
    if relation_ids is not None:
        due = due.filter(pk__in=list(relation_ids))
    if batch is None:
        ids = list(due.order_by('pk').values_list('pk', flat=True))
    else:
        ids = [row.pk for row in batch.page(due.only('pk'), after)]
    told = 0
    for relation_id in ids:
        if batch is not None:
            if batch.spent():
                break
            batch.done(relation_id)
        with transaction.atomic():
            relation = (TrainerClientRelation.objects.select_for_update(of=('self',))
                        .select_related('trainer')
                        .filter(pk=relation_id, status='approved',
                                ends_at_period_end=False).first())
            if relation is None:
                continue
            last = relation.periods.select_for_update().order_by('-number').first()
            if (last is None or last.status != 'settled' or last.renewal_reminder_sent_at
                    or not (now < last.ends_at <= now + lead)):
                continue
            last.renewal_reminder_sent_at = now
            last.save(update_fields=['renewal_reminder_sent_at', 'updated_at'])
            # Whole days, rounded up: 71 hours left reads "3 days", not "2".
            left = last.ends_at - now
            days = max(1, left.days + (1 if left % timedelta(days=1) else 0))
            Event = (CoachingEndingSoonEvent if relation.client_stops_renewal
                     else CoachingRenewalSoonEvent)
            event = Event(
                actor_id=relation.trainer_id, recipient_id=relation.client_id,
                trainer_name=relation.trainer.full_name or relation.trainer.username,
                days=days, period_id=last.pk)
            transaction.on_commit(lambda e=event: emit_event(e))
            told += 1
    return told


def running_month(relation, now=None):
    """The paid month the client is in right now, or None."""
    now = now or timezone.now()
    last = relation.periods.order_by('-number').first()
    if last is not None and last.status == 'settled' and last.ends_at and last.ends_at > now:
        return last
    return None


def end_coaching(relation, *, reason):
    """End a relation whose month went unpaid. Caller holds the relation row, in a
    transaction. Nothing is held for it (a renewal only runs on a paid-out month), so
    there is no money to return: the months paid were delivered."""
    from notifications.domain.dispatcher import emit_event
    from notifications.domain.trainer_client_events import (CoachingEndedEvent,
                                                            CoachingFinishedEvent)

    from .models import CustomUser, TrainerClientRelation

    client = CustomUser.objects.select_for_update(no_key=True).get(pk=relation.client_id)
    if client.assigned_trainer_id == relation.trainer_id:
        client.assigned_trainer = None
        client.save(update_fields=['assigned_trainer'])
    relation_id, trainer, client_user = relation.pk, relation.trainer, relation.client
    TrainerClientRelation.objects.filter(pk=relation_id).delete()
    logger.info("Coaching %s ended (%s)", relation_id, reason)
    # "The monthly fee was not paid" only when that is why; otherwise a plain ending.
    Event = CoachingEndedEvent if reason == 'unpaid' else CoachingFinishedEvent
    events = [
        Event(actor_id=trainer.pk, recipient_id=client_user.pk,
              other_name=trainer.full_name or trainer.username, relation_id=relation_id,
              client_id=client_user.pk, trainer_id=trainer.pk),
        Event(actor_id=client_user.pk, recipient_id=trainer.pk,
              other_name=client_user.full_name or client_user.username,
              relation_id=relation_id, client_id=client_user.pk, trainer_id=trainer.pk),
    ]
    transaction.on_commit(lambda: [emit_event(e) for e in events])


def renew_due(now=None, *, max_months=12, batch=None, phase=None, after=None) -> dict:
    """Renew every due relation, catching up at most `max_months` per relation per run.

    One relation's failure does not stop the others; failures are counted and the
    caller (the beat task) raises so the run is retried — every step is idempotent.

    First, every trainer's share left owed by a stale rate is paid if the rate now
    allows it (`settle_owed_payouts`); `trainer_payouts_settled` is counted when any
    was. An owed share that still cannot be paid is not a failure of this run: it waits
    for the operator's rate and is retried every hour.

    With a `batch` (`training_platform.batching.Batch`, the beat task's), at most one
    page of relations: the on-time queue first, then the frozen ones with something to
    do (`_due`), each in id order from `after` within `phase`. When relations remain,
    the result also carries `next`: `{'phase', 'after'}` for the next invocation. Paging
    changes nothing about a renewal — `renew` locks and re-checks each relation — it
    only stops one hour's run from being cut off part-way by the soft time limit, which
    left whoever sorted later unrenewed (the production-readiness audit, 2026-09-25).
    """
    counts: dict[str, int] = {}
    failed = []
    if batch is None or (phase in (None, 'on_time') and after is None):
        owed = settle_owed_payouts(now)
        if owed['paid']:
            counts['trainer_payouts_settled'] = owed['paid']

    def renew_one(relation_id):
        for _month in range(max_months):
            try:
                outcome = renew(relation_id, now)
            except Exception:
                logger.exception("Coaching renewal failed for relation %s", relation_id)
                failed.append(relation_id)
                break
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome != 'renewed':
                break

    on_time, waking = _due(now)
    if batch is None:
        for relation_id in on_time + waking:
            renew_one(relation_id)
        return {'counts': counts, 'failed': failed}

    queues = [('on_time', on_time), ('waking', waking)]
    start = 1 if phase == 'waking' else 0
    for index, (name, ids) in enumerate(queues[start:]):
        cursor = after if index == 0 else None
        last = cursor
        for relation_id in ids:
            if cursor is not None and relation_id <= cursor:
                continue
            if batch.handled >= batch.size or batch.spent():
                return {'counts': counts, 'failed': failed,
                        'next': {'phase': name, 'after': last}}
            batch.done(relation_id)
            last = relation_id
            renew_one(relation_id)
    return {'counts': counts, 'failed': failed}


# ------------------------------------------------------------------------ reading
def coaching_status(relation, now=None) -> dict:
    """What the client's coaching looks like right now (for `GET /api/v1/coaching/current/`)."""
    now = now or timezone.now()
    last = relation.periods.order_by('-number').first()
    body = {'trainer_id': relation.trainer_id, 'since': relation.responded_at,
            'status': 'active', 'period': None, 'renews_at': None,
            # Kept in the shape, always null: an unpaid month no longer has a grace; it
            # freezes (`frozen_since`).
            'grace_until': None, 'frozen_since': None, 'renewal_reminder': False,
            # The trainer is stopping when the current month ends: no renewal.
            'ending': relation.ends_at_period_end,
            # The client's own «do not renew after this month» (S2.7), which they can
            # switch back; `ends_at` says when the coaching ends instead of renewing
            # (either side's choice), and `ending_reminder` that it is within the same
            # few days the renewal reminder uses.
            'stop_renewal': relation.client_stops_renewal,
            'ends_at': None, 'ending_reminder': False}
    if last is None:
        # Held before months existed, or free, or sold as one fee before months existed.
        if relation.hold_reference:
            body['status'] = 'awaiting_first_program'
        return body
    if last.status == 'held':
        body['status'] = 'awaiting_first_program'
        return body
    if last.status == 'refunded':
        # Support returned month 1 (a reversal) while the coaching stayed on: nothing is
        # paid and nothing renews until support resolves it.
        body['status'] = 'unpaid'
        return body
    body['period'] = {'number': last.number, 'starts_at': last.starts_at,
                      'ends_at': last.ends_at}
    if last.frozen_at is not None:
        # The renewal was tried and the wallet could not cover it: paused since the
        # month ended, until a month is paid.
        body['status'] = 'frozen'
        body['frozen_since'] = last.ends_at
    elif relation.ends_at_period_end or relation.client_stops_renewal:
        # Nothing renews: the coaching ends with this month.
        body['ends_at'] = last.ends_at
        body['ending_reminder'] = last.ends_at - now <= reminder_lead()
    else:
        # Includes the minutes between a month's end and the hourly renewal.
        body['renews_at'] = last.ends_at
        body['renewal_reminder'] = last.ends_at - now <= reminder_lead()
    return body


def set_renewal(client, *, renew):
    """The client's «لا تجدّد بعد هذا الشهر» (Road to 10 S2.7, the owner's D12): set it
    (`renew=False`) or withdraw it (`renew=True`) on their approved coaching.

    Returns `(relation, code)`: `code` is None when done — including a repeat, which
    writes nothing — `'no_coaching'` (no approved trainer; `relation` None), or
    `'nothing_to_renew'` when asked to stop a coaching that never renews: no month and
    nothing held (free, or sold as one fee before months existed), or a month 1 support
    returned (`unpaid`: nothing renews until support resolves it). Withdrawing is never
    refused.

    A FROZEN coaching may be stopped: it would renew the moment the wallet covers the
    month, so the choice matters there too. The hourly run then wakes it at once
    (`_due`) and ends it before any money is read — a top-up landing meanwhile is not
    charged. A hold from before months existed may be stopped as well: its first program
    opens a month, which would renew.

    Moves no money. Locks the relation row only (`of=('self',)`, the first lock of the
    order in `users/trainer_relations.py`), which is the lock `renew` takes first: a
    choice made while the renewal runs waits for it and then finds the coaching
    renewed (the choice applies to the new month) or ended (`no_coaching`), never half
    of each. A trainer's end (`ends_at_period_end`) is not touched: it stands whatever
    the client chooses.
    """
    from .models import TrainerClientRelation

    with transaction.atomic():
        relation = (TrainerClientRelation.objects.select_for_update(of=('self',))
                    .select_related('trainer')
                    .filter(client=client, status='approved').first())
        if relation is None:
            return None, 'no_coaching'
        stop = not renew
        if relation.client_stops_renewal == stop:
            return relation, None
        if stop:
            last = relation.periods.order_by('-number').first()
            if ((last is None and not relation.hold_reference)
                    or (last is not None and last.status not in ('held', 'settled'))):
                return relation, 'nothing_to_renew'
        relation.client_stops_renewal = stop
        relation.save(update_fields=['client_stops_renewal', 'updated_at'])
        logger.info("Coaching %s: the client %s the renewal", relation.pk,
                    'stopped' if stop else 'restored')
        return relation, None


# ----------------------------------------------------------------------- training
def training_refusal(user, routine, at=None):
    """Why `user` may not train on `routine` at `at`, or None.

    `at` is when the training happened: None (now), a datetime (a session's start), or a
    DATE (a progress row or a set logged by day).

    The owner (2026-09-19): when a coaching is unpaid, and after it ends, the client
    still SEES the trainer's routines and cannot START them. A routine written by a
    trainer who has coached this client for money (any `CoachingPeriod` for the pair) is
    trainable only while that coaching is paid:
      * the relation is approved and its latest month is held or paid, not frozen —
        this includes the minutes between a month's end and the hourly renewal; or
      * the training falls inside a paid month: a moment inside it (or before the
        renewal that froze it failed), or a date on which a paid month ran for any part
        of the day. Offline training done while paid still syncs after a freeze or an
        end, whatever day it syncs.
    Otherwise 'coaching_frozen' (paused, unpaid; paying resumes it), 'coaching_unpaid'
    (the relation stands but support returned its month: support resolves it) or
    'coaching_ended'.

    A pair with no paid month at all — free coaching, legacy one-fee coaching, routines
    from before months existed — is not affected. Neither is a trainer or admin acting,
    nor a routine the user wrote themselves. The check reads the relation and the
    months, never the `assigned_trainer` pointer.

    A routine PRODUCT the client bought is theirs (`created_by` is the buyer —
    `routine.Routine.purchase`), so no coaching ever freezes it; it answers
    'product_refunded' once an operator refunded the purchase (`routine/products.py`
    revokes the copy, and this stops a training write that still names it). The
    purchase is read only for a routine the user holds as `created_by`, which is never a
    coached client's routine, so a coaching check costs nothing more.
    """
    if routine is None or not getattr(user, 'is_client', False):
        return None
    trainer_id = routine.created_by_id
    if trainer_id is None:
        return None
    if trainer_id == user.pk:
        return _bought_copy_refusal(routine)
    return _trainer_refusal(user.pk, trainer_id, at)


def _bought_copy_refusal(routine):
    """'product_refunded' for the copy of a routine product whose purchase was refunded,
    None for anything else the user holds as its author."""
    purchase_id = getattr(routine, 'purchase_id', None)
    if purchase_id is None:
        return None
    from routine.models import PurchaseStatus, RoutinePurchase

    refunded = RoutinePurchase.objects.filter(
        pk=purchase_id, status=PurchaseStatus.REFUNDED).exists()
    return 'product_refunded' if refunded else None


def _trainer_refusal(client_id, trainer_id, at):
    """`training_refusal` for one (client, trainer) pair: the answer depends on nothing
    else, so a caller holding many routines asks once per trainer."""
    from django.db.models import Q

    from .models import CoachingPeriod, TrainerClientRelation

    months = CoachingPeriod.objects.filter(trainer_id=trainer_id, client_id=client_id)
    if not months.exists():
        return None
    relation = (TrainerClientRelation.objects
                .filter(trainer_id=trainer_id, client_id=client_id, status='approved')
                .only('pk').first())
    last = None
    if relation is not None:
        last = relation.periods.order_by('-number').first()
        if last is None:
            return None     # coached now without months: free, or sold as one fee
        if last.status in ('held', 'settled') and last.frozen_at is None:
            return None
    start, end = _span(at)
    # A paid month covers [starts_at, ends_at), and past its end until the renewal that
    # froze it failed (`frozen_at`): the minutes before the hourly run were paid time.
    covering = months.filter(status='settled', starts_at__lt=end).filter(
        Q(ends_at__gt=start) | Q(frozen_at__gt=start))
    if covering.exists():
        return None
    if last is not None and last.frozen_at is not None:
        return 'coaching_frozen'
    if last is not None and last.status == 'refunded':
        return 'coaching_unpaid'
    return 'coaching_ended'


def _span(at):
    """`at` as an aware [start, end) interval: now or a datetime is a point (end just
    after start); a DATE is that whole day in the server's zone, the zone every
    training date is stored in."""
    from datetime import date, datetime, time

    if at is None:
        at = timezone.now()
    if isinstance(at, datetime):
        moment = at if timezone.is_aware(at) else timezone.make_aware(at)
        return moment, moment + timedelta(microseconds=1)
    if isinstance(at, date):
        day = timezone.make_aware(datetime.combine(at, time.min))
        return day, day + timedelta(days=1)
    moment = timezone.now()
    return moment, moment + timedelta(microseconds=1)


def refusals_for(trainer_id, client_ids, at=None) -> dict:
    """`{client_id: 'coaching_frozen' | 'coaching_unpaid' | 'coaching_ended' | None}` for
    a whole roster, in two queries.

    `training_refusal` costs a handful of queries per pair, which is fine for one write
    and wrong for a screen that lists every client. Same rule, read in bulk: a pair with
    no paid month at all is not affected, an approved relation whose latest month is held
    or paid and not frozen may train, and everything else names why not.
    """
    from .models import CoachingPeriod, TrainerClientRelation

    ids = list(client_ids)
    if not ids:
        return {}
    start, end = _span(at)          # the same interval `training_refusal` judges
    # The APPROVED relation's own months decide "the latest month", exactly as
    # `_trainer_refusal` reads them (`relation.periods.order_by('-number')`). `number`
    # restarts at 1 with each relation, so picking the highest number across a pair's
    # whole history answered with the old relation's month 5 for a client who left and
    # came back — the two functions then disagreed about the same client.
    relations = dict(TrainerClientRelation.objects
                     .filter(trainer_id=trainer_id, client_id__in=ids, status='approved')
                     .values_list('client_id', 'pk'))
    approved = set(relations)
    latest: dict = {}
    for row in (CoachingPeriod.objects
                .filter(trainer_id=trainer_id, client_id__in=ids)
                .order_by('client_id', 'number')
                .values('client_id', 'relation_id', 'status', 'frozen_at',
                        'starts_at', 'ends_at')):
        client_id = row['client_id']
        entry = latest.setdefault(client_id, {'last': None, 'covers': False})
        if relations.get(client_id) is None or row['relation_id'] == relations[client_id]:
            # No approved relation: any month is "the last" — the pair's history is all
            # there is to judge, and every branch below then answers a refusal anyway.
            entry['last'] = row
        # A paid month covers the moment whichever relation it was bought under.
        if (row['status'] == 'settled' and row['starts_at'] and row['ends_at']
                and row['starts_at'] < end
                and (row['ends_at'] > start
                     or (row['frozen_at'] and row['frozen_at'] > start))):
            entry['covers'] = True

    answers = {}
    for client_id in ids:
        entry = latest.get(client_id)
        if entry is None:
            answers[client_id] = None               # never paid for: nothing to refuse
            continue
        last = entry['last']
        if last is None:
            # Coached now with no month under THIS relation: free, or sold as one fee —
            # `_trainer_refusal` returns None for the same case.
            answers[client_id] = None
            continue
        if client_id in approved and last['status'] in ('held', 'settled') and not last['frozen_at']:
            answers[client_id] = None
        elif entry['covers']:
            answers[client_id] = None
        elif client_id in approved and last['frozen_at']:
            answers[client_id] = 'coaching_frozen'
        elif client_id in approved and last['status'] == 'refunded':
            answers[client_id] = 'coaching_unpaid'
        else:
            answers[client_id] = 'coaching_ended'
    return answers


def require_can_train(user, routine, at=None):
    """Raise the refusal `training_refusal` finds (403 `coaching_frozen` /
    `coaching_unpaid` / `coaching_ended`, naming the trainer; or 403 `product_refunded`
    for a refunded routine product). Call it after validation and after an idempotent
    replay, so a retry of training that landed while paid is answered."""
    raise_refusal(training_refusal(user, routine, at), routine.created_by_id if routine else None)


def raise_refusal(refusal, trainer_id):
    """Raise the 403 for a `training_refusal` answer; nothing for None."""
    from .exceptions import CoachingEnded, CoachingFrozen, CoachingUnpaid, ProductRefunded

    if refusal == 'product_refunded':
        raise ProductRefunded()
    if refusal == 'coaching_frozen':
        raise CoachingFrozen(trainer_id)
    if refusal == 'coaching_unpaid':
        raise CoachingUnpaid(trainer_id)
    if refusal == 'coaching_ended':
        raise CoachingEnded(trainer_id)
