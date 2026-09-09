"""The lifecycle of a trainer/client relation and the money held against it.

Three events, one place. They were spread across `users/views.py` and
`routine/views.py`, connected by nothing but a string in transaction metadata, and the
consequences were: money taken with no way to give it back, money paid out of a shared
pool on an unrelated endpoint at whatever price the trainer currently advertised, and a
relation granted even when the payment for it had failed.

A hold has an identity (`TrainerClientRelation.hold_reference`) and a price agreed in
advance (`agreed_rate`). Every one of the three events below is idempotent against that
identity, so a repeat cannot take twice, refund twice, or pay twice.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction

logger = logging.getLogger(__name__)


class HoldFailed(Exception):
    """The client's charge could not be held, so the relation must not be granted."""


def _wallet_for(user, owner_type):
    from wallet.models import Wallet
    wallet, _created = Wallet.objects.get_or_create(
        owner=user, defaults={"owner_type": owner_type})
    return wallet


def price_of(relation) -> Decimal:
    """What this pair agreed to. Falls back to the trainer's rate only when the row
    predates `agreed_rate`, never to a rate raised after the client asked."""
    if relation.agreed_rate is not None:
        return Decimal(relation.agreed_rate)
    return Decimal(getattr(relation.trainer, 'trainer_hourly_rate', None) or 0)


def take_hold(relation, actor) -> None:
    """Move the agreed price from the client into escrow, once.

    Raises `HoldFailed` rather than logging and continuing. The caller grants a
    relationship on the strength of this, and granting it after the payment failed is
    how a trainer got a client for free and the client got charged nothing — while the
    routine app still paid that trainer out of everyone else's money.
    """
    from wallet.models import move_funds_atomic
    from wallet.utils import get_escrow_wallet

    if relation.hold_reference:
        return  # already held for this pair
    amount = price_of(relation)
    if amount <= 0:
        return

    client_wallet = _wallet_for(relation.client, "client")
    if client_wallet.balance < amount:
        raise HoldFailed("Insufficient client wallet balance for the trainer charge")
    try:
        tx = move_funds_atomic(
            client_wallet, get_escrow_wallet(), amount,
            actor_id=getattr(actor, 'id', None), tx_type='transfer',
            metadata={'purpose': 'trainer_request_hold',
                      'trainer_id': relation.trainer_id,
                      'client_id': relation.client_id,
                      'relation_id': relation.id})
    except Exception as exc:
        raise HoldFailed(str(exc)) from exc
    relation.hold_reference = tx.reference_id
    relation.save(update_fields=['hold_reference', 'updated_at'])


def _move_hold(relation, actor, destination_owner, owner_type, purpose):
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
            locked = (TrainerClientRelation.objects
                      .select_for_update()
                      .select_related('trainer', 'client')
                      .get(pk=relation.pk))
            if not locked.hold_reference:
                return None
            held = Transaction.objects.filter(reference_id=locked.hold_reference).first()
            if held is None:
                locked.hold_reference = ''
                locked.save(update_fields=['hold_reference', 'updated_at'])
                return None

            escrow = get_escrow_wallet()
            if escrow.balance < held.amount:
                # Do NOT clear the reference: the money is still owed, and the row is
                # the only record of who it belongs to.
                logger.error("Escrow cannot cover hold %s (%s)", locked.hold_reference, purpose)
                raise HoldFailed("escrow cannot cover this hold")

            tx = move_funds_atomic(
                escrow, _wallet_for(destination_owner(locked), owner_type), held.amount,
                actor_id=getattr(actor, 'id', None), tx_type='transfer',
                metadata={'purpose': purpose,
                          'hold_reference': locked.hold_reference,
                          'trainer_id': locked.trainer_id,
                          'client_id': locked.client_id})
            locked.hold_reference = ''
            locked.save(update_fields=['hold_reference', 'updated_at'])
            relation.hold_reference = ''
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
    """Pay the trainer the money held for THIS client, once, and forget the hold.

    The routine app used to move `trainer_hourly_rate` from the shared escrow wallet
    to the trainer on every routine assignment, with no link to any hold: fifty
    assignments were fifty payouts, drawn from whatever other clients had deposited.
    """
    return _move_hold(relation, actor, lambda r: r.trainer, "trainer",
                      'trainer_assignment_settlement')


def active_relation(trainer, client):
    """The approved relation for this pair, or None. Takes no lock; the money
    helpers lock the row inside their own transaction."""
    from .models import TrainerClientRelation
    return (TrainerClientRelation.objects
            .filter(trainer=trainer, client=client, status='approved')
            .first())
