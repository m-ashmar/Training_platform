from django.db import models, transaction
from django.conf import settings
from django.utils import timezone
from django.core.validators import MinValueValidator
import uuid


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

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_profile")
    wallet_type = models.CharField(max_length=16, choices=AGENT_WALLET_TYPES, default="prepaid")
    daily_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    monthly_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    status = models.CharField(max_length=16, choices=AGENT_STATUS_CHOICES, default="active")
    ip_allowlist = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AgentProfile({self.user_id}, {self.wallet_type}, {self.status})"


class AgentAPIKey(models.Model):
    """Stores hashed API keys for agents. Raw value is never stored."""
    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name="api_keys")
    key_id = models.CharField(max_length=32, unique=True)  # public identifier
    hashed_key = models.CharField(max_length=128)  # hex digest of HMAC/sha256
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_rotated_at = models.DateTimeField(auto_now=True)

    class Meta:
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

    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet")
    owner_type = models.CharField(max_length=16, choices=OWNER_TYPES)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=8, default="USD")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Wallet({self.owner_id}, {self.owner_type}, {self.balance} {self.currency})"


class IdempotencyKey(models.Model):
    """Ensures POST/transfer/top-up operations are processed exactly once."""
    key = models.CharField(max_length=64, unique=True)
    request_hash = models.CharField(max_length=128)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="idempotency_keys")
    created_at = models.DateTimeField(auto_now_add=True)
    processed = models.BooleanField(default=False)
    response_snapshot = models.JSONField(null=True, blank=True)

    class Meta:
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
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=8, default="USD")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
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
    created_at = models.DateTimeField(auto_now_add=True)

    # Tamper-proof hash chain fields
    prev_hash = models.CharField(max_length=64, default="0" * 64, help_text="SHA-256 hash of the previous audit entry")
    entry_hash = models.CharField(max_length=64, default="", help_text="SHA-256 hash of this entry (prev_hash + event data)")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["event_type", "created_at"])]

    def save(self, *args, **kwargs):
        """Compute tamper-proof hash chain on save."""
        import hashlib
        import json

        if not self.entry_hash:
            # Get the hash of the most recent log entry
            last_entry = WalletAuditLog.objects.order_by("-id").values_list("entry_hash", flat=True).first()
            self.prev_hash = last_entry or ("0" * 64)

            # Compute entry hash: H(prev_hash || event_type || actor_id || payload)
            payload_str = json.dumps(self.payload, sort_keys=True, default=str)
            hash_input = f"{self.prev_hash}|{self.event_type}|{self.actor_id}|{payload_str}"
            self.entry_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

        super().save(*args, **kwargs)


def move_funds_atomic(source: Wallet | None, destination: Wallet | None, amount, actor_id=None, tx_type="transfer", metadata=None) -> Transaction:
    """
    Atomically move funds between wallets. If source is None, it's a credit (e.g., top-up).
    If destination is None, it's a debit. Creates a Transaction record on success.
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")

    metadata = metadata or {}

    with transaction.atomic():
        # Lock rows to prevent race conditions
        if source:
            src = Wallet.objects.select_for_update().get(id=source.id)
            if src.balance < amount:
                raise ValueError("Insufficient balance")
            src.balance = src.balance - amount
            src.save(update_fields=["balance", "updated_at"])
        else:
            src = None

        if destination:
            dst = Wallet.objects.select_for_update().get(id=destination.id)
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


