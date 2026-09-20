from django.contrib.auth import get_user_model
from .models import Wallet
from django.conf import settings


def _ledger_owner_wallet(email, username, currency=None):
    """The wallet of a platform ledger owner: a row that holds money, not a person.

    Not a superuser and not an admin `user_type` (see `get_escrow_wallet`), disabled, no
    usable password.

    `currency` goes through `wallet_for`, so the rule is the one every other wallet
    follows: a wallet that has never held money takes the currency of the money that
    first funds it, and one that has keeps its own — the caller refuses a mismatch
    rather than converting at 1:1.
    """
    from .models import wallet_for

    User = get_user_model()
    user, created = User.objects.get_or_create(
        email=email,
        defaults={'username': username, 'user_type': 'agent', 'is_staff': False,
                  'is_superuser': False, 'is_active': False, 'phone_number': '0000000000'})
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])
    return wallet_for(user, 'agent', currency)


def get_platform_revenue_wallet(currency=None):
    """Where the platform's money lands: the commission on coaching
    (`users/coaching.py`) and the whole of every plan payment
    (`wallet/signals.py:handle_payment_completed`).

    A wallet like escrow's until plan step 4 replaces both with named ledger accounts.
    """
    return _ledger_owner_wallet(
        getattr(settings, 'PLATFORM_REVENUE_EMAIL', 'platform_revenue@local'),
        getattr(settings, 'PLATFORM_REVENUE_USERNAME', 'platform_revenue'),
        currency)


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


