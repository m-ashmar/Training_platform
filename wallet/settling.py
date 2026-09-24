"""The settling window: money released to a trainer that is not withdrawable yet.

The spec (section 8, "Trainer payouts"): "a hold of a few days on newly released escrow
before it becomes withdrawable, to cover disputes". The design draws it as a third
number beside the balance and the escrow, «قيد التسوية · يتاح خلال 3 أيام»
(`15-trainer-money/TrainerEarnings`), and BACKEND_BUGS B-097 is the app having to guess
it from `created_at`.

WHAT SETTLES. A coaching payout: escrow -> trainer, the rows `wallet.activity` reads as
`trainer_fee_payout` (month 1 on the first program, every renewal, an owed share paid
later, and the legacy one-fee settlement), including the credit leg of a payout that
crossed a currency. Nothing else: a client's direct transfer and a plan share are not
released escrow, and the spec holds only that. A payout support has since REVERSED is
not settling either — the money is no longer in the wallet.

HOW LONG. `PAYOUT_HOLD_DAYS` after the row was written (the release). 0 switches the
hold off.

WHAT IS AVAILABLE. The balance less what is settling, and never below zero: the balance
is one number, so the newest money is read as the settling part. Money already in a
withdrawal is NOT subtracted again — it left the balance at the request
(`payouts/services.py` moves it to payout clearing), so it is reported beside
(`in_withdrawal`), never inside.

Read-only, derived from the ledger on every read: there is no "settled" flag to keep
in step and nothing on a timer. A record of money that has already moved, so it is the
same on every `X-App-Channel`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

ZERO = Decimal("0.00")


def payout_purposes() -> frozenset:
    """The `metadata['purpose']` values of a coaching payout: exactly the rows
    `wallet.activity` calls `trainer_fee_payout`, so the two can never disagree."""
    from .activity import PURPOSE_KINDS

    return frozenset(p for p, kind in PURPOSE_KINDS.items() if kind == "trainer_fee_payout")


def hold_days() -> int:
    return max(0, int(getattr(settings, "PAYOUT_HOLD_DAYS", 3)))


def hold() -> timedelta:
    return timedelta(days=hold_days())


def settling_rows(wallet, now: datetime | None = None):
    """The payouts into `wallet` still inside the window, not reversed."""
    from .models import Transaction

    now = now or timezone.now()
    if not hold_days():
        return Transaction.objects.none()
    return (Transaction.objects
            .filter(destination_wallet=wallet, created_at__gt=now - hold(),
                    metadata__purpose__in=sorted(payout_purposes()))
            .filter(reversal__isnull=True))


@dataclass(frozen=True)
class Window:
    balance: Decimal
    settling: Decimal
    available: Decimal
    #: When the next settling payout becomes available, or None when nothing settles.
    settling_until: datetime | None


def window(wallet, now: datetime | None = None) -> Window:
    """The balance of `wallet` split into available and settling, as of `now`.

    Reads `wallet.balance` as given: a caller deciding whether money may move passes the
    row it has LOCKED (`payouts/services.py`), so the split is true when the money moves.
    """
    now = now or timezone.now()
    balance = Decimal(wallet.balance or 0)
    rows = list(settling_rows(wallet, now).values_list("amount", "created_at"))
    raw = sum((amount for amount, _at in rows), ZERO)
    settling = min(raw, balance) if balance > 0 else ZERO
    until = min((at for _amount, at in rows), default=None)
    return Window(balance=balance, settling=settling, available=balance - settling,
                  settling_until=(until + hold()) if until is not None and settling > 0
                  else None)


def available_at(tx) -> datetime | None:
    """When the money of one payout row stops settling; None for any other row."""
    purpose = (tx.metadata or {}).get("purpose")
    if purpose not in payout_purposes():
        return None
    return tx.created_at + hold()


# ------------------------------------------------------------------ on the wire
#
# Two serializer MIXINS rather than edits to `WalletSerializer.Meta.fields` and
# `TransactionSerializer.Meta.fields`: another build adds its own fields to the same two
# serializers, and a mixin whose only footprint there is the class line keeps both
# merges mechanical. DRF does not require a field declared on a parent class to be named
# in `Meta.fields` — nor does it render one that is not — so each mixin appends its own
# names in `get_field_names`.


class WalletSettlingFields(serializers.Serializer):
    """`available`, `settling`, `settling_until` and `in_withdrawal` on a wallet read.

    For a trainer (BACKEND_BUGS B-097): `available` is what a withdrawal may take now
    (`balance - settling`), `settling` the coaching payouts still inside the
    `PAYOUT_HOLD_DAYS` window, `settling_until` when the next of them becomes available
    (ISO 8601, null when nothing settles), and `in_withdrawal` the money of the
    trainer's open withdrawal, which has already left `balance`. For anyone else the
    hold does not apply: `available` is the balance, the others `0.00` / null.
    """

    SETTLING_FIELDS = ("available", "settling", "settling_until", "in_withdrawal")

    available = serializers.SerializerMethodField()
    settling = serializers.SerializerMethodField()
    settling_until = serializers.SerializerMethodField()
    in_withdrawal = serializers.SerializerMethodField()

    def get_field_names(self, declared_fields, info):
        names = list(super().get_field_names(declared_fields, info))
        return names + [n for n in self.SETTLING_FIELDS if n not in names]

    def _window(self, obj) -> Window:
        cache = self.__dict__.setdefault("_settling_windows", {})
        if obj.pk not in cache:
            if obj.owner_type == "trainer":
                cache[obj.pk] = window(obj)
            else:
                balance = Decimal(obj.balance or 0)
                cache[obj.pk] = Window(balance, ZERO, balance, None)
        return cache[obj.pk]

    def get_available(self, obj):
        return str(self._window(obj).available.quantize(Decimal("0.01")))

    def get_settling(self, obj):
        return str(self._window(obj).settling.quantize(Decimal("0.01")))

    def get_settling_until(self, obj):
        until = self._window(obj).settling_until
        return until.isoformat() if until else None

    def get_in_withdrawal(self, obj):
        if obj.owner_type != "trainer":
            return "0.00"
        from payouts.services import open_amount

        return str(open_amount(obj.owner_id).quantize(Decimal("0.01")))


class TransactionSettlingFields(serializers.Serializer):
    """`available_at` and `settling` on a ledger row (BACKEND_BUGS B-097).

    On a `trainer_fee_payout` row read from the trainer's side: when its money becomes
    withdrawable (`created_at + PAYOUT_HOLD_DAYS`, ISO 8601) and whether that is still
    ahead. Every other row: `null` and `false`. A payout support reversed is never
    settling. The reversed payouts inside the window are read in ONE query per response,
    and only when a page holds a payout still inside it, so the ledger stays at a
    constant number of queries whatever its length.
    """

    SETTLING_FIELDS = ("available_at", "settling")

    available_at = serializers.SerializerMethodField()
    settling = serializers.SerializerMethodField()

    def get_field_names(self, declared_fields, info):
        names = list(super().get_field_names(declared_fields, info))
        return names + [n for n in self.SETTLING_FIELDS if n not in names]

    def _reader(self):
        return self.context.get("wallet")

    def _applies(self, obj) -> bool:
        wallet = self._reader()
        return wallet is not None and obj.destination_wallet_id == wallet.id

    def get_available_at(self, obj):
        if not self._applies(obj):
            return None
        at = available_at(obj)
        return at.isoformat() if at else None

    def get_settling(self, obj):
        if not self._applies(obj):
            return False
        at = available_at(obj)
        now = self.context.setdefault("settling_now", timezone.now())
        if at is None or at <= now:
            return False
        reversed_ids = self.context.get("settling_reversed_ids")
        if reversed_ids is None:
            from .models import Transaction

            reversed_ids = set(Transaction.objects.filter(
                reverses__destination_wallet=self._reader(),
                reverses__created_at__gt=now - hold(),
            ).values_list("reverses_id", flat=True))
            self.context["settling_reversed_ids"] = reversed_ids
        return obj.pk not in reversed_ids
