from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from django.db import transaction
from .models import Wallet, AgentProfile, Transaction, move_funds_atomic


User = get_user_model()


@receiver(post_save, sender=User)
def create_wallet_and_agent(sender, instance, created, **kwargs):
    if created:
        # Create wallet for clients and trainers automatically
        if getattr(instance, "user_type", None) in ("client", "trainer"):
            Wallet.objects.get_or_create(owner=instance, defaults={"owner_type": instance.user_type})
        # Prepare agent profile placeholder
        if getattr(instance, "user_type", None) == "agent":
            AgentProfile.objects.get_or_create(user=instance)


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
    trainer = getattr(client, "assigned_trainer", None)
    if not trainer:
        return

    # Idempotency: check if transfer for this payment already exists
    exists = Transaction.objects.filter(
        tx_type="transfer",
        metadata__original_payment_id=str(payment.id),
    ).exists()
    if exists:
        return

    client_wallet, _ = Wallet.objects.get_or_create(owner=client, defaults={"owner_type": "client"})
    trainer_wallet, _ = Wallet.objects.get_or_create(owner=trainer, defaults={"owner_type": "trainer"})

    # Check if payment is external (not internal manual wallet op) and ensure Client has funds
    # "manual" might be used for internal admins, but 'stripe', etc. are definitely external.
    # We'll treat anything not explicitly an internal wallet-only op as external funding source.
    # Currently assuming all Payment objects come from external gateways except potentially 'manual'.
    # Even if 'manual', if they paid cash, we need to credit their digital wallet first? 
    # Yes, for the system to be consistent, if Payment exists, Money entered the system.
    # So we should ALWAYS TopUp the client wallet effectively representing that cashflow.
    
    # 1. Top Up Client Wallet (System -> Client)
    # Ensure idempotency for this specific step too
    topup_exists = Transaction.objects.filter(
        tx_type="topup",
        metadata__original_payment_id=str(payment.id),
    ).exists()

    with transaction.atomic():
        if not topup_exists:
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

        # 2. Transfer to Trainer (Client -> Trainer)
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


