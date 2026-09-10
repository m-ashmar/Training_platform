import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from django.db import transaction

from subscription.services.payment_service import payment_completed

from .models import (Wallet, AgentProfile, Transaction, WalletAuditLog,
                     move_funds_atomic, ensure_agent_profile, wallet_for)

# The one branch written to make an un-clawed-back refund visible used to raise
# NameError instead, which rolled the whole refund back: the payment stayed
# completed, the subscription stayed active, and the gateway retried into the same
# crash forever.
logger = logging.getLogger(__name__)


User = get_user_model()


@receiver(post_save, sender=User)
def create_wallet_and_agent(sender, instance, created, **kwargs):
    if created:
        # Create wallet for clients and trainers automatically
        if getattr(instance, "user_type", None) in ("client", "trainer"):
            Wallet.objects.get_or_create(owner=instance, defaults={"owner_type": instance.user_type})
        # Prepare agent profile with fail-closed default caps
        if getattr(instance, "user_type", None) == "agent":
            ensure_agent_profile(instance)


@receiver(payment_completed)
def handle_payment_completed(sender, payment, subscription=None, **kwargs):
    """
    On Payment completion, transfer funds from client wallet to trainer wallet
    if the client has an assigned trainer. Idempotent via Transaction metadata.

    Listens to `payment_completed`, the signal `PaymentService` was written to emit and
    which had no receivers at all. It used to be a `post_save` with NO sender, so it ran
    an import and an identity check on every row saved anywhere on the platform — and,
    because `post_delete.has_listeners()` is then true for every model, it also switched
    off Django's fast-delete path everywhere. Both problems go away by listening to the
    signal that means the thing.
    """
    if payment is None:
        return

    subscription = subscription or payment.subscription
    client = subscription.user
    # The trainer this client actually has a relationship with, not the pointer.
    # Reading the pointer meant a subscription payment forwarded the FULL price to
    # whoever the pointer named, outside the hold system entirely: no agreed rate, no
    # escrow, no idempotency against the relation. `current_trainer` reads the approved
    # relation, which is the platform's single source of truth for the bond.
    trainer = client.current_trainer
    if not trainer:
        return

    # ONE transaction, with the payment row locked. The idempotency checks were reads
    # outside any transaction followed by writes, so two gateway callbacks for one
    # payment both passed them and the trainer was paid twice — the top-up too, so the
    # platform minted the money for it. The second callback now waits on the lock and
    # then finds the transfer already recorded.
    with transaction.atomic():
        # Re-read the payment UNDER the lock, and decide on that row — not on the
        # in-memory object the caller captured. `complete_payment` defers this to
        # `transaction.on_commit`, so between the COMMIT and this receiver running there
        # is a real window in which a refund webhook can land and commit. The status
        # test used to run on the stale object, so the payout went ahead on an
        # already-refunded payment: `unwind_payment` had found no legs to reverse
        # (they did not exist yet), so nothing was reversed and nothing was recorded —
        # the customer's money went back to their card, access was cancelled, and the
        # trainer kept a full payout minted from nothing. Reproduced on the first
        # attempt with two threads on a barrier.
        locked = type(payment).objects.select_for_update().filter(pk=payment.pk).first()
        if locked is None or locked.status != "completed":
            logger.info(
                "payout skipped for payment %s: status is %s at payout time",
                payment.pk, getattr(locked, "status", "gone"))
            return
        payment = locked
        if Transaction.objects.filter(
                tx_type="transfer",
                metadata__original_payment_id=str(payment.id)).exists():
            return

        # In the payment's OWN currency. `get_or_create` with only `owner_type` left the
        # wallet on its hardcoded "USD" default whatever the plan was priced in.
        client_wallet = wallet_for(client, "client", payment.currency)
        trainer_wallet = wallet_for(trainer, "trainer", payment.currency)
        for w, who in ((client_wallet, "client"), (trainer_wallet, "trainer")):
            if w.currency != payment.currency:
                # A wallet that already holds money in another currency. There is no FX
                # here, so this is refused loudly rather than converted at 1:1.
                logger.error(
                    "payout skipped for payment %s: the %s's wallet holds %s and the "
                    "payment is %s", payment.pk, who, w.currency, payment.currency)
                return

        # A Payment means money entered the system, so the client's wallet is credited
        # first and the trainer is paid out of it.
        if not Transaction.objects.filter(
                tx_type="topup",
                metadata__original_payment_id=str(payment.id)).exists():
            move_funds_atomic(
                None,  # Source=None means TopUp from System
                client_wallet,
                payment.amount,
                actor_id=client.id,
                tx_type="topup",
                # The payment's OWN currency, checked against the wallet. Without it the
                # mint leg was the one path with no currency check anywhere, and a SYP
                # payment credited a USD-labelled wallet at 1:1.
                currency=payment.currency,
                metadata={
                    "original_payment_id": str(payment.id),
                    "subscription_id": str(subscription.id),
                    "note": f"Automatic top-up from payment {payment.id}",
                },
            )

        move_funds_atomic(
            client_wallet,
            trainer_wallet,
            payment.amount,
            actor_id=client.id,
            currency=payment.currency,
            tx_type="transfer",
            metadata={
                "original_payment_id": str(payment.id),
                "subscription_id": str(subscription.id),
                "plan_id": subscription.plan_id,
            },
        )


def unwind_payment(payment) -> int:
    """Take the ledger back to where it was before this payment completed.

    `refund_payment` withdrew the ACCESS a payment bought and wrote nothing to the wallet
    ledger. So a gateway refund or a chargeback — both routed there by
    `PaymentWebhookView` — cancelled the subscription, returned the money to the card, and
    left the minted top-up in the client's wallet and the full amount in the trainer's.
    Every refund was a net loss of the amount to the platform and an un-clawed-back
    payout to the trainer.

    Reverses each leg through `move_funds_atomic`, so the money moves under the same
    wallet locks as the original and the reversal is recorded as a reversal. Idempotent:
    a leg already reversed is skipped, and `Transaction.reverses` is a OneToOne, so a
    second attempt cannot double it even if two callbacks race.

    Returns how many legs were reversed.
    """
    from .models import AlreadyReversed

    reversed_legs = 0
    legs = Transaction.objects.filter(
        tx_type__in=("transfer", "topup"),
        metadata__original_payment_id=str(payment.id),
    ).order_by("-id")   # the transfer first, so the client's wallet can fund it
    for leg in legs:
        if Transaction.objects.filter(reverses=leg).exists():
            continue
        try:
            move_funds_atomic(
                leg.destination_wallet, leg.source_wallet, leg.amount,
                actor_id=leg.actor_id, tx_type="reversal",
                metadata={"original_reference": leg.reference_id,
                          "original_payment_id": str(payment.id),
                          "reason": "payment refunded"},
                reverses=leg)
            reversed_legs += 1
        except AlreadyReversed:
            continue
        except ValueError:
            # Insufficient balance: the client has already spent the credit. Record it
            # rather than leaving a silent hole — this is money someone has to chase.
            # A log line is not enough: it is not queryable, it is not retained with the
            # ledger, and it is the only trace that a refunded payment left value behind.
            logger.error(
                "could not reverse %s leg %s for refunded payment %s: the wallet no "
                "longer holds the funds", leg.tx_type, leg.reference_id, payment.id)
            WalletAuditLog.objects.create(
                event_type="refund_leg_unreversible",
                actor=None,
                path="wallet.signals.unwind_payment",
                payload={
                    "payment_id": str(payment.id),
                    "leg_id": leg.id,
                    "leg_type": leg.tx_type,
                    "reference_id": leg.reference_id,
                    "amount": str(leg.amount),
                    "source_wallet_id": leg.source_wallet_id,
                    "destination_wallet_id": leg.destination_wallet_id,
                    "reason": "destination wallet no longer holds the funds",
                },
            )
    return reversed_legs


