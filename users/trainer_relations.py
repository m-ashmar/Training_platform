"""The lifecycle of a trainer/client relation and the money held against it.

Three events, one place. They were spread across `users/views.py` and
`routine/views.py`, connected by nothing but a string in transaction metadata, and the
consequences were: money taken with no way to give it back, money paid out of a shared
pool on an unrelated endpoint at whatever price the trainer currently advertised, and a
relation granted even when the payment for it had failed.

A hold has an identity (`TrainerClientRelation.hold_reference`) and a price agreed in
advance (`agreed_rate`). Every one of the three events below is idempotent against that
identity, so a repeat cannot take twice, refund twice, or pay twice.

Lock order. Everything that moves a relation's money, or changes the people on either
side of it, takes its locks in one order:

  1. relation rows   FOR UPDATE, `of=('self',)` so no joined user row is locked with it
  2. user rows       FOR NO KEY UPDATE, never FOR UPDATE — and always BEFORE any wallet
  3. wallets         FOR UPDATE in ascending id, inside `move_funds_atomic` — which takes
                     FOR KEY SHARE on the actor's user row before its wallets

User rows are FOR NO KEY UPDATE because every ledger row carries `actor`, a foreign key
Postgres checks at COMMIT with FOR KEY SHARE on the actor's user row, and FOR KEY SHARE
conflicts with FOR UPDATE and with nothing weaker. Approval held the client's row FOR
UPDATE and then waited for the client's wallet while that client's transfer, or a
payment payout, held the wallet and waited at COMMIT for the row. Measured: 20 deadlocks
in 60 contended approve/transfer pairs, and 6 of 6 for settlement at 0-4 ms offsets.

The one FOR UPDATE on a user row is erasure's, on the account being erased: rewriting
`email` and `username` is an UPDATE of unique columns, which Postgres locks FOR UPDATE
whether we ask for it or not. That is why step 2 says "before any wallet" as well —
erasure returns a hold (wallets) and only then anonymises (user row), and that order,
against the same client's transfer or payout, deadlocked every time it was contended:
"deadlock detected ... while updating tuple in relation users_customuser". The lock is
taken up front in `release_holds_before_erasure` instead.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from policy.money import MoneyType

logger = logging.getLogger(__name__)


class HoldFailed(Exception):
    """The client's charge could not be held, so the relation must not be granted."""


class TrainerHasActiveClients(Exception):
    """A trainer asked to erase their account while still coaching someone.

    The owner (2026-09-19): a trainer with active clients can never delete the account.
    Active means every APPROVED relation in which they are the trainer — month 1 held,
    a paid month running, a month that ends at renewal, or a paused (frozen) coaching.
    Each has a way out first: `trainer/unassign-client/` (a paid month only with
    `at_period_end`, and the erasure then waits for the month to end).
    """

    def __init__(self, count):
        super().__init__(count)
        self.count = count


def _wallet_for(user, owner_type, currency=None):
    """This person's wallet, in the currency the money is arriving in.

    Delegates to `wallet.models.wallet_for` rather than a bare `get_or_create`: that
    left a brand-new wallet on the platform's BASE currency whatever the money was, so
    a trainer signing up to be paid in dollars got a pound wallet the first payout then
    refused. A wallet that has already held money keeps its own currency, which is the
    person's and is not ours to change (`wallet.models.set_currency_for`).
    """
    from wallet.models import wallet_for

    return wallet_for(user, owner_type, currency)


def price_of(relation) -> Decimal:
    """What this pair agreed to, as the PRICE OF RECORD: the trainer's number, in the
    trainer's own currency.

    Falls back to the trainer's rate only when the row predates `agreed_rate`, never to
    a rate raised after the client asked. What the CLIENT is charged is this converted
    into the client's currency — `charge_for_client`, because a person pays in one
    currency and it is theirs (the owner, 2026-09-21).
    """
    if relation.agreed_rate is not None:
        return Decimal(relation.agreed_rate)
    return Decimal(getattr(relation.trainer, 'trainer_hourly_rate', None) or 0)


def charge_for_client(relation):
    """`(amount, currency, rate_row)`: what this client pays for one month, in theirs.

    The trainer prices their coaching in the currency they trade in; the client pays in
    the currency THEY trade in. Same rule as a plan (`subscription.pricing.charge_for`)
    and the same single conversion boundary underneath, so the two can never disagree
    about a rate or a rounding.

    Raises `wallet.exchange.ExchangeRateUnavailable` when the two differ and the
    operator's rate is missing or stale. A pair who already share a currency converts
    nothing and is never stopped by a stale rate.
    """
    from subscription.pricing import charge_for
    from wallet import currency_for

    priced = charge_for(price_of(relation), currency_for(relation.trainer),
                        currency_for(relation.client))
    return priced.amount, priced.currency, priced.rate


def take_hold(relation, actor) -> None:
    """Move the agreed price from the client into escrow, once.

    Raises `HoldFailed` rather than logging and continuing. The caller grants a
    relationship on the strength of this, and granting it after the payment failed is
    how a trainer got a client for free and the client got charged nothing — while the
    routine app still paid that trainer out of everyone else's money.
    """
    from wallet.models import move_funds_atomic
    from wallet.utils import get_escrow_wallet

    from wallet.exchange import ExchangeRateUnavailable

    if relation.hold_reference:
        return  # already held for this pair
    if price_of(relation) <= 0:
        return
    try:
        # In the CLIENT's currency: they pay in the one currency they trade in,
        # whatever the trainer prices in. Refused rather than guessed when the two
        # differ and the operator's rate is missing or stale — the code travels on the
        # exception so the view can answer it.
        amount, currency, _rate = charge_for_client(relation)
    except ExchangeRateUnavailable as exc:
        failure = HoldFailed(str(exc))
        failure.code = exc.code
        raise failure from exc
    if amount <= 0:
        return

    client_wallet = _wallet_for(relation.client, "client", currency)
    if client_wallet.balance < amount:
        raise HoldFailed("Insufficient client wallet balance for the trainer charge")
    try:
        tx = move_funds_atomic(
            client_wallet, get_escrow_wallet(currency), amount, currency=currency,
            actor_id=getattr(actor, 'id', None), tx_type='transfer',
            money_type=MoneyType.COACHING_1TO1,
            metadata={'purpose': 'trainer_request_hold',
                      'trainer_id': relation.trainer_id,
                      'client_id': relation.client_id,
                      'relation_id': relation.id})
    except ValueError as exc:
        # `move_funds_atomic`'s own refusals (balance, currency): our words, safe to show.
        raise HoldFailed(str(exc)) from exc
    except Exception as exc:
        # Anything else is the database's words, and approval puts this text in a 402
        # body: a lost deadlock sent Postgres process ids, transaction ids and relation
        # names to the app. Logged here, summarised there.
        logger.exception("Could not take the hold for relation %s", relation.pk)
        raise HoldFailed("the charge could not be moved; please try again") from exc
    relation.hold_reference = tx.reference_id
    relation.save(update_fields=['hold_reference', 'updated_at'])
    # Month 1 of the coaching, with the commission rate of today (users/coaching.py).
    from .coaching import open_period
    open_period(relation, tx)


def _move_hold(relation, actor, destination_owner, owner_type, purpose, *, pay_out=False):
    """Move this relation's held money to `destination_owner`, once.

    One body for the refund and the payout, because they differ only in where the
    money goes and are otherwise the same three steps under the same lock: read the
    hold, move it, forget it. Doing them in separate unlocked code was how the same
    hold could be settled twice.

    Everything happens inside one transaction with the relation row locked, so a
    concurrent caller waits and then finds `hold_reference` already cleared. The lock
    used to be taken outside any transaction, which PostgreSQL refuses outright — so
    in production every settlement raised and no trainer was ever paid.
    """
    from wallet.models import Transaction, move_funds_atomic
    from wallet.utils import get_escrow_wallet

    from .models import TrainerClientRelation

    if not relation.pk:
        return None
    try:
        with transaction.atomic():
            # The relation row only (see "Lock order" above). With a bare
            # `select_for_update()` the `select_related` join also took FOR UPDATE on
            # both user rows before the wallets, so routine assignment (settle) and
            # unassign (release) deadlocked with the same client's transfer or payout
            # exactly as approval did.
            locked = (TrainerClientRelation.objects
                      .select_for_update(of=('self',))
                      .select_related('trainer', 'client')
                      .get(pk=relation.pk))
            if not locked.hold_reference:
                return None
            held = Transaction.objects.filter(reference_id=locked.hold_reference).first()
            if held is None:
                locked.hold_reference = ''
                locked.save(update_fields=['hold_reference', 'updated_at'])
                return None
            from .models import CoachingPeriod
            period = (CoachingPeriod.objects.select_for_update()
                      .filter(hold_reference=locked.hold_reference).first())
            if Transaction.objects.filter(reverses=held).exists():
                # An administrator already put this money back through
                # /api/wallet/admin/reversal/ — the one recovery path support has for a
                # hold that got stuck — and the reference stayed on the row. Moving it
                # again pays a second time out of an escrow wallet that holds other
                # clients' money: measured, one 80.00 hold reversed and then released by
                # unassign left the client with 180.00 of a 100.00 wallet. The hold is
                # settled, so the row forgets it.
                logger.warning("Hold %s was already reversed; not moving it for %s",
                               locked.hold_reference, purpose)
                locked.hold_reference = ''
                locked.save(update_fields=['hold_reference', 'updated_at'])
                relation.hold_reference = ''
                if period is not None and period.status == 'held':
                    period.status = 'refunded'
                    period.save(update_fields=['status', 'updated_at'])
                return None

            if pay_out:
                # The first month is delivered: pay the trainer their share and the
                # platform its commission, and start the month (users/coaching.py).
                from . import coaching
                if period is None:
                    # A hold taken before months existed.
                    period = coaching.open_period(locked, held)
                tx = coaching.pay_out(period, actor_id=getattr(actor, 'id', None),
                                      starts_at=timezone.now(), trainer_purpose=purpose)
                locked.hold_reference = ''
                locked.save(update_fields=['hold_reference', 'updated_at'])
                relation.hold_reference = ''
                return tx

            # The escrow that actually holds THIS money: one ledger wallet per
            # currency since SIDE_BUGS S-001, resolved by the currency of the hold row
            # rather than by a default that used to be whichever came first.
            escrow = get_escrow_wallet(held.currency)
            if escrow.balance < held.amount:
                # Do NOT clear the reference: the money is still owed, and the row is
                # the only record of who it belongs to.
                logger.error("Escrow cannot cover hold %s (%s)", locked.hold_reference, purpose)
                raise HoldFailed("escrow cannot cover this hold")

            # Back to the client, in the currency they were charged in. Their wallet
            # holds it by construction — it is where the hold came from.
            tx = move_funds_atomic(
                escrow, _wallet_for(destination_owner(locked), owner_type, held.currency),
                held.amount, currency=held.currency,
                actor_id=getattr(actor, 'id', None), tx_type='transfer',
                money_type=MoneyType.COACHING_1TO1,
                metadata={'purpose': purpose,
                          'hold_reference': locked.hold_reference,
                          'trainer_id': locked.trainer_id,
                          'client_id': locked.client_id})
            locked.hold_reference = ''
            locked.save(update_fields=['hold_reference', 'updated_at'])
            relation.hold_reference = ''
            if period is not None:
                period.status = 'refunded'
                period.save(update_fields=['status', 'updated_at'])
            return tx
    except HoldFailed:
        raise
    except Exception:
        logger.exception("Could not %s hold on relation %s", purpose, relation.pk)
        raise HoldFailed(f"could not {purpose}")


def release_hold(relation, actor):
    """Give the held money back to the client and forget the hold.

    Raises `HoldFailed` if it cannot. The caller decides what to do about it — the
    one thing it must not do is delete the relation anyway, because that row is the
    only record of whose money is sitting in escrow.
    """
    return _move_hold(relation, actor, lambda r: r.client, "client",
                      'trainer_hold_release')


def settle_hold(relation, actor):
    """Pay out the money held for THIS client, once, and forget the hold.

    The first program has arrived, so month 1 of the coaching starts: the trainer gets
    the price less the platform's commission and the platform gets the commission, at
    the rate copied onto the month when it was held (`users/coaching.py`). Returns the
    trainer's ledger row.

    The routine app used to move `trainer_hourly_rate` from the shared escrow wallet
    to the trainer on every routine assignment, with no link to any hold: fifty
    assignments were fifty payouts, drawn from whatever other clients had deposited.
    """
    return _move_hold(relation, actor, lambda r: r.trainer, "trainer",
                      'trainer_assignment_settlement', pay_out=True)


def release_holds_before_erasure(user, relations, *, dry_run=False) -> dict:
    """Give back every hold on `relations` before an erasure deletes them, and stop
    anyone still pointing at `user` as their trainer.

    Erasure deleted these rows with a bulk `qs.delete()`, and `hold_reference` is the
    only record of whose money is in escrow, so the money stayed there with nothing left
    that could return it. Nothing was delivered for a hold that was never settled — no
    routine was assigned — so it goes back to the client, whichever side is erasing.

    Also clears `assigned_trainer` on the clients of an erasing trainer. Only the erased
    account's own pointer was cleared, so a client of a retired trainer kept pointing at
    them; their unassign-trainer then found no approved relation, answered 200
    "Successfully unassigned" and refunded nothing.

    Raises `HoldFailed` if any hold cannot be returned, and `TrainerHasActiveClients` if
    `user` is still the trainer on an approved relation (the owner's rule); the caller
    must then erase nothing. A dry run never raises: it reports `active_clients`. Locks in the order at the top of this module: relation rows, user rows,
    then wallets — including the erased account's own row, FOR UPDATE, which the
    anonymisation that follows takes anyway when it rewrites `email` and `username`.
    Taken there instead — after the wallets — it deadlocked with any ledger write for
    the same client: measured, "deadlock detected ... while updating tuple in relation
    users_customuser" on a refund racing that client's transfer.
    """
    from .models import CustomUser, TrainerClientRelation

    rows = TrainerClientRelation.objects.filter(pk__in=relations.values('pk'))
    if dry_run:
        return {'holds_released': rows.exclude(hold_reference='').count(),
                'active_clients': rows.filter(trainer=user, status='approved').count()}

    with transaction.atomic():
        # A concurrent approval either committed before this lock, and its hold is
        # seen and returned here, or waits behind it and then finds the row gone.
        list(rows.select_for_update().order_by('pk'))
        CustomUser.objects.select_for_update().get(pk=user.pk)
        others = list(CustomUser.objects.select_for_update(no_key=True)
                      .filter(assigned_trainer=user).exclude(pk=user.pk)
                      .order_by('pk'))
        for person in others:
            if person.assigned_trainer_id == user.pk:
                person.assigned_trainer = None
                person.save(update_fields=['assigned_trainer'])

        # Read the set again, now that no new row naming this person can commit: an
        # INSERT into the relation table checks its `trainer`/`client` foreign key with
        # FOR KEY SHARE on the row we now hold FOR UPDATE. Without this pass, a relation
        # created and approved between the first lock and this one would have been
        # deleted below with its hold still in escrow.
        locked = list(rows.select_for_update().order_by('pk'))
        # Checked here, under the locks: an approval that committed first is seen, and
        # one still waiting finds this account's row gone. Raising (not returning) rolls
        # back the pointers cleared above with everything else.
        coaching = sum(1 for r in locked if r.trainer_id == user.pk and r.status == 'approved')
        if coaching:
            raise TrainerHasActiveClients(coaching)

        released = 0
        for relation in locked:
            if relation.hold_reference and release_hold(relation, user) is not None:
                released += 1
    return {'holds_released': released}


def active_relation(trainer, client):
    """The approved relation for this pair, or None. Takes no lock; the money
    helpers lock the row inside their own transaction."""
    from .models import TrainerClientRelation
    return (TrainerClientRelation.objects
            .filter(trainer=trainer, client=client, status='approved')
            .first())
