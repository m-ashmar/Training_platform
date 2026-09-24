"""The shapes the trainer's money screens read (`15-trainer-money/TrainerWithdraw`,
`TrainerWithdrawStatus`, `TrainerPayoutMethod`). Every value is the trainer's own; no
price and nothing to buy, so the same on every build."""
from decimal import Decimal

from rest_framework import serializers

from wallet.serializers import AgentListingSerializer

from .models import MethodKind, PayoutMethod, State, Withdrawal
from .services import grouped

_MIN_AMOUNT = Decimal("0.01")


def _agent(agent):
    return AgentListingSerializer(agent).data if agent is not None else None


class PayoutMethodSerializer(serializers.ModelSerializer):
    """`{id, kind, address, holder_name, agent, is_default, verified, verified_at,
    created_at}`. `address` is grouped in fours as the screen shows it ("0a3f 91c2 7be4
    55d0"); `agent` is the listing shape of `GET /api/wallet/agents/`. A ShamCash method
    has no `agent`, a cash one no `address` or `holder_name`. `verified` is the design's
    «محقّق»: money sent to this address was confirmed to have arrived."""
    address = serializers.SerializerMethodField()
    holder_name = serializers.SerializerMethodField()
    agent = serializers.SerializerMethodField()
    verified = serializers.SerializerMethodField()

    class Meta:
        model = PayoutMethod
        fields = ["id", "kind", "address", "holder_name", "agent", "is_default",
                  "verified", "verified_at", "created_at"]
        read_only_fields = fields

    def get_address(self, obj):
        return grouped(obj.shamcash_address) if obj.kind == MethodKind.SHAMCASH else None

    def get_holder_name(self, obj):
        return obj.holder_name if obj.kind == MethodKind.SHAMCASH else None

    def get_agent(self, obj):
        return _agent(obj.agent) if obj.kind == MethodKind.AGENT_CASH else None

    def get_verified(self, obj):
        return obj.verified_at is not None


class WithdrawalSerializer(serializers.ModelSerializer):
    """One withdrawal, as the status screen draws it.

    `state`: `requested` | `approved` | `sent` | `confirmed` | `failed` | `cancelled`.
    `destination` is where it goes AS IT WAS WHEN REQUESTED (`{kind, address,
    holder_name, agent}`), whatever has happened to the method since; `method_id` is
    null once that method is deleted. `transfer_reference` is the ShamCash transaction
    the operator sent it with (null until `sent`). `failure_reason` is the operator's
    words when it `failed`. `can_cancel` is true only while `requested`. `timeline` is
    every transition, oldest first: `[{state, at}]`.
    """
    destination = serializers.SerializerMethodField()
    method_id = serializers.IntegerField(read_only=True, allow_null=True)
    transfer_reference = serializers.CharField(source="shamcash_transaction_id",
                                               read_only=True, allow_null=True)
    can_cancel = serializers.SerializerMethodField()
    timeline = serializers.SerializerMethodField()

    class Meta:
        model = Withdrawal
        fields = ["id", "number", "amount", "currency", "state", "destination",
                  "method_id", "transfer_reference", "failure_reason", "can_cancel",
                  "requested_at", "approved_at", "sent_at", "confirmed_at", "failed_at",
                  "cancelled_at", "timeline"]
        read_only_fields = fields

    def get_destination(self, obj):
        shamcash = obj.destination_kind == MethodKind.SHAMCASH
        return {"kind": obj.destination_kind,
                "address": grouped(obj.destination_address) if shamcash else None,
                "holder_name": obj.destination_holder if shamcash else None,
                "agent": None if shamcash else _agent(obj.destination_agent)}

    def get_can_cancel(self, obj):
        return obj.state == State.REQUESTED

    def get_timeline(self, obj):
        # `.all()`, so a list prefetched with `events` costs no query per row.
        return [{"state": e.to_state, "at": e.created_at}
                for e in sorted(obj.events.all(), key=lambda e: (e.created_at, e.pk))]


class WithdrawalRequestSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=_MIN_AMOUNT)
    #: One of the trainer's payout methods; the default one when left out.
    method_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    idempotency_key = serializers.CharField(max_length=64)
