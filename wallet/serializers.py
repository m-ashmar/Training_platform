from decimal import Decimal
from rest_framework import serializers
from .models import Wallet, Transaction, AgentProfile, AgentAPIKey, WalletAuditLog, IdempotencyKey

_MIN_AMOUNT = Decimal("0.01")


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ["id", "owner", "owner_type", "balance", "currency", "created_at", "updated_at"]
        read_only_fields = ["owner", "owner_type", "balance", "currency", "created_at", "updated_at"]


class TransactionSerializer(serializers.ModelSerializer):
    source_owner = serializers.SerializerMethodField()
    destination_owner = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
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
        ]
        read_only_fields = fields

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

