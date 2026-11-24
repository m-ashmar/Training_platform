"""The agent shell: caps, the receivable, settlements and the signing key's pairing.

Plan step 9 (the spec, section 8 "Agents"; BACKEND_BUGS B-113..B-118, B-124). The screens
are `16-agent-operator/`: AgentHome, AgentHistory, AgentReceipt, AgentKey, AgentMe.

THE CAPS, per agent and per currency, all FAIL CLOSED (`0` means no top-ups):

| Cap | Column | Refusal (429) |
|---|---|---|
| one top-up | `per_transaction_limit` / `_usd` (NULL: none of its own) | `per_transaction_limit_exceeded` |
| a local day | `daily_limit` / `_usd` | `daily_limit_exceeded` |
| a calendar month | `monthly_limit` / `_usd` | `monthly_limit_exceeded` |
| cash held, unsettled | `receivable_ceiling` / `_usd` (NULL: the monthly cap) | `receivable_ceiling_exceeded` |

checked in that order, each refusal carrying `limit`, `used` (null for the per-top-up
cap), `remaining` and `currency` (BACKEND_BUGS B-124). One function (`usage_of`) counts
what is used for the enforcement AND for `GET wallet/agent/me/`, so the screen and the
refusal never disagree.

THE RECEIVABLE is `AgentReceivableEntry` (see its docstring for why a record and not a
ledger wallet): every agent top-up posts `+amount` in the same transaction, a
settlement an operator records posts `-amount`, and a reversal of a top-up posts
`-amount`. Everything that writes it holds the agent's profile row lock
(`lock_profile`), which is also the lock one agent's top-ups serialise on, so a
`balance_after` is true when written and the ceiling check is true when the money moves.

LOCK ORDER. The profile row first, then (inside `move_funds_atomic`) the actor's user
row and the wallets by ascending id. A reversal of an agent top-up takes the profile
BEFORE `move_funds_atomic` for the same reason (`wallet.views.AdminReversalView`); a
settlement takes only the profile.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

CENT = Decimal("0.01")
ZERO = Decimal("0")


def money(value) -> str | None:
    """A Decimal as the API writes money: a string with two decimals, or None."""
    if value is None:
        return None
    return str(Decimal(value).quantize(CENT))


def windows(now=None):
    """`(day_start, tomorrow, month_start)`: the local day and calendar month the caps
    count, as aware datetimes. Ranges on `created_at`, never a function of it, so
    `tx_actor_type_time_idx` serves them (see `wallet.views._topup_limit_error`)."""
    today = timezone.localdate(now)
    day_start = timezone.make_aware(datetime.datetime.combine(today, datetime.time.min))
    month_start = timezone.make_aware(
        datetime.datetime.combine(today.replace(day=1), datetime.time.min))
    return day_start, day_start + datetime.timedelta(days=1), month_start


# ------------------------------------------------------------------------ the caps
@dataclass(frozen=True)
class CapRefusal:
    code: str
    scope: str
    limit: Decimal
    used: Decimal | None
    remaining: Decimal
    currency: str

    def body(self, message) -> dict:
        return {"error": message, "code": self.code, "limit": money(self.limit),
                "used": money(self.used), "remaining": money(self.remaining),
                "currency": self.currency}


@dataclass(frozen=True)
class Usage:
    """One agent's caps in one currency, and what is used against them now."""
    currency: str
    per_transaction_limit: Decimal | None
    daily_limit: Decimal
    monthly_limit: Decimal
    used_today: Decimal
    used_this_month: Decimal
    count_today: int
    count_this_month: int
    receivable: Decimal
    receivable_ceiling: Decimal

    @property
    def remaining_today(self) -> Decimal:
        return max(ZERO, self.daily_limit - self.used_today)

    @property
    def remaining_this_month(self) -> Decimal:
        return max(ZERO, self.monthly_limit - self.used_this_month)

    @property
    def receivable_room(self) -> Decimal:
        return max(ZERO, self.receivable_ceiling - self.receivable)

    def refusal(self, amount) -> CapRefusal | None:
        """The first cap a top-up of `amount` would break, or None."""
        amount = Decimal(amount)
        per_tx = self.per_transaction_limit
        if per_tx is not None and amount > per_tx:
            return CapRefusal("per_transaction_limit_exceeded", "per_transaction", per_tx,
                              None, per_tx, self.currency)
        if self.used_today + amount > self.daily_limit:
            return CapRefusal("daily_limit_exceeded", "daily", self.daily_limit,
                              self.used_today, self.remaining_today, self.currency)
        if self.used_this_month + amount > self.monthly_limit:
            return CapRefusal("monthly_limit_exceeded", "monthly", self.monthly_limit,
                              self.used_this_month, self.remaining_this_month,
                              self.currency)
        if self.receivable + amount > self.receivable_ceiling:
            return CapRefusal("receivable_ceiling_exceeded", "receivable",
                              self.receivable_ceiling, self.receivable,
                              self.receivable_room, self.currency)
        return None

    def max_topup(self) -> Decimal:
        """The largest top-up the caps allow now (0 when any of them is spent)."""
        bounds = [self.remaining_today, self.remaining_this_month, self.receivable_room]
        if self.per_transaction_limit is not None:
            bounds.append(self.per_transaction_limit)
        return max(ZERO, min(bounds))

    def blocked_reason(self) -> str | None:
        """The code the smallest possible top-up would be refused with, or None."""
        refusal = self.refusal(CENT)
        return refusal.code if refusal is not None else None


def receivable_of(profile, currency) -> Decimal:
    from .models import AgentReceivableEntry

    return (AgentReceivableEntry.objects.filter(agent=profile, currency=currency)
            .aggregate(total=Sum("amount"))["total"] or ZERO)


def platform_cash_held_by(user) -> list[dict]:
    """The platform's cash `user` holds as an agent and has not settled:
    `[{"currency", "amount"}]`, the non-zero currencies only, by currency, each amount a
    decimal string. Empty for an account that never took cash.

    ONE count for what account deletion shows (`money.agent_cash`,
    `training_platform/privacy/views.py money_left`) and what refuses it (409
    `agent_holds_platform_cash`, the owner's rule of 2026-11-24), so the preview and the
    refusal never disagree. Read-only, no lock: the erasure takes the profile lock
    before it asks (`training_platform/privacy/sources.py`)."""
    from .models import AgentReceivableEntry

    return [{"currency": row["currency"], "amount": str(row["total"])}
            for row in (AgentReceivableEntry.objects.filter(agent__user=user)
                        .values("currency").annotate(total=Sum("amount"))
                        .order_by("currency"))
            if row["total"]]


def usage_of(profile, currency, now=None) -> Usage:
    """What `profile` may still top up in `currency`. Counts the agent's top-up ledger
    rows (`actor`, `tx_type="topup"`, this currency), the same rows the caps have always
    counted: a top-up later reversed still used the cap it was made under."""
    from .models import Transaction

    limits = profile.limits_for(currency)
    daily, monthly = limits if limits is not None else (ZERO, ZERO)
    day_start, tomorrow, month_start = windows(now)
    mine = Transaction.objects.filter(actor_id=profile.user_id, tx_type="topup",
                                      currency=currency)
    today = mine.filter(created_at__gte=day_start, created_at__lt=tomorrow).aggregate(
        total=Sum("amount"), n=Count("id"))
    month = mine.filter(created_at__gte=month_start).aggregate(
        total=Sum("amount"), n=Count("id"))
    ceiling = profile.receivable_ceiling_for(currency)
    return Usage(
        currency=currency,
        per_transaction_limit=profile.per_transaction_limit_for(currency)
        if limits is not None else ZERO,
        daily_limit=daily, monthly_limit=monthly,
        used_today=today["total"] or ZERO, used_this_month=month["total"] or ZERO,
        count_today=today["n"] or 0, count_this_month=month["n"] or 0,
        receivable=receivable_of(profile, currency),
        # NULL is the monthly cap, never "no ceiling": the caps fail closed.
        receivable_ceiling=ceiling if ceiling is not None else monthly,
    )


def lock_profile(profile):
    """The agent's profile row, locked. Every write of the receivable and every top-up
    holds it, so one agent's money is decided one request at a time. Raises
    TransactionManagementError outside an atomic block, which is the loud failure
    wanted over a silently unlocked read."""
    from .models import AgentProfile

    return AgentProfile.objects.select_for_update().get(pk=profile.pk)


# ------------------------------------------------------------------ the receivable
def _post(profile, *, kind, currency, amount, transaction_row=None, settlement=None,
          created_at=None):
    from policy.money import MoneyType

    from .models import AgentReceivableEntry

    balance = receivable_of(profile, currency) + amount
    return AgentReceivableEntry.objects.create(
        agent=profile, kind=kind, currency=currency, amount=amount,
        balance_after=balance, money_type=MoneyType.WALLET_MOVE,
        transaction=transaction_row, settlement=settlement,
        **({"created_at": created_at} if created_at else {}))


def post_topup(profile, tx):
    """Post a top-up the agent just made: the cash they now hold for the platform. Call
    inside the top-up's transaction, with `profile` locked (`lock_profile`)."""
    return _post(profile, kind="topup", currency=tx.currency, amount=tx.amount,
                 transaction_row=tx)


def profile_of_topup(tx):
    """The AgentProfile an agent top-up is owed by, or None for any other row (a plan
    payment's mint leg is also a `topup`, and names no `agent_id`)."""
    from .models import AgentProfile

    if tx.tx_type != "topup" or not (tx.metadata or {}).get("agent_id"):
        return None
    return AgentProfile.objects.filter(user_id=tx.actor_id).first()


def post_reversal(profile, reversal_tx):
    """A reversed agent top-up comes off what the agent owes: the agent hands the cash
    back to the client. Same transaction and lock as the reversal."""
    return _post(profile, kind="reversal", currency=reversal_tx.currency,
                 amount=-reversal_tx.amount, transaction_row=reversal_tx)


class SettlementRefused(Exception):
    """The console's settlement could not be recorded; nothing was written."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def record_settlement(profile_id, currency, amount, operator, *, note="", key):
    """An operator records that the agent handed over `amount` of `currency`.

    Under the agent's profile lock; IDEMPOTENT on `key` (the console form's token): the
    same key answers the settlement it already recorded and writes nothing. Refuses a
    currency the platform does not trade in, an amount that is not positive, and more
    than the agent holds now — a settlement cannot make the platform owe the agent.
    Writes the immutable `AgentSettlement` and its negative receivable entry together.
    """
    from .currencies import is_supported
    from .models import AgentProfile, AgentSettlement

    key = (key or "").strip()
    if not key:
        raise SettlementRefused("settlement_key_required", "The form carried no token.")
    if not is_supported(currency):
        raise SettlementRefused("currency_unsupported",
                                f"{currency!r} is not a currency the platform trades in.")
    try:
        amount = Decimal(amount).quantize(CENT)
    except Exception:
        raise SettlementRefused("settlement_amount_invalid", "The amount is not a number.")
    if amount <= 0:
        raise SettlementRefused("settlement_amount_invalid",
                                "A settlement is a positive amount.")
    with transaction.atomic():
        profile = lock_profile(AgentProfile(pk=profile_id))
        done = AgentSettlement.objects.filter(idempotency_key=key).first()
        if done is not None:
            return done
        before = receivable_of(profile, currency)
        if amount > before:
            raise SettlementRefused(
                "settlement_exceeds_receivable",
                f"The agent holds {money(before)} {currency} for the platform; a "
                f"settlement of {money(amount)} would leave the platform owing them.")
        settlement = AgentSettlement.objects.create(
            agent=profile, currency=currency, amount=amount, receivable_before=before,
            receivable_after=before - amount, note=(note or "")[:300],
            recorded_by=operator, idempotency_key=key)
        _post(profile, kind="settlement", currency=currency, amount=-amount,
              settlement=settlement, created_at=settlement.created_at)
    return settlement


def next_settlement_on(profile, today=None):
    """The next settlement date AN OPERATOR SET, while it is still ahead (today counts),
    else None. Nothing schedules settlements, so nothing else is ever served."""
    day = profile.next_settlement_on
    if day is None or day < timezone.localdate(today):
        return None
    return day


def last_settlements(profile) -> dict:
    """currency -> the newest settlement in it."""
    from .currencies import CURRENCY_CODES
    from .models import AgentSettlement

    out = {}
    for code in CURRENCY_CODES:
        row = (AgentSettlement.objects.filter(agent=profile, currency=code)
               .order_by("-created_at", "-id").first())
        if row is not None:
            out[code] = row
    return out


# ------------------------------------------------------------------ the signing key
def active_key(profile):
    from .models import AgentAPIKey

    return (AgentAPIKey.objects.filter(agent=profile, is_active=True)
            .order_by("-created_at", "-id").first())


def key_revoked(profile) -> bool:
    """True while the agent's top-ups are stopped by `apikey/revoke/`: revoked, and no
    key paired since. An agent who never paired a key is not stopped."""
    return profile.key_revoked_at is not None and active_key(profile) is None


def mark_signed(api_key, at=None) -> None:
    """The key signed a top-up now. `update()`, so `last_rotated_at` (auto_now) does not
    move: signing is not rotating."""
    from .models import AgentAPIKey

    at = at or timezone.now()
    AgentAPIKey.objects.filter(pk=api_key.pk).update(last_signature_at=at)
    api_key.last_signature_at = at


def key_body(api_key) -> dict:
    """The pairing AgentKey draws, for one key (or the empty pairing)."""
    if api_key is None:
        return {"key_id": None, "fingerprint": None, "device_name": None,
                "paired_at": None, "last_signature_at": None}
    return {"key_id": api_key.key_id, "fingerprint": api_key.fingerprint,
            "device_name": api_key.device_name or None,
            "paired_at": _iso(api_key.created_at),
            "last_signature_at": _iso(api_key.last_signature_at)}


def _iso(value):
    """ISO 8601 in the SERVER's zone (BACKEND_BUGS B-146): the same instant it always
    was, now with the offset `wallet/transactions/` rows carry (+03:00), so the date
    part of a row's `created_at` is the day `since` / `until` and the caps count it in."""
    return timezone.localtime(value).isoformat() if value is not None else None


def utc_offset_minutes(local_moment) -> int:
    """The UTC offset of an aware moment in whole minutes east of UTC (+03:00 is 180,
    -05:00 is -300): the offset `_iso` writes the agent's rows with (BACKEND_BUGS
    B-190)."""
    return int(local_moment.utcoffset().total_seconds() // 60)


# ------------------------------------------------------------------ the push
def notify_client_on_commit(tx, client, profile) -> None:
    """The client hears of the top-up once it has committed (BACKEND_BUGS B-115). Only
    a top-up that moved money calls this: a replay returns before it."""
    from notifications.domain.dispatcher import emit_event
    from notifications.domain.wallet_events import WalletToppedUpEvent

    event = WalletToppedUpEvent(
        recipient_id=client.id, reference_id=tx.reference_id, amount=money(tx.amount),
        currency=tx.currency, agent=agent_name(profile))
    transaction.on_commit(lambda: emit_event(event), robust=True)


def client_notified(entry) -> bool:
    """Whether the client's notice of this top-up was written (the in-app row the push
    is sent from)."""
    from notifications.models import Notification

    tx = entry.transaction
    client = tx.destination_wallet.owner if tx and tx.destination_wallet else None
    if client is None:
        return False
    return Notification.objects.filter(recipient=client, event_type="wallet_topped_up",
                                       related_object_id=tx.reference_id).exists()


# ------------------------------------------------------------------ what the app reads
def agent_name(profile) -> str:
    user = profile.user
    return profile.display_name or getattr(user, "full_name", "") or user.username


def person(user) -> dict | None:
    from .utils import account_number_for

    if user is None:
        return None
    return {"id": user.id, "name": user.full_name or user.username,
            "account_number": account_number_for(user.id)}


def account_body(profile) -> dict:
    """`GET wallet/agent/me/` (BACKEND_BUGS B-113; `currency` B-144, `today` and
    `time_zone` B-146, `utc_offset_minutes` B-190)."""
    from django.conf import settings

    from .currencies import CURRENCY_CODES
    from .models import currency_for

    blocked = ("agent_not_active" if profile.status != "active"
               else "agent_key_revoked" if key_revoked(profile) else None)
    last = last_settlements(profile)
    currencies = {}
    for code in CURRENCY_CODES:
        usage = usage_of(profile, code)
        settlement = last.get(code)
        currencies[code] = {
            "currency": code,
            "per_transaction_limit": money(usage.per_transaction_limit),
            "daily_limit": money(usage.daily_limit),
            "monthly_limit": money(usage.monthly_limit),
            "used_today": money(usage.used_today),
            "used_this_month": money(usage.used_this_month),
            "count_today": usage.count_today,
            "count_this_month": usage.count_this_month,
            "remaining_today": money(usage.remaining_today),
            "remaining_this_month": money(usage.remaining_this_month),
            "receivable": money(usage.receivable),
            "receivable_ceiling": money(usage.receivable_ceiling),
            "receivable_room": money(usage.receivable_room),
            "max_topup": money(ZERO if blocked else usage.max_topup()),
            "blocked_reason": blocked or usage.blocked_reason(),
            "last_settlement": settlement_row(settlement) if settlement else None,
        }
    day = next_settlement_on(profile)
    # One reading of the clock for the day and its offset, so the two always agree.
    local_now = timezone.localtime()
    return {
        "user_id": profile.user_id,
        "profile": {"display_name": profile.display_name, "city": profile.city,
                    "area": profile.area, "opening_hours": profile.opening_hours,
                    "public_phone": profile.public_phone, "status": profile.status,
                    "is_listed": profile.is_listed},
        "status": profile.status,
        "can_top_up": blocked is None,
        "blocked_reason": blocked,
        "next_settlement_on": day.isoformat() if day else None,
        # The agent's own wallet currency (B-144): which of `currencies` to draw first.
        "currency": currency_for(profile.user_id),
        # The server's day and zone (B-146): the caps' "today" and the day `since` /
        # `until` on agent/ledger/ are read in.
        "today": local_now.date().isoformat(),
        "time_zone": settings.TIME_ZONE,
        # That zone's offset from UTC now, in minutes east (B-190; 180 for
        # Asia/Damascus): the offset `today` and the rows' timestamps are written with,
        # so the phone never has to map a zone name to an offset itself.
        "utc_offset_minutes": utc_offset_minutes(local_now),
        "currencies": currencies,
    }


def settlement_row(settlement) -> dict:
    return {"number": settlement.number, "amount": money(settlement.amount),
            "currency": settlement.currency,
            "receivable_before": money(settlement.receivable_before),
            "receivable_after": money(settlement.receivable_after),
            "note": settlement.note, "settled_at": _iso(settlement.created_at)}


def _client_of(entry):
    tx = entry.transaction
    if tx is None:
        return None
    wallet = tx.destination_wallet if entry.kind == "topup" else tx.source_wallet
    return getattr(wallet, "owner", None)


def _reversal_of(tx):
    """The row that reversed `tx`, or None (read off `select_related`, no query)."""
    from django.core.exceptions import ObjectDoesNotExist

    try:
        return tx.reversal
    except ObjectDoesNotExist:
        return None


def entry_row(entry) -> dict:
    """One row of the agent's ledger (BACKEND_BUGS B-114; the link between a top-up and
    its reversal, B-147)."""
    tx = entry.transaction
    return {
        "id": entry.pk,
        "kind": entry.kind,
        "amount": money(entry.amount),
        "currency": entry.currency,
        "receivable_after": money(entry.balance_after),
        "created_at": _iso(entry.created_at),
        "reference_id": tx.reference_id if tx else None,
        "receipt_number": entry.receipt_number,
        "client": person(_client_of(entry)),
        "settlement": settlement_row(entry.settlement) if entry.settlement_id else None,
        # B-147: a reversal row names the top-up it took back (whose receipt opens by
        # that reference), and a top-up row says whether it was taken back.
        "reverses_reference_id": (tx.reverses.reference_id
                                  if entry.kind == "reversal" and tx is not None
                                  and tx.reverses_id else None),
        "reversed": (_reversal_of(tx) is not None
                     if entry.kind == "topup" and tx is not None else None),
    }


def receipt_body(entry) -> dict:
    """AgentReceipt: one top-up the agent made, by its ledger reference."""
    from .models import Transaction

    profile = entry.agent
    tx = entry.transaction
    usage = usage_of(profile, entry.currency)
    return {
        "receipt_number": entry.receipt_number,
        "reference_id": tx.reference_id,
        "amount": money(entry.amount),
        "currency": entry.currency,
        "created_at": _iso(tx.created_at),
        "client": person(_client_of(entry)),
        "agent": {"id": profile.pk, "name": agent_name(profile), "city": profile.city,
                  "area": profile.area},
        "remaining_today": money(usage.remaining_today),
        "client_notified": client_notified(entry),
        "reversed": Transaction.objects.filter(reverses=tx).exists(),
    }
