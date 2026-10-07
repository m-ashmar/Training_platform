"""`note` on a ledger row: what a client's payment to a trainer was for (BACKEND_BUGS B-163).

SendMoney collects «ملاحظة لرامي» ("جلسة الجمعة الإضافية"). The note is user-written
text, so it never enters the ledger, which is append-only and kept through an erasure:
it lives on `wallet.models.CoachingTransfer` and is erased with either person's account.
It is JOINED to the two rows the two people read when the ledger is read: the client's
leg (client -> escrow, `coaching_transfer: "charge"`) and the trainer's (escrow ->
trainer, `coaching_transfer: "payout"`, which names the charge as `transfer_reference`).

Read in ONE query per page, and only when the page holds such a row
(`prime_transfer_notes`, called by the ledger's list serializer before the first row
renders), so the ledger keeps its constant query count. Imports nothing from `wallet`
models at module level: `wallet/serializers.py` imports this.
"""
from rest_framework import serializers


def transfer_reference(tx, wallet) -> str | None:
    """The charge reference of the coaching transfer `tx` is the reader's leg of, or
    None. Only the client's leg read from the client's wallet and the trainer's leg read
    from the trainer's: the commission leg is nobody's, and a reader on the other side of
    a row (escrow's, an export's) is not one of the two people."""
    if wallet is None:
        return None
    metadata = tx.metadata if isinstance(tx.metadata, dict) else {}
    leg = metadata.get("coaching_transfer")
    if leg == "charge" and tx.source_wallet_id == wallet.id:
        return tx.reference_id
    if leg == "payout" and tx.destination_wallet_id == wallet.id:
        ref = metadata.get("transfer_reference")
        return ref if isinstance(ref, str) and ref else None
    return None


def notes_by_reference(refs) -> dict:
    """`{charge_reference: note}` for `refs`, in one query (none when `refs` is empty).
    A transfer sent without a note, or whose note was erased, is not in it — nor one
    between two people of whom either has blocked the other, for as long as the block
    stands (the review of wave 1, 2026-10-07): a block closes one-to-one text, and an
    invitation's note is hidden the same way (`users.views` ClientPendingTrainerInvites).
    The note is only ever shown to one of the two people, so the block between THEM is
    the one that counts, read in the same query (`moderation.services.blocked_between`).
    """
    from moderation.services import blocked_between

    from .models import CoachingTransfer

    refs = {r for r in refs if r}
    if not refs:
        return {}
    return dict(CoachingTransfer.objects.filter(charge_reference__in=refs)
                .exclude(note="")
                .exclude(blocked_between("client_id", "trainer_id"))
                .values_list("charge_reference", "note"))


class TransactionNoteFields(serializers.Serializer):
    """`note` on a ledger row (BACKEND_BUGS B-163).

    On a client's payment to a trainer, read by either of the two people from their own
    row: the line the client wrote to the trainer, or null when they wrote none (or it
    was erased with an account, or either of the two has blocked the other since — it
    shows again if the block is lifted). Null on every other row.
    """

    NOTE_FIELDS = ("note",)

    note = serializers.SerializerMethodField()

    def get_field_names(self, declared_fields, info):
        names = list(super().get_field_names(declared_fields, info))
        return names + [n for n in self.NOTE_FIELDS if n not in names]

    def prime_transfer_notes(self, rows):
        """Read every note `rows` carry in one query, before rendering starts."""
        wallet = self.context.get("wallet")
        self.context["transfer_notes"] = notes_by_reference(
            transfer_reference(tx, wallet) for tx in rows)

    def get_note(self, obj):
        ref = transfer_reference(obj, self.context.get("wallet"))
        if ref is None:
            return None
        found = self.context.get("transfer_notes")
        if found is None:
            found = notes_by_reference([ref])
        return found.get(ref)
