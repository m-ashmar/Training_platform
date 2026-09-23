from django.db import models, transaction, connection
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
from django.core.validators import MinValueValidator
import uuid
from decimal import Decimal

from policy.money import MoneyType

from .currencies import SUPPORTED_CURRENCIES


def generate_reference_id():
    return str(uuid.uuid4())


class AgentProfile(models.Model):
    AGENT_STATUS_CHOICES = [
        ("active", "Active"),
        ("suspended", "Suspended"),
        ("banned", "Banned"),
    ]
    AGENT_WALLET_TYPES = [
        ("prepaid", "Prepaid"),
        ("postpaid", "Postpaid"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="agent_profile")
    wallet_type = models.CharField(max_length=16, choices=AGENT_WALLET_TYPES, default="prepaid")
    #: The caps on an agent's top-ups, ONE PAIR PER CURRENCY (`limits_for`). These two
    #: predate the second currency and have always capped Syrian pounds: every wallet
    #: they were minted into was SYP. `0` means "no top-ups in this currency", never
    #: "unlimited" — the caps fail closed (`wallet.views._topup_limit_error`).
    daily_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    monthly_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    #: The same caps in US dollars. They were one number for both currencies, so a cap
    #: an operator set in pounds allowed the same NUMBER of dollars — about 13,000 times
    #: the value (SIDE_BUGS S-059). Zero until an operator sets them, so an agent tops up
    #: no dollars until someone has decided how many.
    daily_limit_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    monthly_limit_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    status = models.CharField(max_length=16, choices=AGENT_STATUS_CHOICES, default="active")
    ip_allowlist = models.JSONField(default=list, blank=True)

    # --- The public listing: "وكلاء قريبون" on the TopUp screen -----------------
    # A client with no balance has to find a human being holding cash, so the app
    # shows a list of agents with the shop's name, the district and the opening hours,
    # and a button to call. None of it is inferred from the account: an agent appears
    # only when an operator has filled this in AND set `is_listed`, so an agent who
    # has not agreed to be published is not published.
    #
    # `public_phone` is DELIBERATELY not `CustomUser.phone_number`. That number was
    # given to register an account, not to be handed to every client in the country;
    # this one is the shop line the agent asked us to publish. Personal data either
    # way — `wallet.AgentProfile` is in the privacy registry with `fields=None`, so
    # these travel with the agent's own export and are kept, like the rest of the
    # financial record, when they erase their account.
    display_name = models.CharField(max_length=120, blank=True, default="")
    city = models.CharField(max_length=80, blank=True, default="", db_index=True)
    area = models.CharField(max_length=120, blank=True, default="")
    #: Free text, in the operator's words ("9 ص إلى 9 م"). Not a pair of times: an
    #: agent who closes for Friday prayers or opens twice a day cannot be described by
    #: one, and a wrong structure is worse than a string the shop wrote itself.
    opening_hours = models.CharField(max_length=120, blank=True, default="")
    public_phone = models.CharField(max_length=32, blank=True, default="")
    is_listed = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AgentProfile({self.user_id}, {self.wallet_type}, {self.status})"

    #: currency -> the (daily, monthly) columns that cap top-ups in it.
    LIMIT_FIELDS = {"SYP": ("daily_limit", "monthly_limit"),
                    "USD": ("daily_limit_usd", "monthly_limit_usd")}

    def limits_for(self, currency):
        """`(daily, monthly)` caps for top-ups in `currency`, or None for a currency
        this agent has no caps for — which the caller refuses, never treats as open."""
        fields = self.LIMIT_FIELDS.get(currency)
        if fields is None:
            return None
        return tuple(getattr(self, name) for name in fields)

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']


class AgentAPIKey(models.Model):
    """
    Stores agent API keys. HMAC verification requires the raw secret, so it is
    kept encrypted at rest in `secret_ciphertext` (Fernet, key held in settings —
    never in the DB). `hashed_key` is retained only as a non-signing lookup/compat
    digest and MUST NOT be used as the HMAC signing key.
    """
    agent = models.ForeignKey(AgentProfile, on_delete=models.PROTECT, related_name="api_keys")
    key_id = models.CharField(max_length=32, unique=True)  # public identifier
    hashed_key = models.CharField(max_length=128)  # SHA-256 digest (lookup/compat only, NOT the signing key)
    secret_ciphertext = models.TextField(null=True, blank=True)  # Fernet-encrypted raw secret for HMAC verification
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_rotated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        indexes = [
            # No `Index(fields=["key_id"])`: the column is already `unique=True`, which
            # builds its own btree. This was a second index on the same column.
            models.Index(fields=["is_active"]),
        ]
        constraints = [
            # The "single active key policy" the rotation code describes, said by the
            # database. It was enforced by an unlocked `update()` followed by a
            # `create()` with no transaction, so two concurrent POSTs both deactivated
            # and both created — the agent held two valid signing credentials and
            # revoking the known one left the other live.
            models.UniqueConstraint(
                fields=["agent"], condition=models.Q(is_active=True),
                name="one_active_api_key_per_agent"),
        ]


def _platform_currency():
    """The platform's BASE currency, read per row rather than frozen at import.

    Not "the platform's only currency": Yalla Gym sells in SYP and in USD, both first
    class (the owner, 2026-09-21; `wallet.currencies`). This is what a row gets when
    nothing has said which money it is yet — a wallet nobody has funded, a ledger row
    written outside `move_funds_atomic`. Everything that MOVES money names its currency
    and never falls back here.

    A callable, not `settings.PLATFORM_CURRENCY` evaluated at class-definition time:
    a module-level read is captured once when the class is first loaded, so it would
    stop tracking the setting — the same defect `preferred_timezone` had.
    """
    from django.conf import settings

    return getattr(settings, "PLATFORM_CURRENCY", "SYP")


class ExchangeRate(models.Model):
    """What one currency is worth in the other, as an operator set it. APPEND-ONLY.

    The owner (2026-09-21): "let's link usd to syp, like 1 usd = x sp, and we link this
    to admin dashboard so admin set the rate". So `rate` is read `1 <base> = rate
    <quote>` — one pair, one direction (`wallet.currencies.RATE_BASE/RATE_QUOTE`), one
    number for a person to maintain.

    A NEW RATE IS A NEW ROW. Nothing edits one and nothing deletes one, enforced here
    and by the admin. The reason is not tidiness: every past purchase's meaning depends
    on the rate that was live when it happened, and the Syrian pound moves — that is
    the whole reason this is operator-set rather than a constant. Editing yesterday's
    row would silently re-value yesterday's money. `CoachingPeriod.commission_rate` is
    the same pattern: copied onto the thing it priced, never read back from today.

    "The rate now" is the newest row whose `effective_from` has passed (`current`), so
    an operator can queue a rate for a date without it taking effect early, and a row
    written by mistake is corrected by appending a better one.

    Staleness is in `wallet.exchange`, not here: this model records what was set and
    when, and the policy about how old is too old belongs with the conversion.
    """
    base = models.CharField(max_length=3, choices=SUPPORTED_CURRENCIES)
    quote = models.CharField(max_length=3, choices=SUPPORTED_CURRENCIES)
    #: How many `quote` one `base` buys. Room for 12 whole digits: SYP per USD is in the
    #: thousands today and this column must outlive several more zeros.
    rate = models.DecimalField(max_digits=18, decimal_places=6,
                               validators=[MinValueValidator(Decimal('0.000001'))])
    #: When this rate starts applying. Defaults to now; an operator may date it ahead.
    effective_from = models.DateTimeField(default=timezone.now, db_index=True)
    #: NULL when it did not come from a signed-in operator: a migration, a command, a
    #: shell. Same rule as `policy.CapabilityChangeLog.changed_by`.
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="exchange_rates")
    note = models.CharField(max_length=200, blank=True, default="",
                            help_text="Why this rate, in the operator's words.")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-effective_from", "-id"]
        verbose_name = "Exchange rate"
        verbose_name_plural = "Exchange rates"
        indexes = [models.Index(fields=["base", "quote", "-effective_from", "-id"],
                                name="fx_pair_latest_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(rate__gt=0), name="fx_rate_positive"),
            # A pair must be two DIFFERENT currencies. A row saying 1 USD = 2 USD is
            # not a rate, and it would be picked up by `current` as if it were.
            models.CheckConstraint(condition=~Q(base=models.F("quote")),
                                   name="fx_pair_is_two_currencies"),
        ]

    def __str__(self):
        return f"1 {self.base} = {self.rate} {self.quote} from {self.effective_from:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError(
                "ExchangeRate is append-only: set a new rate instead of editing this one, "
                "or every transaction priced at it changes meaning.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("ExchangeRate is append-only and cannot be deleted.")

    @classmethod
    def current(cls, base: str, quote: str, at=None):
        """The rate in force for `base`/`quote` at `at` (default: now), or None.

        The newest row whose `effective_from` has passed — NOT the newest row, so a rate
        dated into the future is queued rather than live.
        """
        at = at or timezone.now()
        return (cls.objects.filter(base=base, quote=quote, effective_from__lte=at)
                .order_by("-effective_from", "-id").first())


class Wallet(models.Model):
    OWNER_TYPES = [
        ("client", "Client"),
        ("trainer", "Trainer"),
        ("agent", "Agent"),
    ]

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="wallet")
    owner_type = models.CharField(max_length=16, choices=OWNER_TYPES)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    #: Which of the platform's two currencies this wallet holds (`wallet.currencies`:
    #: SYP and USD, both sold). ONE per wallet, for ever — and, because a person has
    #: one wallet, THIS IS THE PERSON'S CURRENCY, the one they chose and are held to
    #: (`currency_for`, `set_currency_for`). A wallet that has never held money takes
    #: the currency of the money that first funds it (`wallet_for`) and keeps it
    #: thereafter; a price in the other currency is converted into this one at
    #: purchase, never credited here in its own unit.
    #:
    #: The default is the platform's base currency from settings — never a hardcoded
    #: literal. It read `default="USD"` while every plan and payment was SYP, and
    #: nothing anywhere set it from the plan, so a 5,000 SYP payment minted 5,000 "USD"
    #: into the wallet and paid the trainer that. See settings_base.PLATFORM_CURRENCY.
    currency = models.CharField(max_length=8, default=_platform_currency)
    #: When the PERSON chose this currency, as opposed to inheriting the platform's
    #: base. Set only by `set_currency_for`.
    #:
    #: The distinction is load-bearing. `wallet_for` lets an untouched wallet take the
    #: currency of the money that first funds it, which is right for a wallet nobody
    #: has expressed a wish about — but a trainer who chose to be paid in dollars and
    #: has not been paid yet has an untouched wallet too, and that rule would have
    #: flipped them to pounds on their first payout, silently, at 1:1. A chosen
    #: currency is never changed by arriving money: the money is converted or refused.
    currency_chosen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Wallet({self.owner_id}, {self.owner_type}, {self.balance} {self.currency})"

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']


class IdempotencyKey(models.Model):
    """Ensures POST/transfer/top-up operations are processed exactly once.

    Scoped to the caller. The key used to be unique across the whole table, and
    callers choose their own, so two clients picking the same string was enough for
    one of them to be handed the other's stored response. See wallet/idempotency.py.
    """
    key = models.CharField(max_length=64, db_index=True)
    request_hash = models.CharField(
        max_length=128, blank=True,
        help_text=(
            "Digest of the request that claimed this key, so a replay carrying "
            "different content is refused instead of answered with the first "
            "request's result. Blank on rows written before this was compared."
        ),
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="idempotency_keys")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed = models.BooleanField(default=False)
    response_snapshot = models.JSONField(null=True, blank=True)

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=["created_by", "key"], name="wallet_idem_created_ec3507_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["created_by", "key"], name="uniq_idempotency_key_per_caller"
            ),
        ]


class Transaction(models.Model):
    TX_TYPES = [
        ("topup", "TopUp"),
        ("transfer", "Transfer"),
        ("reversal", "Reversal"),
        ("adjustment", "Adjustment"),
    ]

    reference_id = models.CharField(max_length=36, unique=True, default=generate_reference_id)
    tx_type = models.CharField(max_length=16, choices=TX_TYPES)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="acted_transactions")
    source_wallet = models.ForeignKey(Wallet, on_delete=models.SET_NULL, null=True, blank=True, related_name="outgoing_transactions")
    destination_wallet = models.ForeignKey(Wallet, on_delete=models.SET_NULL, null=True, blank=True, related_name="incoming_transactions")
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0'))])
    #: The unit of `amount`. `move_funds_atomic` — which writes every row in normal
    #: operation — takes it from the wallets the money moved between, after refusing a
    #: movement whose sides disagree, so it is never guessed there.
    #:
    #: The DEFAULT is the platform's base currency, the same callable `Wallet.currency`
    #: uses. It was the literal `"USD"` while every plan and payment was SYP: the exact
    #: hardcoding whose twin on `Wallet.currency` minted 5,000 SYP into a wallet
    #: labelled USD. A literal here is a loaded gun — it means a row written outside the
    #: ledger helper (a fixture, a shell, the next writer's new code path) is stamped
    #: with a currency nobody chose.
    currency = models.CharField(max_length=8, default=_platform_currency)
    #: Which of the four kinds of money this row moved (`policy.money`). The store rules
    #: attach to the kind, not to the endpoint: a plan payment's legs are PLATFORM_SUB, a
    #: coaching hold and its release are COACHING_1TO1, and a plain top-up or transfer is
    #: WALLET_MOVE — which is also the honest reading of a movement nobody gave a purpose.
    #: Every call in the codebase names its own; `tests/test_policy_money.py` fails on one
    #: that does not.
    money_type = models.CharField(max_length=16, choices=MoneyType.choices,
                                  default=MoneyType.WALLET_MOVE, db_index=True)
    #: THE RATE THIS ROW WAS PRICED AT, when a currency was crossed to write it.
    #:
    #: NULL for the ordinary row, which never crosses: both wallets hold the same
    #: currency and no rate was consulted. Set on BOTH legs of an exchange
    #: (`exchange_funds_atomic`), which is the only place money changes unit.
    #:
    #: A snapshot, not a lookup. `ExchangeRate` is append-only and this is PROTECT, so
    #: the row it names cannot be edited or deleted and what this leg was worth is
    #: fixed for ever — a rate set tomorrow cannot re-value it, and a refund reverses
    #: at what was actually paid rather than at today's number.
    exchange_rate = models.ForeignKey('ExchangeRate', null=True, blank=True,
                                      on_delete=models.PROTECT, related_name='legs')
    #: The same number, copied. The FK is the authority; this is what an operator or an
    #: export reads without a join, and it is the evidence if the row is ever restored
    #: into a database that has lost the rate table.
    exchange_rate_value = models.DecimalField(max_digits=18, decimal_places=6,
                                              null=True, blank=True)
    #: Ties the two legs of one exchange together. Both legs carry the SAME value,
    #: which is the debit leg's `reference_id`, so "what did this exchange do" is one
    #: indexed query and neither half can be read as a movement on its own.
    exchange_reference = models.CharField(max_length=36, blank=True, default="",
                                          db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    #: The transaction this one reverses, when it is a reversal. A OneToOne, so the
    #: DATABASE says a transfer can be reversed at most once.
    #:
    #: The rule was enforced by reading `metadata__original_reference` and refusing if a
    #: row was found — a check in one transaction and a write in another, with no lock on
    #: the original. Two administrators hitting Reverse on the same reference both passed
    #: the check and both moved money: measured with two real threads, one 100.00
    #: transfer came back as 200.00 out of the trainer's wallet. An idempotency key does
    #: not help, because it is scoped to `(created_by, key)` — a different admin, or the
    #: same admin retrying with a fresh key, is a different key.
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reversal", limit_choices_to={"tx_type__in": ("topup", "transfer")})
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ["-created_at", '-id']
        indexes = [
            # No `Index(fields=["reference_id"])`: the column is already `unique=True`,
            # so this was a second btree on the same column of the hottest write table
            # in the app.
            models.Index(fields=["tx_type", "created_at"]),
            # The ledger screen filters `source_wallet = w OR destination_wallet = w`
            # and sorts by `(-created_at, -id)`. Single-column FK indexes cannot serve
            # that OR combined with that sort, so the planner walked `created_at`
            # backwards over the WHOLE table: measured on 500,000 rows, a wallet with no
            # transactions at all took 207 ms and read every row to return zero. One
            # composite per side lets each half of the union use its own index — the
            # same query then costs 0.4 ms and reads nine pages.
            models.Index(fields=["source_wallet", "-created_at", "-id"],
                         name="tx_source_recent_idx"),
            models.Index(fields=["destination_wallet", "-created_at", "-id"],
                         name="tx_destination_recent_idx"),
            # The agent top-up cap counts one actor's top-ups in a window, twice per
            # top-up, and had only the plain `actor_id` index to work with: one agent
            # with 10,000 ledger rows scanned 10,000 to find 500.
            models.Index(fields=["actor", "tx_type", "created_at"],
                         name="tx_actor_type_time_idx"),
        ]
        constraints = [
            # `choices` is only checked by forms; the ledger is written by services.
            models.CheckConstraint(condition=Q(money_type__in=MoneyType.values),
                                   name="tx_money_type_known"),
        ]


class WalletAuditLog(models.Model):
    """
    Append-only audit log for wallet operations and security events.
    Tamper-proof: each entry contains a hash chain from the previous entry.
    """
    event_type = models.CharField(max_length=64)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="wallet_audit_events")
    request_id = models.CharField(max_length=64, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=256, null=True, blank=True)
    path = models.CharField(max_length=256, null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Tamper-proof hash chain fields
    prev_hash = models.CharField(max_length=64, default="0" * 64, help_text="SHA-256 hash of the previous audit entry")
    entry_hash = models.CharField(max_length=64, default="", help_text="SHA-256 hash of this entry (prev_hash + event data)")

    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ["-created_at", '-id']
        indexes = [models.Index(fields=["event_type", "created_at"])]

    @staticmethod
    def _compute_entry_hash(prev_hash, event_type, actor_id, request_id, ip_address, path, payload):
        """Deterministic hash of an entry. Kept in sync with verify_chain()."""
        import hashlib
        import json
        payload_str = json.dumps(payload, sort_keys=True, default=str)
        hash_input = f"{prev_hash}|{event_type}|{actor_id}|{request_id}|{ip_address}|{path}|{payload_str}"
        return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        """
        Compute the tamper-evident hash chain atomically.

        A DB advisory lock (PostgreSQL) serialises chain computation so concurrent
        writes cannot read the same predecessor and fork the chain. On other
        backends (dev SQLite) it degrades to a plain atomic insert.
        """
        if self.entry_hash:
            super().save(*args, **kwargs)
            return

        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cur:
                    # Session-arbitrary constant; scopes the lock to the audit chain.
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", [4222116101])
            last_entry = WalletAuditLog.objects.order_by("-id").values_list("entry_hash", flat=True).first()
            self.prev_hash = last_entry or ("0" * 64)
            self.entry_hash = self._compute_entry_hash(
                self.prev_hash, self.event_type, self.actor_id,
                self.request_id, self.ip_address, self.path, self.payload,
            )
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Append-only at the app layer. True immutability must be enforced at the
        DB layer (revoked DELETE/UPDATE grants or a trigger) — this only blocks
        the ORM path."""
        raise PermissionError("WalletAuditLog is append-only and cannot be deleted.")

    @classmethod
    def verify_chain(cls):
        """
        Recompute the hash chain over all entries in order.
        Returns (is_valid: bool, first_bad_id: int | None).
        """
        prev = "0" * 64
        for entry in cls.objects.order_by("id").iterator():
            expected = cls._compute_entry_hash(
                prev, entry.event_type, entry.actor_id,
                entry.request_id, entry.ip_address, entry.path, entry.payload,
            )
            if entry.prev_hash != prev or entry.entry_hash != expected:
                return False, entry.id
            prev = entry.entry_hash
        return True, None


class AlreadyReversed(Exception):
    """This transaction has been reversed. Money must not move a second time."""

    def __init__(self, reference_id):
        self.reference_id = reference_id
        super().__init__(f"{reference_id} has already been reversed")


def wallet_for(owner, owner_type: str, currency: str | None = None) -> "Wallet":
    """The owner's wallet, in the currency the money is actually arriving in.

    The platform sells in SYP and in USD, both first class (`wallet.currencies`), and
    `Wallet.currency` was a hardcoded `default="USD"` that nothing ever set from the
    plan. Since the currency guard in `move_funds_atomic` only ran when there were two
    wallets, the mint leg of a payment never checked at all: a 5,000 SYP subscription
    credited a wallet labelled USD at 1:1 and paid the trainer 5,000 "USD". The ledger
    reconciled the whole way, because the arithmetic was right and only the unit was
    wrong.

    A PERSON has one wallet. A wallet that has never held money AND whose owner has not
    chosen (`currency_chosen_at`) has no currency of its own yet, so the first payment
    to fund it decides; a currency the person picked is never overwritten by arriving
    money, which is converted or refused instead. Once it holds a balance or has a
    single transaction leg, its currency is a fact: money in a different one is refused
    by the caller rather than dropped in at 1:1. It is not stuck outside, though — a
    price of record in the other currency is CONVERTED into this wallet's currency at
    purchase, once, at the operator's rate (`subscription.pricing`), so the wallet only
    ever sees its own unit. That is the whole of the two-currency rule for a person:
    they trade in one of them, whichever they chose before the first money arrived.

    The PLATFORM's own ledger wallets are the exception, and they get it by having one
    owner per currency rather than by bending this rule: see
    `wallet.utils._ledger_owner_wallet`.

    A TRAINER's wallet is never passed a currency here by the coaching code (SIDE_BUGS
    S-058): a trainer's `trainer_hourly_rate` and every `agreed_rate` are read in the
    trainer's currency, so letting a client's month choose it re-priced the trainer by
    four orders of magnitude. The payout converts instead (`users/coaching.pay_out`).

    The rewrite happens under a row lock, and "untouched" is decided on the LOCKED row
    (SIDE_BUGS S-158). Decided on the unlocked read, a first credit that committed in
    between was relabelled with the new currency at 1:1.
    """
    from django.db import transaction as _transaction

    with _transaction.atomic():
        wallet, created = Wallet.objects.get_or_create(
            owner=owner,
            defaults={"owner_type": owner_type,
                      **({"currency": currency} if currency else {})},
        )
        if created or not currency or wallet.currency == currency:
            return wallet

        wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        if (wallet.currency != currency and wallet.currency_chosen_at is None
                and wallet_is_untouched(wallet)):
            wallet.currency = currency
            wallet.save(update_fields=["currency"])
        return wallet


def wallet_is_untouched(wallet) -> bool:
    """True while this wallet's currency is still a choice rather than a fact.

    No balance and not one ledger leg on either side. The moment either is false the
    currency is history that other rows depend on, and changing it would re-denominate
    every transaction already written against it.
    """
    return bool(wallet.balance == 0 and not Transaction.objects.filter(
        Q(source_wallet=wallet) | Q(destination_wallet=wallet)).exists())


class CurrencyLocked(Exception):
    """This person's currency is already decided by money they have moved."""

    code = "currency_locked"

    def __init__(self, currency):
        self.currency = currency
        super().__init__(f"this account already trades in {currency}")


class CurrencyChangeBlocked(Exception):
    """The wallet is untouched, but money priced in its currency is on its way.

    A checkout still open (or completed and not yet paid out), or a coaching request
    or hold, was priced in the person's current currency. Switching now made the
    payout find a wallet in the other currency and record the payment as settled with
    no ledger leg at all (SIDE_BUGS S-159).
    """

    code = "currency_locked_open_checkout"

    def __init__(self, currency):
        self.currency = currency
        super().__init__(f"money priced in {currency} is still on its way")


class CurrencyMismatch(ValueError):
    """Two sides of one movement are in different currencies. A `ValueError`, so every
    caller that already refuses a bad movement keeps doing so; the `code` is for the
    ones that answer the app."""

    code = "currency_mismatch"

    def __init__(self, currencies):
        self.currencies = sorted(currencies)
        super().__init__(f"Currency mismatch: {self.currencies}")


class InsufficientBalance(ValueError):
    """The source wallet does not hold the amount. A `ValueError`, like the above."""

    code = "insufficient_balance"

    def __init__(self):
        super().__init__("Insufficient balance")


def money_on_its_way(user) -> bool:
    """True while something priced in this person's currency has not landed yet.

    A plan checkout still open, or completed with its payout still owed; a coaching
    request or invitation still pending (its `agreed_rate` was quoted against this
    currency), or a coaching hold not yet paid out. See `CurrencyChangeBlocked`.
    """
    from django.db.models import Q as _Q

    from subscription.models import OPEN_PAYMENT_STATUSES, Payment
    from users.models import TrainerClientRelation

    if Payment.objects.filter(
            _Q(status__in=OPEN_PAYMENT_STATUSES)
            | _Q(status="completed", payout_settled_at__isnull=True),
            subscription__user=user).exists():
        return True
    return TrainerClientRelation.objects.filter(
        _Q(trainer=user) | _Q(client=user)).filter(
        _Q(status="pending") | ~_Q(hold_reference="")).exists()


def currency_for(user) -> str:
    """THE PERSON'S CURRENCY. It is their wallet's, and there is no other answer.

    The owner (2026-09-21): a person picks SYP or USD and "we force him to it" — every
    price they see and every charge they pay is in that one currency, for every service.
    Rather than invent a second field beside `Wallet.currency` that could disagree with
    it, the wallet IS the answer: it already holds exactly this rule (one currency, set
    by the first money in, a fact thereafter), and a ledger whose unit came from
    somewhere else is the whole family of bugs this file is scar tissue for.

    A person with no wallet row yet reads as the platform's base currency, which is what
    they will get the moment one is made for them.
    """
    row = Wallet.objects.filter(owner=user).values_list("currency", flat=True).first()
    return row or _platform_currency()


def set_currency_for(user, currency: str, owner_type: str | None = None) -> "Wallet":
    """Set the currency this person trades in, while that is still theirs to choose.

    Raises `CurrencyLocked` once the wallet holds a balance or has a single ledger leg,
    and `ValueError` for a currency the platform does not trade in. Moving an account
    that already holds money to the other currency would be an exchange of the whole
    balance at a recorded rate — a different act, with a different audit trail, and the
    owner has not asked for it.

    Raises `CurrencyChangeBlocked` while money priced in the current currency is still
    on its way (`money_on_its_way`), even though the wallet is untouched. Asking for
    the currency it already has is not a change and is answered as before.
    """
    from django.db import transaction as _transaction

    from .currencies import CURRENCY_CODES, is_supported

    if not is_supported(currency):
        raise ValueError(f"{currency!r} is not one of {CURRENCY_CODES}")
    with _transaction.atomic():
        wallet, created = Wallet.objects.select_for_update().get_or_create(
            owner=user,
            defaults={"owner_type": owner_type or getattr(user, "user_type", "client"),
                      "currency": currency,
                      "currency_chosen_at": timezone.now()})
        if created:
            return wallet
        if not wallet_is_untouched(wallet):
            # Already decided by money that has moved. Re-choosing the SAME currency is
            # still refused rather than silently accepted: it is the same question, and
            # answering it "yes" once the wallet is locked would tell the app the switch
            # is available when it is not.
            raise CurrencyLocked(wallet.currency)
        if wallet.currency != currency and money_on_its_way(user):
            raise CurrencyChangeBlocked(wallet.currency)
        wallet.currency = currency
        wallet.currency_chosen_at = timezone.now()
        wallet.save(update_fields=["currency", "currency_chosen_at"])
        return wallet


def move_funds_atomic(source: Wallet | None, destination: Wallet | None, amount,
                      actor_id=None, tx_type="transfer", metadata=None, reverses=None,
                      currency: str | None = None,
                      money_type: str | None = None,
                      exchange_rate=None, exchange_reference: str = "") -> Transaction:
    """
    Atomically move funds between wallets. If source is None, it's a credit (e.g., top-up).
    If destination is None, it's a debit. Creates a Transaction record on success.

    ONE CURRENCY PER MOVEMENT, ALWAYS. Every wallet involved, and `currency` when the
    caller names it, must agree; the row is written in that currency. NOTHING is
    converted here and nothing ever will be — a rate belongs to a decision someone made
    (`wallet.exchange`), not to the bottom of the ledger, and a conversion hidden at
    this depth is indistinguishable from a 1:1 transfer at the call site. Money that
    genuinely crosses goes through `exchange_funds_atomic`, which calls this twice and
    stamps both legs with the rate row it used; `exchange_rate`/`exchange_reference` are
    how it does that and are not for anyone else to pass.

    `money_type` says which kind of money this is (`policy.money.MoneyType`). A reversal
    that names none takes the kind of the transaction it reverses; anything else that
    names none is a plain wallet movement.
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")
    if money_type is None:
        money_type = reverses.money_type if reverses is not None else MoneyType.WALLET_MOVE
    if money_type not in MoneyType.values:
        raise ValueError(f"Unknown money type: {money_type!r}")

    metadata = metadata or {}

    # EVERY wallet involved, and the currency the caller says the money is in. The
    # check used to be `if source and destination`, so the mint leg of a payment — which
    # passes `source=None` — was never checked at all: a SYP payment was credited into a
    # wallet labelled USD at 1:1 and the trainer was paid that. `currency` is optional so
    # existing internal callers keep working, but when it is given it is authoritative.
    wallets = [w for w in (source, destination) if w]
    if not wallets:
        # Neither minted nor spent: a row with no wallet on either side moves nothing,
        # belongs to no ledger and has no currency to take. It used to be written, with
        # the literal "USD" stamped on it.
        raise ValueError("a movement needs a wallet on at least one side")
    currencies = {w.currency for w in wallets}
    if currency:
        currencies.add(currency)
    if len(currencies) > 1:
        # Two different units. Converting here would transfer at 1:1, which is the bug
        # this guard exists for; converting at the operator's rate here would hide an
        # exchange inside something every caller reads as a transfer. Both are wrong:
        # `exchange_funds_atomic` is where a currency is crossed, deliberately and in
        # two legs.
        raise CurrencyMismatch(currencies)
    # Exactly one, and it is the money's own: never a literal, never a setting.
    (row_currency,) = currencies

    with transaction.atomic():
        # The actor's user row BEFORE any wallet, in the mode the ledger row's foreign
        # key will take anyway. `Transaction.actor` is DEFERRABLE INITIALLY DEFERRED, so
        # Postgres takes FOR KEY SHARE on that row at COMMIT — after the wallets, which
        # is the reverse of approval, settlement and unassign (relation, user rows,
        # wallets). A client's transfer then held its wallet and waited at COMMIT for a
        # user row that an approval held while waiting for the same wallet: 20
        # deadlocks in 60 contended pairs. Taken here, the order is the same
        # everywhere, and a transfer that has to wait does so holding no wallet. See
        # "Lock order" in users/trainer_relations.py.
        if actor_id is not None and connection.vendor == "postgresql":
            from django.contrib.auth import get_user_model

            meta = get_user_model()._meta
            table, pk = (connection.ops.quote_name(meta.db_table),
                         connection.ops.quote_name(meta.pk.column))
            with connection.cursor() as cur:
                cur.execute(f"SELECT 1 FROM {table} WHERE {pk} = %s FOR KEY SHARE", [actor_id])

        # Lock every wallet involved in ONE deterministic order (ascending id) before
        # touching any of them. Locking in argument order meant a concurrent A->B and
        # B->A pair each held the lock the other needed: Postgres aborted one with
        # "deadlock detected" and that user's transfer failed with a 500.
        wallet_ids = sorted(w.id for w in (source, destination) if w)
        locked = {
            w.id: w
            for w in Wallet.objects.select_for_update().filter(id__in=wallet_ids).order_by("id")
        }
        # The check above read the CALLER's objects, which may be stale: an untouched
        # wallet whose currency `wallet_for` or `set_currency_for` rewrote after the
        # caller read it would otherwise take this row in the old unit at 1:1
        # (SIDE_BUGS S-158). The locked rows are the truth, and nothing can change
        # them now until this transaction ends.
        if {w.currency for w in locked.values()} | {row_currency} != {row_currency}:
            raise CurrencyMismatch({w.currency for w in locked.values()} | {row_currency})

        # BEFORE the balances are touched. Locking the original and re-reading it here
        # is what stops two callers both finding it un-reversed; the OneToOne on
        # `reverses` is the backstop. Order matters for the MESSAGE too: after the first
        # reversal the destination wallet is empty, so a second attempt placed below the
        # balance check fails with "Insufficient balance" — an accidental refusal that
        # reads as a wallet problem and disappears the moment the wallet has other money
        # in it. That is why this defect survived testing.
        if reverses is not None:
            original = (Transaction.objects.select_for_update()
                        .filter(pk=reverses.pk).first())
            if original is None:
                raise ValueError("the transaction being reversed no longer exists")
            if Transaction.objects.filter(reverses=original).exists():
                raise AlreadyReversed(original.reference_id)

        if source:
            src = locked[source.id]
            if src.balance < amount:
                raise InsufficientBalance()
            src.balance = src.balance - amount
            src.save(update_fields=["balance", "updated_at"])
        else:
            src = None

        if destination:
            dst = locked[destination.id]
            dst.balance = dst.balance + amount
            dst.save(update_fields=["balance", "updated_at"])
        else:
            dst = None

        tx = Transaction.objects.create(
            tx_type=tx_type,
            actor_id=actor_id,
            source_wallet=src,
            destination_wallet=dst,
            amount=amount,
            currency=row_currency,
            money_type=money_type,
            exchange_rate=exchange_rate,
            exchange_rate_value=getattr(exchange_rate, "rate", None),
            exchange_reference=exchange_reference,
            metadata=metadata,
            reverses=reverses,
        )

    return tx


def exchange_funds_atomic(source: Wallet, destination: Wallet, amount, *, amount_in: str,
                          actor_id=None, metadata=None, money_type: str | None = None,
                          at=None) -> tuple[Transaction, Transaction]:
    """Move money from a wallet in one currency to a wallet in another, as an EXCHANGE.

    Returns `(debit_leg, credit_leg)`. Two rows, never one:

        debit   source      -> nothing      amount_out  in source.currency
        credit  nothing     -> destination  amount_in   in destination.currency

    Both carry the same `exchange_reference` (the debit's `reference_id`) and the same
    `ExchangeRate` row, so the pair reads as one act and neither half can be mistaken
    for a plain transfer. A single row cannot express this: `Transaction.amount` has one
    `currency`, and the two sides are genuinely different numbers of different things.

    `amount` is in `amount_in`, which must be the currency of ONE of the two wallets;
    the other side is derived by `wallet.exchange.convert_at_the_operators_rate`. Which
    side is authoritative matters, and the caller knows: a coaching payout owes the
    trainer a figure in the trainer's currency, so that is the given side and the debit
    from escrow is derived.

    Raises `NoExchangeRate` / `StaleExchangeRate` (both carry a `code`) rather than
    guessing, and `ValueError` if the two wallets are in fact the same currency — that
    is `move_funds_atomic`, and calling this instead would write two rows where one
    belongs.

    Lock order is unchanged: each leg re-locks its wallets in ascending id inside one
    outer transaction, so a caller that already holds both (every one of them does —
    see `users/coaching.py lock_wallets`) pays nothing for it.
    """
    from .exchange import convert_at_the_operators_rate

    if source.currency == destination.currency:
        raise ValueError("an exchange needs two currencies; use move_funds_atomic")
    if amount_in not in (source.currency, destination.currency):
        raise ValueError(
            f"{amount_in!r} is neither wallet's currency "
            f"({source.currency}, {destination.currency})")

    if amount_in == destination.currency:
        credited = Decimal(amount)
        debited, rate_row = convert_at_the_operators_rate(
            credited, destination.currency, source.currency, at=at)
    else:
        debited = Decimal(amount)
        credited, rate_row = convert_at_the_operators_rate(
            debited, source.currency, destination.currency, at=at)
    if debited <= 0 or credited <= 0:
        # Rounding took one side to nothing. Moving the other anyway would destroy
        # value in one direction and mint it in the other.
        raise ValueError(
            f"this exchange rounds to nothing: {debited} {source.currency} / "
            f"{credited} {destination.currency}")

    common = dict(metadata or {})
    with transaction.atomic():
        debit = move_funds_atomic(
            source, None, debited, actor_id=actor_id, tx_type="transfer",
            currency=source.currency, money_type=money_type, exchange_rate=rate_row,
            metadata={**common, "exchange": "out",
                      "exchange_to": destination.currency,
                      "exchange_to_amount": str(credited)})
        # The debit's reference names the pair, so the link exists before the credit
        # is written and a partial read can never find a credit with no debit.
        Transaction.objects.filter(pk=debit.pk).update(exchange_reference=debit.reference_id)
        debit.exchange_reference = debit.reference_id
        credit = move_funds_atomic(
            None, destination, credited, actor_id=actor_id, tx_type="transfer",
            currency=destination.currency, money_type=money_type, exchange_rate=rate_row,
            exchange_reference=debit.reference_id,
            metadata={**common, "exchange": "in",
                      "exchange_from": source.currency,
                      "exchange_from_amount": str(debited)})
    return debit, credit


def ensure_agent_profile(user) -> "AgentProfile":
    """
    Get-or-create an AgentProfile with the configured default caps.

    New agents are provisioned with AGENT_DEFAULT_DAILY_LIMIT / MONTHLY_LIMIT
    from settings (fail-closed caps), never with 0 (which means "no top-ups").

    Those two are the SYP caps. The USD caps come from
    AGENT_DEFAULT_DAILY_LIMIT_USD / AGENT_DEFAULT_MONTHLY_LIMIT_USD, which nobody has
    set yet: until the owner names a number they are 0, and an agent tops up no dollars
    (SIDE_BUGS S-059 — a cap in pounds used to allow the same number of dollars).
    """
    profile, _created = AgentProfile.objects.get_or_create(
        user=user,
        defaults={
            "wallet_type": "prepaid",
            "status": "active",
            "daily_limit": settings.AGENT_DEFAULT_DAILY_LIMIT,
            "monthly_limit": settings.AGENT_DEFAULT_MONTHLY_LIMIT,
            "daily_limit_usd": Decimal(str(getattr(
                settings, "AGENT_DEFAULT_DAILY_LIMIT_USD", "0"))),
            "monthly_limit_usd": Decimal(str(getattr(
                settings, "AGENT_DEFAULT_MONTHLY_LIMIT_USD", "0"))),
        },
    )
    return profile


