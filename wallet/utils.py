from django.conf import settings
from django.contrib.auth import get_user_model

from .currencies import CURRENCY_CODES, is_supported
from .models import Wallet


class UnsupportedCurrency(ValueError):
    """A ledger wallet was asked for in a currency the platform does not trade in."""


def _ledger_address(base_email: str, base_username: str, currency: str) -> tuple[str, str]:
    """The address of the ledger owner that holds `currency`.

    `('platform_revenue@local', 'platform_revenue', 'SYP')` ->
    `('platform_revenue_syp@local', 'platform_revenue_syp')`.

    The settings are the BASE of the address, not the address: one row per currency, and
    the currency is IN the name, so an operator reading `users_customuser` can see which
    books a balance belongs to without joining anything.
    """
    local, at, domain = base_email.partition("@")
    suffix = currency.lower()
    email = f"{local}_{suffix}{at}{domain}" if at else f"{base_email}_{suffix}"
    return email, f"{base_username}_{suffix}"


def _ledger_owner_wallet(base_email, base_username, currency):
    """The platform's own wallet for `currency`: a row that holds money, not a person.

    WHY ONE OWNER PER CURRENCY, and not one wallet with two balances.

    `Wallet` is one row per owner and a wallet's currency is a fact once money has
    touched it (`wallet.models.wallet_for`). For a PERSON that is right, and it is the
    product rule: they pick SYP or USD and are held to it (the owner, 2026-09-21), and
    a price in the other currency is CONVERTED into theirs at purchase rather than
    landing in their wallet in the wrong unit. For the platform's own ledger wallets it
    was wrong, because they are shared by everybody: `get_platform_revenue_wallet()` returned ONE wallet, so
    the first payment ever taken decided the platform's currency for ever, and a plan
    paid in the other one was minted into the customer's wallet, logged as an error and
    left there (SIDE_BUGS S-001). Escrow had the same shape, which meant a coaching month
    in the second currency could not even be HELD: `take_hold` moved client -> escrow,
    `move_funds_atomic` saw two currencies and raised, and the trainer request came back
    a 402.

    Of the three ways out, this is the smallest:

    | Shape | What it costs |
    |---|---|
    | A ledger OWNER per currency (this) | two disabled user rows per ledger account; `Wallet`'s one-per-owner rule untouched, so no person's wallet changes |
    | Drop `Wallet`'s `OneToOneField` for `unique(owner, currency)` | every wallet in the system gains a second identity column; every `Wallet.objects.get(owner=...)` in the codebase becomes ambiguous, including the ones that read a CUSTOMER's balance |
    | One wallet, many balances | a balance table, and every `move_funds_atomic` lock rewritten |

    The currency is PINNED here, deliberately unlike `wallet_for`: a person's untouched
    wallet may take the currency of the money that first funds it, but a ledger wallet's
    currency is decided by which address it is, so a row that disagrees is corruption and
    is raised on rather than quietly used.
    """
    if not is_supported(currency):
        raise UnsupportedCurrency(
            f"{currency!r} is not one of {CURRENCY_CODES}, so the platform has no "
            f"ledger wallet for it")

    email, username = _ledger_address(base_email, base_username, currency)
    user = _ledger_owner(email, username)

    wallet, _created = Wallet.objects.get_or_create(
        owner=user, defaults={'owner_type': 'agent', 'currency': currency})
    if wallet.currency != currency:
        raise UnsupportedCurrency(
            f"the ledger wallet at {email} holds {wallet.currency}, not {currency}")
    return wallet


def _ledger_owner(email, username):
    """The disabled user row that owns a ledger wallet, found by its ADDRESS.

    The address is what every lookup uses (`ledger_owner_emails`, and this function);
    the username is only a label. It used to be `get_or_create(email=...,
    defaults={'username': ...})`, and registration accepted any username, so a person
    who signed up as `platform_escrow_usd` made that create raise IntegrityError: every
    coaching hold in dollars failed and every dollar plan payout stayed owed for ever
    (SIDE_BUGS S-086). Registration now refuses the prefix (`is_reserved_username`);
    this is the backstop for a name taken before that, or by an operator by hand: the
    row is created under a suffixed username rather than not at all.
    """
    import uuid

    from django.db import IntegrityError, transaction

    User = get_user_model()
    existing = User.objects.filter(email=email).first()
    if existing is not None:
        return existing
    for attempt in range(3):
        name = username if attempt == 0 else f"{username}_{uuid.uuid4().hex[:8]}"
        if User.objects.filter(username=name).exists():
            continue
        try:
            with transaction.atomic():
                user = User.objects.create(
                    email=email, username=name, user_type='agent', is_staff=False,
                    is_superuser=False, is_active=False, phone_number='0000000000')
                user.set_unusable_password()
                user.save(update_fields=['password'])
                return user
        except IntegrityError:
            # A concurrent first use created the same address: that row is the one.
            existing = User.objects.filter(email=email).first()
            if existing is not None:
                return existing
    raise UnsupportedCurrency(f"could not create the ledger owner {email}")


def reserved_username_prefixes() -> tuple[str, ...]:
    """The username bases of the platform's ledger owners, lower-cased."""
    return tuple(base.lower() for base in (
        getattr(settings, 'PLATFORM_ESCROW_USERNAME', 'platform_escrow'),
        getattr(settings, 'PLATFORM_REVENUE_USERNAME', 'platform_revenue')))


def is_reserved_username(username) -> bool:
    """True for a name a person must not register: the ledger owners' own, in any
    currency and any case (`platform_escrow_usd`, `Platform_Revenue_SYP`, ...)."""
    name = (username or '').strip().lower()
    return any(name == base or name.startswith(f"{base}_")
               for base in reserved_username_prefixes())


def get_platform_revenue_wallet(currency):
    """Where the platform's money in `currency` lands: the commission on coaching
    (`users/coaching.py`) and the whole of every plan payment
    (`wallet/signals.py:handle_payment_completed`).

    `currency` is REQUIRED. It used to default, and a default is how revenue in the
    second currency ended up sitting in customers' balances: the caller always knows
    which money it is moving — the payment's, the coaching month's, the client wallet's
    — and a caller that does not is a bug to find, not a default to invent.
    """
    return _ledger_owner_wallet(
        getattr(settings, 'PLATFORM_REVENUE_EMAIL', 'platform_revenue@local'),
        getattr(settings, 'PLATFORM_REVENUE_USERNAME', 'platform_revenue'),
        currency)


def get_escrow_wallet(currency):
    """The platform's escrow wallet for `currency`: coaching money between the hold and
    the payout (`users/trainer_relations.py`, `users/coaching.py`).

    One per currency, for the reason in `_ledger_owner_wallet`. `currency` is REQUIRED:
    it is the currency of the client's wallet the month is charged from, or of the hold
    row being released.

    NOT a superuser, and not an admin `user_type`. This row is a ledger owner, not a
    person: it was created with platform-wide privileges, a predictable address and no
    password, and `user_type='admin'` alone makes `can_access_user_data` return True for
    every user on the platform.
    """
    return _ledger_owner_wallet(
        getattr(settings, 'PLATFORM_ESCROW_EMAIL', 'platform_escrow@local'),
        getattr(settings, 'PLATFORM_ESCROW_USERNAME', 'platform_escrow'),
        currency)


def ledger_owner_emails() -> list[str]:
    """Every address a ledger owner can have: escrow and revenue, in every currency.

    `wallet.activity` hides these from a client's ledger screen, so the list has to grow
    with the currency list — reading only the two BASE addresses from settings stopped
    matching anything the moment the owners became per-currency, and the client would
    have seen "platform_escrow_syp" named as the person they paid.
    """
    bases = [(getattr(settings, 'PLATFORM_ESCROW_EMAIL', 'platform_escrow@local'),
              getattr(settings, 'PLATFORM_ESCROW_USERNAME', 'platform_escrow')),
             (getattr(settings, 'PLATFORM_REVENUE_EMAIL', 'platform_revenue@local'),
              getattr(settings, 'PLATFORM_REVENUE_USERNAME', 'platform_revenue'))]
    return [_ledger_address(email, username, code)[0]
            for email, username in bases for code in CURRENCY_CODES]


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
    # ASCII digits only: "²" passes `isdigit()` and `int()` refuses it (a 500).
    if not text or not text.isascii() or not text.isdigit():
        return None
    return int(text)
