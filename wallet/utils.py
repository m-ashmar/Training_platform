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

    # NOT a superuser, and not an admin `user_type`. This row is a ledger owner, not
    # a person: it was created with platform-wide privileges, a predictable address
    # and no password, and `user_type='admin'` alone makes `can_access_user_data`
    # return True for every user on the platform.
    user, created = User.objects.get_or_create(
        email=email,
        defaults={
            'username': username,
            'user_type': 'agent',
            'is_staff': False,
            'is_superuser': False,
            'is_active': False,
            'phone_number': '0000000000',
        }
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    wallet, _ = Wallet.objects.get_or_create(owner=user, defaults={'owner_type': 'agent'})
    return wallet


