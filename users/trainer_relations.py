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


def release_hold(relation, actor):
    """Give the held money back to the client and forget the hold.

    Called when the relationship ends. Never raises: a refund that fails must not trap
    a client in a relationship they are trying to leave, and the hold stays recorded.
    """
    from wallet.models import Transaction, move_funds_atomic
    from wallet.utils import get_escrow_wallet

    if not relation.hold_reference:
        return None
    try:
        held = Transaction.objects.filter(reference_id=relation.hold_reference).first()
        if held is None:
            relation.hold_reference = ''
            relation.save(update_fields=['hold_reference', 'updated_at'])
            return None
        escrow = get_escrow_wallet()
        if escrow.balance < held.amount:
            logger.error("Escrow cannot cover hold %s", relation.hold_reference)
            return None
        tx = move_funds_atomic(
            escrow, _wallet_for(relation.client, "client"), held.amount,
            actor_id=getattr(actor, 'id', None), tx_type='transfer',
            metadata={'purpose': 'trainer_hold_release',
                      'hold_reference': relation.hold_reference,
                      'trainer_id': relation.trainer_id,
                      'client_id': relation.client_id})
        relation.hold_reference = ''
        relation.save(update_fields=['hold_reference', 'updated_at'])
        return tx
    except Exception:
        logger.exception("Could not release hold %s", relation.hold_reference)
        return None


def settle_hold(relation, actor):
    """Pay the trainer the money held for THIS client, once, and forget the hold.

    The routine app used to move `trainer_hourly_rate` from the shared escrow wallet
    to the trainer on every routine assignment, with no link to any hold: fifty
    assignments were fifty payouts, drawn from whatever other clients had deposited,
    and a trainer who set a rate after approval collected against a hold that never
    existed.
    """
    from wallet.models import Transaction, move_funds_atomic
    from wallet.utils import get_escrow_wallet

    if not relation.hold_reference:
        return None
    try:
        held = Transaction.objects.filter(reference_id=relation.hold_reference).first()
        if held is None:
            relation.hold_reference = ''
            relation.save(update_fields=['hold_reference', 'updated_at'])
            return None
        escrow = get_escrow_wallet()
        if escrow.balance < held.amount:
            logger.error("Escrow cannot cover hold %s", relation.hold_reference)
            return None
        tx = move_funds_atomic(
            escrow, _wallet_for(relation.trainer, "trainer"), held.amount,
            actor_id=getattr(actor, 'id', None), tx_type='transfer',
            metadata={'purpose': 'trainer_assignment_settlement',
                      'hold_reference': relation.hold_reference,
                      'trainer_id': relation.trainer_id,
                      'client_id': relation.client_id})
        relation.hold_reference = ''
        relation.save(update_fields=['hold_reference', 'updated_at'])
        return tx
    except Exception:
        logger.exception("Could not settle hold %s", relation.hold_reference)
        return None


def active_relation(trainer, client):
    """The approved relation for this pair, locked for update, or None."""
    from .models import TrainerClientRelation
    return (TrainerClientRelation.objects
            .select_for_update()
            .filter(trainer=trainer, client=client, status='approved')
            .first())
