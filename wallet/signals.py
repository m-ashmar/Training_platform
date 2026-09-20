import logging
from decimal import Decimal

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from subscription.services.payment_service import payment_completed

from .models import (Wallet, AgentProfile, Transaction, WalletAuditLog,
                     move_funds_atomic, ensure_agent_profile, wallet_for)
from .utils import get_platform_revenue_wallet


def _subscription_trainer_share() -> Decimal:
    """How much of a plan payment a trainer is owed. Zero (the owner, 2026-09-19)."""
    return Decimal(str(getattr(settings, "SUBSCRIPTION_TRAINER_SHARE", "0")))

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
    """Record where a completed plan payment's money went: into the platform's revenue,
    and to the trainer only for the share the owner has priced.

    THE MONEY. A plan payment stays 100% with the platform, whether or not the client has
    a trainer (the owner, 2026-09-19; plan step 4). This used to move the WHOLE amount to
    whoever coached the client — no rate, no escrow, no relation to any agreed price — so
    a client with a coach bought their plan for the platform and the platform earned
    nothing from it. `SUBSCRIPTION_TRAINER_SHARE` is that share, and it is zero: the
    forward is priced at nothing rather than deleted, so the owner can raise it later.

    THE LEGS. Two, always, whether or not there is a trainer: a top-up that mints the
    payment into the client's wallet (the money entered the system there) and a transfer
    out of it — to the platform's revenue wallet, less the trainer's share when there is
    one. Crediting the client and stopping would hand them the plan's price as spendable
    balance; writing nothing at all, which is what a client with no trainer used to get,
    leaves the platform's largest revenue line invisible in its own ledger.

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
            _mark_payout_settled(payment)
            return

        # The trainer this client actually has a relationship with, not the pointer.
        # Reading the pointer meant a subscription payment forwarded the FULL price to
        # whoever the pointer named, outside the hold system entirely: no agreed rate, no
        # escrow, no idempotency against the relation. `current_trainer` reads the approved
        # relation, which is the platform's single source of truth for the bond. It is
        # asked for at all only while a trainer is owed a share; at zero, the coach of a
        # client who buys a plan is not part of the transaction.
        share_rate = _subscription_trainer_share()
        trainer = client.current_trainer if share_rate > 0 else None

        # In the payment's OWN currency. `get_or_create` with only `owner_type` left the
        # wallet on its hardcoded "USD" default whatever the plan was priced in.
        client_wallet = wallet_for(client, "client", payment.currency)
        platform_wallet = get_platform_revenue_wallet(payment.currency)
        trainer_wallet = wallet_for(trainer, "trainer", payment.currency) if trainer else None
        checked = [(client_wallet, "client"), (platform_wallet, "platform")]
        if trainer_wallet is not None:
            checked.append((trainer_wallet, "trainer"))
        for w, who in checked:
            if w.currency != payment.currency:
                # A wallet that already holds money in another currency. There is no FX
                # here, so this is refused loudly rather than converted at 1:1 — and
                # recorded as resolved, because retrying cannot change the answer; the
                # error log is what puts it in front of a person.
                logger.error(
                    "payout skipped for payment %s: the %s's wallet holds %s and the "
                    "payment is %s", payment.pk, who, w.currency, payment.currency)
                _mark_payout_settled(payment)
                return

        # BOTH wallets, in id order, before either leg moves. The top-up leg locked the
        # client's wallet alone and the transfer leg then asked for both sorted by id —
        # client first, trainer second whenever the trainer signed up first, which is
        # the ordinary case. A client's own transfer to their trainer at the same moment
        # (`move_funds_atomic` sorts too) held the trainer's wallet and waited for the
        # client's: a real Postgres deadlock cycle. When the payout was the victim it
        # rolled back after the payment had committed, and nothing ever paid it again —
        # 13 lost payouts in 400 runs. `move_funds_atomic` re-locks these rows inside
        # this transaction, which costs nothing once they are held.
        held = {client_wallet.pk, platform_wallet.pk}
        if trainer_wallet is not None:
            held.add(trainer_wallet.pk)
        list(Wallet.objects.select_for_update().filter(pk__in=held).order_by("pk"))

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
                money_type=payment.money_type,
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

        # Out of the client's wallet again: the trainer's priced share, then everything
        # left to the platform. `split` is the same arithmetic the coaching commission
        # uses, so the two never disagree about a rounded half-unit.
        from users.coaching import split

        common = {
            "original_payment_id": str(payment.id),
            "subscription_id": str(subscription.id),
            "plan_id": subscription.plan_id,
        }
        for_platform, for_trainer = split(payment.amount, Decimal("1") - share_rate)
        if trainer_wallet is not None and for_trainer > 0:
            move_funds_atomic(
                client_wallet, trainer_wallet, for_trainer,
                actor_id=client.id, currency=payment.currency, tx_type="transfer",
                money_type=payment.money_type,
                metadata={"purpose": "subscription_trainer_share",
                          "rate": str(share_rate), **common},
            )
        if for_platform > 0:
            move_funds_atomic(
                client_wallet, platform_wallet, for_platform,
                actor_id=client.id, currency=payment.currency, tx_type="transfer",
                money_type=payment.money_type,
                metadata={"purpose": "subscription_revenue", **common},
            )
        _mark_payout_settled(payment)


def _mark_payout_settled(payment) -> None:
    """Record, in the payout's own transaction, that nothing more is owed for `payment`.

    Until this is written a completed payment's payout is OWED, and every retry path
    (`PaymentService.redeliver_payout`) and the sweep (`subscription.settle_owed_payouts`)
    sends it again. Written with `update()` so the row's `updated_at` — which the sweep's
    grace period reads — is left alone.
    """
    type(payment).objects.filter(pk=payment.pk, payout_settled_at__isnull=True).update(
        payout_settled_at=timezone.now())


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
                money_type=leg.money_type,
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


