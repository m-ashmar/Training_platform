"""`withdrawal` on a ledger row: where the money went and whether it arrived (B-125).

A trainer's `withdrawal` / `withdrawal_returned` row is a ledger row: it records that
money left the balance (or came back), and the ledger is append-only, so it cannot
also carry what happened to the transfer afterwards. The design's row «سحب إلى شام
كاش · 20 آب · وصل» needs both, so the row is JOINED to its withdrawal when it is read:
the destination as it was sent and the withdrawal's state now.

Read in ONE query per page (`prime_withdrawals`, called by the ledger's list
serializer before the first row renders), so the ledger keeps its constant query
count. Imports nothing from `wallet` at module level: `wallet/serializers.py` imports
this.
"""
from rest_framework import serializers


def _label(withdrawal) -> str:
    """What the row says the money went to, never the whole address: the last four
    characters of a ShamCash address, or the agent's shop name."""
    from .models import MethodKind

    if withdrawal.destination_kind == MethodKind.SHAMCASH:
        address = (withdrawal.destination_address or "").strip()
        return f"•••• {address[-4:]}" if address else ""
    agent = withdrawal.destination_agent
    return (agent.display_name or "") if agent is not None else ""


def withdrawal_block(withdrawal) -> dict:
    from .models import State

    return {
        "id": withdrawal.pk,
        "number": withdrawal.number,
        "state": withdrawal.state,
        "arrived": withdrawal.state == State.CONFIRMED,
        "destination_kind": withdrawal.destination_kind,
        "destination_label": _label(withdrawal),
    }


class TransactionWithdrawalFields(serializers.Serializer):
    """`withdrawal` on a ledger row (BACKEND_BUGS B-125).

    On a row that moved a trainer's withdrawal money, read from that trainer's own
    wallet: `{id, number, state, arrived, destination_kind, destination_label}` —
    `state` is the withdrawal's state NOW (`requested` … `confirmed`, `failed`,
    `cancelled`), `arrived` is `state == "confirmed"`, `destination_kind` is `shamcash`
    or `agent_cash`, `destination_label` the last four characters of the ShamCash
    address or the agent's shop name. `null` on every other row, and on any row read
    from a wallet that is not the trainer's (the clearing wallet's, the console's).
    """

    WITHDRAWAL_FIELDS = ("withdrawal",)

    withdrawal = serializers.SerializerMethodField()

    def get_field_names(self, declared_fields, info):
        names = list(super().get_field_names(declared_fields, info))
        return names + [n for n in self.WITHDRAWAL_FIELDS if n not in names]

    @staticmethod
    def _withdrawal_id(obj):
        metadata = obj.metadata if isinstance(obj.metadata, dict) else {}
        value = metadata.get("withdrawal_id")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def prime_withdrawals(self, rows):
        """Read every withdrawal `rows` name in one query, before rendering starts."""
        from .models import Withdrawal

        ids = {pk for pk in (self._withdrawal_id(tx) for tx in rows) if pk is not None}
        self.context["withdrawals_by_id"] = (
            {w.pk: w for w in Withdrawal.objects.filter(pk__in=ids)
             .select_related("destination_agent")} if ids else {})

    def get_withdrawal(self, obj):
        wallet = self.context.get("wallet")
        pk = self._withdrawal_id(obj)
        if wallet is None or pk is None:
            return None
        if wallet.id not in (obj.source_wallet_id, obj.destination_wallet_id):
            return None
        found = self.context.get("withdrawals_by_id")
        if found is None or pk not in found:
            from .models import Withdrawal

            withdrawal = (Withdrawal.objects.select_related("destination_agent")
                          .filter(pk=pk).first())
        else:
            withdrawal = found[pk]
        if withdrawal is None or withdrawal.trainer_id != wallet.owner_id:
            return None
        return withdrawal_block(withdrawal)
