from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from django.db import transaction
from .models import Wallet, AgentProfile, Transaction, move_funds_atomic, ensure_agent_profile


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


@receiver(post_save)
def handle_payment_completed(sender, instance, created, **kwargs):
    """
    On Payment completion, transfer funds from client wallet to trainer wallet
    if the client has an assigned trainer. Idempotent via Transaction metadata.
    """
    try:
        from subscription.models import Payment
    except Exception:
        return

    if sender is not Payment:
        return

    payment = instance
    if payment.status != "completed":
        return

    subscription = payment.subscription
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
        type(payment).objects.select_for_update().filter(pk=payment.pk).first()
        if Transaction.objects.filter(
                tx_type="transfer",
                metadata__original_payment_id=str(payment.id)).exists():
            return

        client_wallet, _c = Wallet.objects.get_or_create(
            owner=client, defaults={"owner_type": "client"})
        trainer_wallet, _t = Wallet.objects.get_or_create(
            owner=trainer, defaults={"owner_type": "trainer"})

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
            tx_type="transfer",
            metadata={
                "original_payment_id": str(payment.id),
                "subscription_id": str(subscription.id),
                "plan_id": subscription.plan_id,
            },
        )


