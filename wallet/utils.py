from django.contrib.auth import get_user_model
from .models import Wallet
from django.conf import settings


def get_escrow_wallet():
    """
    Returns the platform escrow wallet used to hold funds between approval and assignment.
    Creates an admin user for escrow if necessary.
    """
    User = get_user_model()
    email = getattr(settings, 'PLATFORM_ESCROW_EMAIL', 'platform_escrow@local')
    username = getattr(settings, 'PLATFORM_ESCROW_USERNAME', 'platform_escrow')

    user, created = User.objects.get_or_create(
        email=email,
        defaults={
            'username': username,
            'user_type': 'admin',
            'is_staff': True,
            'is_superuser': True,
            'phone_number': '0000000000',
        }
    )
    wallet, _ = Wallet.objects.get_or_create(owner=user, defaults={'owner_type': 'agent'})
    return wallet


