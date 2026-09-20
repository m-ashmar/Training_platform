from decimal import Decimal
from rest_framework import serializers
from .models import Wallet, Transaction, AgentProfile, AgentAPIKey, WalletAuditLog, IdempotencyKey

_MIN_AMOUNT = Decimal("0.01")


class WalletSerializer(serializers.ModelSerializer):
    """The wallet as the Wallet and TopUp screens draw it.

    `held` and `held_for` are the "إضافةً إلى 12,000 محجوزة لمدربك رامي" line: money
    that has left the balance into escrow for a coaching month and is neither spent
    nor available. Escrow is a real wallet with a real balance, so without this the
    client's screen could only show the smaller number and no reason for it.

    `account_number` is what the TopUp screen tells the client to read out to an agent
    (`wallet.utils.account_number_for`) — an address, not a secret.

    None of it is a price or a way to buy anything, so all of it is answered on every
    `X-App-Channel`; the spec lets even the iOS build show the balance and the history.
    """
    held = serializers.SerializerMethodField()
    held_for = serializers.SerializerMethodField()
    account_number = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = ["id", "owner", "owner_type", "balance", "currency", "created_at",
                  "updated_at", "held", "held_for", "account_number"]
        read_only_fields = fields

    def _hold(self, obj):
        """The client's live coaching hold as `(amount, relation)`, or `(0, None)`.

        `TrainerClientRelation.hold_reference` is the identity of the hold — the one
        place that says whose money is in escrow — so this reads the transaction it
        names rather than searching escrow by metadata, which is exactly the mistake
        the field was added to fix.
        """
        from decimal import Decimal

        from users.models import TrainerClientRelation

        if "_hold_cache" not in self.__dict__:
            relation = (TrainerClientRelation.objects
                        .filter(client_id=obj.owner_id, status='approved')
                        .exclude(hold_reference='')
                        .select_related('trainer').first())
            amount = Decimal("0.00")
            if relation is not None:
                amount = (Transaction.objects
                          .filter(reference_id=relation.hold_reference)
                          .values_list('amount', flat=True).first()) or Decimal("0.00")
            self._hold_cache = (amount, relation)
        return self._hold_cache

    def get_held(self, obj):
        if obj.owner_type != "client":
            return "0.00"
        return str(self._hold(obj)[0])

    def get_held_for(self, obj):
        if obj.owner_type != "client":
            return None
        amount, relation = self._hold(obj)
        if relation is None or not amount:
            return None
        trainer = relation.trainer
        return {"trainer_id": trainer.id,
                "trainer_name": trainer.full_name or trainer.username}

    def get_account_number(self, obj):
        from .utils import account_number_for

        return account_number_for(obj.owner_id)


class _TransactionListSerializer(serializers.ListSerializer):
    """Gives the child one batch of counterparties for the whole page.

    DRF renders a list row by row, so anything a field looks up is looked up per row.
    The counterparty is a user the row names in its metadata, and reading it per row
    made the ledger cost a query per row again — the exact regression
    `test_the_ledger_does_not_cost_two_queries_per_row` guards. One query here, before
    the first row is rendered.
    """

    def to_representation(self, data):
        rows = list(data)
        prime = getattr(self.child, "prime_counterparties", None)
        if prime is not None:
            prime(rows)
        return super().to_representation(rows)


class TransactionSerializer(serializers.ModelSerializer):
    """One row of "الحركات" on the Wallet screen.

    The stored columns say what the database did; `kind`, `reason`, `direction`,
    `signed_amount` and `counterparty` say what HAPPENED, which is what the screen
    draws: an icon, a line of text, a name and a date, and a number with a sign.
    `wallet.activity` derives all five, and the `wallet` in the serializer context is
    whose side the row is read from — the same escrow release is "returned to you"
    for the client and invisible to everyone else.

    Branch on `kind`, never on `reason`: `reason` is translated and may be reworded;
    `kind` is a contract value and will not change.
    """
    source_owner = serializers.SerializerMethodField()
    destination_owner = serializers.SerializerMethodField()
    kind = serializers.SerializerMethodField()
    reason = serializers.SerializerMethodField()
    direction = serializers.SerializerMethodField()
    signed_amount = serializers.SerializerMethodField()
    counterparty = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        #: Resolves a whole page's counterparties in one query, before any row is
        #: rendered. Per row it was one query each, which is the N+1 the ledger's
        #: own gate test catches (20 rows, 21 extra queries).
        list_serializer_class = _TransactionListSerializer
        fields = [
            "reference_id",
            "tx_type",
            "actor",
            "source_wallet",
            "destination_wallet",
            "amount",
            "currency",
            "money_type",
            "metadata",
            "created_at",
            "source_owner",
            "destination_owner",
            "kind",
            "reason",
            "direction",
            "signed_amount",
            "counterparty",
        ]
        read_only_fields = fields

    @property
    def _wallet(self):
        """Whose ledger this is being read as. Absent for a caller with no wallet in
        context (the operator console's exports), and then the derived fields are
        null rather than a guess about a reader who is not there."""
        return self.context.get("wallet")

    def get_kind(self, obj):
        from . import activity

        wallet = self._wallet
        return activity.kind_of(obj, wallet) if wallet is not None else None

    def get_reason(self, obj):
        from . import activity

        kind = self.get_kind(obj)
        return activity.reason_of(kind) if kind else None

    def get_direction(self, obj):
        from . import activity

        wallet = self._wallet
        return activity.direction_of(obj, wallet) if wallet is not None else None

    def get_signed_amount(self, obj):
        wallet = self._wallet
        if wallet is None:
            return None
        sign = "" if obj.destination_wallet_id == wallet.id else "-"
        return f"{sign}{obj.amount}"

    def prime_counterparties(self, rows):
        """Read every counterparty of `rows` in one query, before rendering starts."""
        from . import activity

        wallet = self._wallet
        if wallet is None:
            return
        owner_ids = self.context.get("ledger_owner_ids")
        ids = {activity.counterparty_id(tx, wallet, owner_ids=owner_ids)
               for tx in rows}
        self.context["counterparty_users"] = activity.counterparty_users(ids)

    def get_counterparty(self, obj):
        from . import activity

        wallet = self._wallet
        if wallet is None:
            return None
        return activity.counterparty_of(
            obj, wallet, owner_ids=self.context.get("ledger_owner_ids"),
            users=self.context.get("counterparty_users"))

    def get_source_owner(self, obj):
        if obj.source_wallet and obj.source_wallet.owner:
            return {
                "id": obj.source_wallet.owner_id,
                "username": getattr(obj.source_wallet.owner, "username", None),
                "user_type": getattr(obj.source_wallet.owner, "user_type", None),
            }
        return None

    def get_destination_owner(self, obj):
        if obj.destination_wallet and obj.destination_wallet.owner:
            return {
                "id": obj.destination_wallet.owner_id,
                "username": getattr(obj.destination_wallet.owner, "username", None),
                "user_type": getattr(obj.destination_wallet.owner, "user_type", None),
            }
        return None


class AgentProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentProfile
        fields = [
            "id", "user", "wallet_type", "daily_limit", "monthly_limit", "status", "ip_allowlist",
            "created_at", "updated_at"
        ]
        read_only_fields = ["created_at", "updated_at", "user"]


class TopUpRequestSerializer(serializers.Serializer):
    client_identifier = serializers.CharField()  # id or email
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=_MIN_AMOUNT)
    idempotency_key = serializers.CharField()
    #: Signed AND checked for freshness. The view used to check the header's copy and
    #: sign the body's, which made them independent and the replay window unbounded.
    timestamp = serializers.IntegerField()
    #: Declared and read by nothing — the signature the view verifies comes from the
    #: `X-Agent-Auth` header. Kept required so a caller that sends it here and nowhere
    #: else fails loudly at the signature check rather than being quietly unauthenticated.
    signature = serializers.CharField()


class TransferRequestSerializer(serializers.Serializer):
    trainer_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=_MIN_AMOUNT)
    idempotency_key = serializers.CharField()


class ReversalRequestSerializer(serializers.Serializer):
    reference_id = serializers.CharField()
    reason = serializers.CharField(required=False, allow_blank=True)
    idempotency_key = serializers.CharField()


class AgentTopUpProxyRequestSerializer(serializers.Serializer):
    client_identifier = serializers.CharField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=_MIN_AMOUNT)
    idempotency_key = serializers.CharField()


class AgentListingSerializer(serializers.ModelSerializer):
    """An agent as the TopUp screen lists them: a shop to walk into.

    Published fields only, and only for an agent an operator has listed. The account
    behind it — limits, key ids, ip allowlist, the user's own registration phone — is
    not here and must not be added: this is the one agent surface a client can read.
    """
    name = serializers.SerializerMethodField()
    phone = serializers.CharField(source="public_phone", read_only=True)
    hours = serializers.CharField(source="opening_hours", read_only=True)

    class Meta:
        model = AgentProfile
        fields = ["id", "name", "city", "area", "hours", "phone"]
        read_only_fields = fields

    def get_name(self, obj):
        return obj.display_name or getattr(obj.user, "full_name", "") or obj.user.username
