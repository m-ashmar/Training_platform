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




# --------------------------------------------------------------------- account number
#: The shape the TopUp screen shows and the client reads out loud to an agent:
#: "YG-204-781" (`screens/07-money/TopUp.png`).
ACCOUNT_NUMBER_PREFIX = "YG"


def account_number_for(user) -> str:
    """The client's own account number, as the agent types it into their app.

    Derived from the user id rather than stored, so it exists for every account that
    has ever existed, cannot drift from the row it names, and needs no migration and
    no uniqueness constraint of its own. It is an ADDRESS, not a secret — the same
    kind of thing as an IBAN: knowing it lets someone PUT money in, never take any
    out, and the top-up itself is still signed by the agent's key and capped by their
    limits.

    Grouped in threes from the right and padded to six digits, so the shortest id is
    "YG-000-007" and an id past a million simply grows the left group.
    """
    n = int(getattr(user, "id", user))
    digits = f"{n:06d}"
    head, tail = digits[:-3], digits[-3:]
    return f"{ACCOUNT_NUMBER_PREFIX}-{head}-{tail}"


def user_id_from_account_number(value: str) -> int | None:
    """The user id inside an account number, or None when it is not one.

    Accepts the number as the screen shows it and as anyone would retype it: any
    case, spaces or dashes anywhere, prefix optional. Returns None rather than
    guessing, so a caller can fall through to its other ways of naming a client.
    """
    if not value:
        return None
    text = str(value).strip().upper().replace(" ", "").replace("-", "")
    if text.startswith(ACCOUNT_NUMBER_PREFIX):
        text = text[len(ACCOUNT_NUMBER_PREFIX):]
    elif not text.isdigit():
        return None
    if not text.isdigit() or not text:
        return None
    return int(text)
