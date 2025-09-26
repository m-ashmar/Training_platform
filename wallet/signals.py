from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
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


