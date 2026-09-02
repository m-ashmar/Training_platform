from django.db import models, transaction, connection
from django.conf import settings
from django.utils import timezone
from django.core.validators import MinValueValidator
import uuid
from decimal import Decimal


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
    daily_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    monthly_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    status = models.CharField(max_length=16, choices=AGENT_STATUS_CHOICES, default="active")
    ip_allowlist = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AgentProfile({self.user_id}, {self.wallet_type}, {self.status})"

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
            models.Index(fields=["key_id"]),
            models.Index(fields=["is_active"]),
        ]


class Wallet(models.Model):
    OWNER_TYPES = [
        ("client", "Client"),
        ("trainer", "Trainer"),
        ("agent", "Agent"),
    ]

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="wallet")
    owner_type = models.CharField(max_length=16, choices=OWNER_TYPES)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0'))])
    currency = models.CharField(max_length=8, default="USD")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Wallet({self.owner_id}, {self.owner_type}, {self.balance} {self.currency})"

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']


class IdempotencyKey(models.Model):
    """Ensures POST/transfer/top-up operations are processed exactly once."""
    key = models.CharField(max_length=64, unique=True)
    request_hash = models.CharField(max_length=128)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="idempotency_keys")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed = models.BooleanField(default=False)
    response_snapshot = models.JSONField(null=True, blank=True)

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=["key"])]


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
    currency = models.CharField(max_length=8, default="USD")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ["-created_at", '-id']
        indexes = [
            models.Index(fields=["reference_id"]),
            models.Index(fields=["tx_type", "created_at"]),
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


def move_funds_atomic(source: Wallet | None, destination: Wallet | None, amount, actor_id=None, tx_type="transfer", metadata=None) -> Transaction:
    """
    Atomically move funds between wallets. If source is None, it's a credit (e.g., top-up).
    If destination is None, it's a debit. Creates a Transaction record on success.
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")

    metadata = metadata or {}

    if source and destination and source.currency != destination.currency:
        # No FX rate exists in this system; moving the raw amount between wallets of
        # different currencies would silently transfer at 1:1.
        raise ValueError(
            f"Currency mismatch: {source.currency} -> {destination.currency}"
        )

    with transaction.atomic():
        # Lock every wallet involved in ONE deterministic order (ascending id) before
        # touching any of them. Locking in argument order meant a concurrent A->B and
        # B->A pair each held the lock the other needed: Postgres aborted one with
        # "deadlock detected" and that user's transfer failed with a 500.
        wallet_ids = sorted(w.id for w in (source, destination) if w)
        locked = {
            w.id: w
            for w in Wallet.objects.select_for_update().filter(id__in=wallet_ids).order_by("id")
        }

        if source:
            src = locked[source.id]
            if src.balance < amount:
                raise ValueError("Insufficient balance")
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
            currency=(destination.currency if destination else (source.currency if source else "USD")),
            metadata=metadata,
        )

    return tx


def ensure_agent_profile(user) -> "AgentProfile":
    """
    Get-or-create an AgentProfile with the configured default caps.

    New agents are provisioned with AGENT_DEFAULT_DAILY_LIMIT / MONTHLY_LIMIT
    from settings (fail-closed caps), never with 0 (which means "no top-ups").
    """
    profile, _created = AgentProfile.objects.get_or_create(
        user=user,
        defaults={
            "wallet_type": "prepaid",
            "status": "active",
            "daily_limit": settings.AGENT_DEFAULT_DAILY_LIMIT,
            "monthly_limit": settings.AGENT_DEFAULT_MONTHLY_LIMIT,
        },
    )
    return profile


