"""One-to-one coaching, sold by the month, with the platform's commission cut from every
month (owner, 2026-09-19).

    accept         month 1 is held: client -> escrow            (`take_hold`)
    first program  month 1 is paid out and starts               (`settle_hold`)
    month ends     month N+1 is charged and paid out at once    (`renew`)
    can't pay      one reminder, COACHING_RENEWAL_GRACE_DAYS to top up, then the coaching
                   ends (`renew` -> `end_coaching`)
    leave early    an unpaid month 1 goes back to the client    (`release_hold`)

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


def grace_period() -> timedelta:
    return timedelta(days=int(getattr(settings, 'COACHING_RENEWAL_GRACE_DAYS', 3)))


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


def open_period(relation, hold_tx):
    """Record month N for money just moved client -> escrow. Caller holds the relation."""
    from django.db.models import Max

    from .models import CoachingPeriod

    last = relation.periods.aggregate(n=Max('number'))['n'] or 0
    return CoachingPeriod.objects.create(
        trainer_id=relation.trainer_id, client_id=relation.client_id, relation=relation,
        number=last + 1, price=hold_tx.amount, commission_rate=commission_rate(),
        hold_reference=hold_tx.reference_id)


def pay_out(period, *, actor_id, starts_at, trainer_purpose):
    """Escrow -> trainer and escrow -> platform for `period`, which starts at `starts_at`.

    Inside the caller's transaction, with the relation and the period locked. Returns the
    trainer's ledger row (the commission's when the trainer's share is nothing). Raises
    `HoldFailed` when escrow cannot cover the month, leaving everything as it was.
    """
    from wallet.models import move_funds_atomic
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    from .trainer_relations import HoldFailed, _wallet_for

    escrow = get_escrow_wallet()
    trainer_wallet = _wallet_for(period.trainer, 'trainer')
    platform = get_platform_revenue_wallet()
    locked = lock_wallets(actor_id, [escrow, trainer_wallet, platform])
    if locked[escrow.pk].balance < period.price:
        logger.error("Escrow cannot cover coaching period %s", period.pk)
        raise HoldFailed("escrow cannot cover this month")

    commission, share = split(period.price, period.commission_rate)
    common = {'period_id': period.pk, 'hold_reference': period.hold_reference,
              'trainer_id': period.trainer_id, 'client_id': period.client_id,
              'month': period.number}
    payout = fee = None
    if share > 0:
        payout = move_funds_atomic(escrow, trainer_wallet, share, actor_id=actor_id,
                                   tx_type='transfer', money_type=MoneyType.COACHING_1TO1,
                                   metadata={'purpose': trainer_purpose, **common})
    if commission > 0:
        fee = move_funds_atomic(escrow, platform, commission, actor_id=actor_id,
                                tx_type='transfer', money_type=MoneyType.COACHING_1TO1,
                                metadata={'purpose': 'coaching_commission',
                                          'rate': str(period.commission_rate), **common})
    period.status = 'settled'
    period.commission, period.trainer_share = commission, share
    period.payout_reference = payout.reference_id if payout else ''
    period.commission_reference = fee.reference_id if fee else ''
    period.starts_at, period.ends_at = starts_at, starts_at + period_length()
    period.save(update_fields=['status', 'commission', 'trainer_share', 'payout_reference',
                               'commission_reference', 'starts_at', 'ends_at', 'updated_at'])
    return payout or fee


# ------------------------------------------------------------------------ renewal
def due_relation_ids(now=None) -> list[int]:
    """Approved relations whose latest month is paid out and over."""
    from .models import CoachingPeriod, TrainerClientRelation

    now = now or timezone.now()
    latest = CoachingPeriod.objects.filter(relation=OuterRef('pk')).order_by('-number')
    return list(TrainerClientRelation.objects
                .filter(status='approved')
                .annotate(last_status=Subquery(latest.values('status')[:1]),
                          last_end=Subquery(latest.values('ends_at')[:1]))
                .filter(last_status='settled', last_end__lte=now)
                .order_by('pk').values_list('pk', flat=True))


def renew(relation_id, now=None) -> str:
    """Charge and pay out the next month for one relation, if it is due.

    Returns what happened: 'renewed', 'reminded', 'due' (still in grace, already told),
    'ended' (unpaid past the grace, or an account disabled), or 'not_due' / 'gone' /
    'not_started' when there is nothing to do.
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
            # The trainer asked to stop when this month ended; it has.
            end_coaching(relation, reason='ended_by_trainer')
            return 'ended'
        # The month's own price, never the trainer's current rate.
        price = last.price

        client_wallet = _wallet_for(relation.client, 'client')
        trainer_name = relation.trainer.full_name or relation.trainer.username
        # Decided on an unlocked read: the unpaid branch takes a user row, which must
        # never come after a wallet lock. A top-up that lands meanwhile is charged on
        # the next run; the paying branch re-checks under the wallet locks.
        if client_wallet.balance < price:
            if now >= last.ends_at + grace_period():
                end_coaching(relation, reason='unpaid')
                return 'ended'
            if last.renewal_reminder_sent_at is not None:
                return 'due'
            last.renewal_reminder_sent_at = now
            last.save(update_fields=['renewal_reminder_sent_at', 'updated_at'])
            days = max(1, (last.ends_at + grace_period() - now).days)
            event = CoachingPaymentDueEvent(actor_id=relation.trainer_id,
                                            recipient_id=relation.client_id,
                                            trainer_name=trainer_name, days=days,
                                            period_id=last.pk)
            transaction.on_commit(lambda: emit_event(event))
            return 'reminded'

        escrow = get_escrow_wallet()
        locked = lock_wallets(None, [client_wallet, escrow,
                                     _wallet_for(relation.trainer, 'trainer'),
                                     get_platform_revenue_wallet()])
        if locked[client_wallet.pk].balance < price:
            return 'due'    # spent between the read and the lock; the next run decides
        hold = move_funds_atomic(client_wallet, escrow, price, actor_id=None,
                                 tx_type='transfer', money_type=MoneyType.COACHING_1TO1,
                                 metadata={'purpose': 'coaching_renewal_charge',
                                           'trainer_id': relation.trainer_id,
                                           'client_id': relation.client_id,
                                           'relation_id': relation.pk,
                                           'month': last.number + 1})
        period = open_period(relation, hold)
        pay_out(period, actor_id=None, starts_at=last.ends_at,
                trainer_purpose='coaching_renewal_payout')
        event = CoachingRenewedEvent(actor_id=relation.trainer_id,
                                     recipient_id=relation.client_id,
                                     trainer_name=trainer_name, period_id=period.pk)
        transaction.on_commit(lambda: emit_event(event))
        return 'renewed'


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
              other_name=trainer.full_name or trainer.username, relation_id=relation_id),
        Event(actor_id=client_user.pk, recipient_id=trainer.pk,
              other_name=client_user.full_name or client_user.username,
              relation_id=relation_id),
    ]
    transaction.on_commit(lambda: [emit_event(e) for e in events])


def renew_due(now=None, *, max_months=12) -> dict:
    """Renew every due relation, catching up at most `max_months` per relation per run.

    One relation's failure does not stop the others; failures are counted and the
    caller (the beat task) raises so the run is retried — every step is idempotent.
    """
    counts: dict[str, int] = {}
    failed = []
    for relation_id in due_relation_ids(now):
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
    return {'counts': counts, 'failed': failed}


# ------------------------------------------------------------------------ reading
def coaching_status(relation, now=None) -> dict:
    """What the client's coaching looks like right now (for `GET /api/v1/coaching/current/`)."""
    now = now or timezone.now()
    last = relation.periods.order_by('-number').first()
    body = {'trainer_id': relation.trainer_id, 'since': relation.responded_at,
            'status': 'active', 'period': None, 'renews_at': None, 'grace_until': None,
            # The trainer is stopping when the current month ends: no renewal.
            'ending': relation.ends_at_period_end}
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
    if last.renewal_reminder_sent_at is not None and last.ends_at <= now:
        # A renewal was tried and the wallet could not cover it.
        body['status'] = 'payment_due'
        body['grace_until'] = last.ends_at + grace_period()
    elif not relation.ends_at_period_end:
        # Includes the minutes between a month's end and the hourly renewal.
        body['renews_at'] = last.ends_at
    return body

