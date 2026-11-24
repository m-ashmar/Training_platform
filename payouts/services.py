"""Everything that changes a payout method or a withdrawal. The ONLY place a
withdrawal's state changes.

**Money.** Every movement is `move_funds_atomic` with `money_type=WALLET_MOVE`: a
withdrawal is real money moving from the platform to a person, which is what
`policy.money` says WALLET_MOVE is ("moving real money between people"), and not a
coaching fee or a digital good. Three legs, each written once:

    request            trainer wallet -> payout clearing   `hold_reference`
    failed / cancelled payout clearing -> trainer wallet   `return_reference`
    confirmed          payout clearing -> (off the platform) `payout_reference`

so the clearing wallet in a currency always holds exactly the open withdrawals in it.

**No leg is undone on its own** (the payouts review, 2026-09-24). Support's reversal
endpoint refuses every one of the three (`withdrawal_of_leg`; 409 `withdrawal_leg`):
reversing the request leg put the money back while the withdrawal stayed open, and the
cancel or fail after it paid the trainer a second time out of clearing — out of every
other trainer's open withdrawal. Undoing a withdrawal is `fail` or `cancel`. As a
backstop, like `users.trainer_relations._move_hold`'s `reverses=held` check, nothing
here moves, sends or pays out a withdrawal whose request leg was reversed some other
way (`_hold_reversal`): the trainer's cancel is refused `withdrawal_hold_reversed`, and
approve, sent, confirm and fail `ledger_refused`. Such a withdrawal is closed by hand.

**Idempotent.** A request carries the caller's `idempotency_key` (`wallet.idempotency`,
like every wallet write): a retry of a request that landed answers the same withdrawal
and moves nothing. A transition that has already happened is a no-op answering the
row as it is; one that cannot happen from the current state is refused, never forced.

**Lock order**, the one everything that moves money uses (`users/trainer_relations.py`):

    request      the trainer's user row (FOR NO KEY UPDATE) -> wallets, ascending id
                 (`users.coaching.lock_wallets`) -> the numbering lock (advisory)
    transitions  the withdrawal row -> the trainer's user row (FOR KEY SHARE; approve
                 and sent) -> the actor's user row (FOR KEY SHARE) -> wallets

The user row serialises one trainer's requests, so "one open request", the daily cap
and the available amount are decided on reads nothing else of theirs can change before
the money moves. The available amount is read off the LOCKED wallet row.

**Approve and sent re-read the trainer** (the payouts review, 2026-09-24): a
verification revoked, or an account suspended, AFTER the request used to leave the
withdrawal to be approved and the money sent off the platform. Both are refused
(`trainer_not_verified`, `trainer_inactive`) and `fail` is the operator's move. The row
is taken FOR KEY SHARE, not FOR NO KEY UPDATE: deleting a payout method holds the
trainer's row FOR NO KEY UPDATE and then updates the withdrawal rows that name it
(`on_delete=SET_NULL`), so NO KEY UPDATE here, after the withdrawal row, deadlocks with
it; KEY SHARE does not conflict with it, and still waits for a revocation (which holds
the row FOR UPDATE). `confirm` does not re-read it: the money has already left the
platform, and refusing to record that it arrived would only leave clearing holding
money that is gone, or push the operator to fail a transfer that arrived and pay it
twice.

**Guardrails** (settings, each a number the owner confirms): `PAYOUT_MIN_AMOUNT`,
`PAYOUT_DAILY_CAP` (per currency; a currency without one withdraws nothing),
`PAYOUT_HOLD_DAYS` (`wallet.settling`), one open request per trainer (also a database
constraint), `PAYOUT_METHODS_MAX`. Only VERIFIED trainers withdraw (spec section 8,
"Only verified trainers can withdraw"): `trainer_is_verified`, which only
`trainer_verification` writes.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext_lazy as _

from policy.money import MoneyType

from .models import (OPEN_STATES, ActorRole, MethodKind, PayoutMethod, State, Withdrawal,
                     WithdrawalEvent)

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
#: Scopes the advisory lock that numbers withdrawals (`_next_number`).
_NUMBER_LOCK = 7300417201
MAX_NOTE = 500


class Refused(Exception):
    """A refusal the app branches on: `code`, an HTTP `status`, a translated message
    and any `extra` keys that go into the body."""

    def __init__(self, code: str, message, status: int = 400, **extra):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


class TransitionRefused(Exception):
    """An operator's transition that cannot happen from the withdrawal's state, or
    lacks what it needs. The message is for the console, in plain English."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class HoldReversed(Exception):
    """The withdrawal's request leg was reversed outside this module, so its money is
    already back in the trainer's wallet (see "No leg is undone on its own")."""

    def __init__(self, withdrawal, reversal: str):
        super().__init__(reversal)
        self.withdrawal = withdrawal
        self.reversal = reversal

    def console_message(self) -> str:
        return (f"Its request leg {self.withdrawal.hold_reference} was reversed "
                f"({self.reversal}), so the money is already back in the trainer's "
                f"wallet. Nothing moved. Do not send it: it has to be closed by hand.")


# --------------------------------------------------------------------- settings
def minimum(currency: str) -> Decimal | None:
    value = (getattr(settings, "PAYOUT_MIN_AMOUNT", {}) or {}).get(currency)
    return Decimal(str(value)) if value is not None else None


def daily_cap(currency: str) -> Decimal:
    """0 for a currency nobody set a cap for: withdrawals in it are refused."""
    value = (getattr(settings, "PAYOUT_DAILY_CAP", {}) or {}).get(currency)
    return Decimal(str(value)) if value is not None else ZERO


def methods_max() -> int:
    return int(getattr(settings, "PAYOUT_METHODS_MAX", 5))


def money(value) -> str | None:
    """An amount as the wire carries it: a string with two decimals."""
    return None if value is None else str(Decimal(value).quantize(ZERO))


# ------------------------------------------------------------------- addresses
_ADDRESS = re.compile(r"^[0-9a-z]{8,64}$")


def normalise_address(raw) -> str:
    """The address as stored: spaces and dashes removed, lower case. The screen shows
    it in groups of four ("0a3f 91c2 7be4 55d0"), and people retype it that way."""
    return re.sub(r"[\s\-]+", "", str(raw or "")).lower()


def address_is_valid(normalised: str) -> bool:
    """8 to 64 ASCII letters and digits. ShamCash has published no format; this refuses
    what cannot be an address without guessing at one (the owner confirms the rule)."""
    return bool(_ADDRESS.match(normalised)) and normalised.isascii()


_DIGEST_SALT = "payouts.shamcash_address"


def digest_keys() -> list[str]:
    """The secret keys an address digest may have been made with: the current
    `SECRET_KEY`, then every `SECRET_KEY_FALLBACKS` entry (a rotation's old key,
    `DJANGO_OLD_SECRET_KEY_1`). The payer-address digests use the same list."""
    return [settings.SECRET_KEY, *(getattr(settings, "SECRET_KEY_FALLBACKS", None) or [])]


def address_digest(normalised: str) -> str:
    """The digest a row is WRITTEN with: under the current key."""
    return salted_hmac(_DIGEST_SALT, normalised).hexdigest()


def address_digests(normalised: str) -> list[str]:
    """Every digest a stored row of this address can carry — under the current key and
    under each fallback — for MATCHING (SIDE_BUGS S-232). `salted_hmac` reads only
    `SECRET_KEY`, so after a key rotation a row saved before it no longer matched: the
    same address could be saved twice (the unique constraint compares digests) and a
    confirmed withdrawal no longer verified the method it went to. The current key's
    digest is first. `manage.py rekey_address_digests` rewrites old rows under the
    current key before the old key is dropped."""
    digests = [salted_hmac(_DIGEST_SALT, normalised, secret=key).hexdigest()
               for key in digest_keys()]
    return list(dict.fromkeys(digests))


def grouped(normalised: str) -> str:
    return " ".join(normalised[i:i + 4] for i in range(0, len(normalised), 4))


# ------------------------------------------------------------------ reading
def open_withdrawal(trainer_id):
    return (Withdrawal.objects.filter(trainer_id=trainer_id, state__in=OPEN_STATES)
            .order_by("-requested_at", "-id").first())


def open_amount(trainer_id) -> Decimal:
    """The money of this trainer's open withdrawal: out of their balance, in clearing."""
    total = (Withdrawal.objects.filter(trainer_id=trainer_id, state__in=OPEN_STATES)
             .aggregate(total=Sum("amount"))["total"])
    return total or ZERO


#: `metadata['purpose']` of each of a withdrawal's three ledger legs (`_ledger_metadata`).
LEG_PURPOSES = ("withdrawal_request", "withdrawal_returned", "withdrawal_paid_out")


def withdrawal_of_leg(tx):
    """`(is_leg, withdrawal)` for the ledger row `tx`: whether it is one of a
    withdrawal's three legs (the request, the return, the payout), and that withdrawal.

    Decided on what the row itself says — every leg carries `withdrawal_id` and its
    `purpose`, written with it and never changed — and on the withdrawal naming it by
    reference, so a leg is found either way. `withdrawal` is None only for a row that
    says it is a leg of a withdrawal that no longer names it.
    """
    from django.db.models import Q

    metadata = tx.metadata or {}
    named = metadata.get("withdrawal_id")
    ref = tx.reference_id
    match = Q(hold_reference=ref) | Q(return_reference=ref) | Q(payout_reference=ref)
    if str(named).isdigit():
        match |= Q(pk=int(named))
    withdrawal = Withdrawal.objects.filter(match).order_by("pk").first()
    is_leg = (withdrawal is not None or named is not None
              or metadata.get("purpose") in LEG_PURPOSES)
    return is_leg, withdrawal


def _hold_reversal(withdrawal) -> str | None:
    """The reference of the reversal of this withdrawal's request leg, or None."""
    from wallet.models import Transaction

    if not withdrawal.hold_reference:
        return None
    return (Transaction.objects.filter(reverses__reference_id=withdrawal.hold_reference)
            .values_list("reference_id", flat=True).first())


def _refuse_if_hold_reversed(withdrawal) -> None:
    reversal = _hold_reversal(withdrawal)
    if reversal:
        logger.error("Withdrawal %s: its request leg %s was reversed by %s; refusing to "
                     "move it", withdrawal.number, withdrawal.hold_reference, reversal)
        raise HoldReversed(withdrawal, reversal)


def _day_bounds(now):
    today = timezone.localdate(now)
    start = timezone.make_aware(datetime.combine(today, time.min))
    return start, start + timedelta(days=1)


def requested_today(trainer_id, currency: str, now=None) -> Decimal:
    """What this trainer asked to withdraw in `currency` today (local day), less what
    was cancelled or failed — that money came back and never left."""
    start, end = _day_bounds(now or timezone.now())
    total = (Withdrawal.objects
             .filter(trainer_id=trainer_id, currency=currency,
                     requested_at__gte=start, requested_at__lt=end)
             .exclude(state__in=(State.CANCELLED, State.FAILED))
             .aggregate(total=Sum("amount"))["total"])
    return total or ZERO


def trainer_wallet(trainer):
    from wallet.models import wallet_for

    # No currency: arriving money never chooses a person's, and neither does a read.
    return wallet_for(trainer, "trainer")


def setup_for(trainer, *, build_refusal: str | None = None, now=None) -> dict:
    """Everything the withdraw screen draws before an amount is chosen.

    `refusal` is the code a request would be refused with right now whatever the
    amount, or null: the build (`build_refusal`, from the view), then
    `trainer_not_verified`, `withdrawal_already_open`, `payout_method_required`,
    `daily_limit_exceeded`, `withdrawal_below_minimum` (less is available than the
    minimum). It never replaces the request's own checks.
    """
    from wallet.settling import hold_days, window

    now = now or timezone.now()
    wallet = trainer_wallet(trainer)
    currency = wallet.currency
    split = window(wallet, now)
    cap = daily_cap(currency)
    cap_left = max(ZERO, cap - requested_today(trainer.pk, currency, now))
    floor = minimum(currency)
    current = open_withdrawal(trainer.pk)
    methods = list(PayoutMethod.objects.filter(trainer=trainer)
                   .select_related("agent", "agent__user")
                   .order_by("-is_default", "-created_at", "-id"))
    refusal = build_refusal
    if refusal is None and not trainer.trainer_is_verified:
        refusal = "trainer_not_verified"
    if refusal is None and current is not None:
        refusal = "withdrawal_already_open"
    if refusal is None and not methods:
        refusal = "payout_method_required"
    if refusal is None and (cap_left <= 0 or (floor is not None and cap_left < floor)):
        refusal = "daily_limit_exceeded"
    if refusal is None and (floor is None or split.available < floor):
        refusal = "withdrawal_below_minimum"
    return {
        "currency": currency,
        "balance": split.balance,
        "available": split.available,
        "settling": split.settling,
        "settling_until": split.settling_until,
        "hold_days": hold_days(),
        "in_withdrawal": open_amount(trainer.pk),
        "minimum": floor,
        "daily_cap": cap,
        "daily_cap_left": cap_left,
        # The design's «لا رسوم من المنصة على السحب»: the platform takes nothing.
        "fee": ZERO,
        "verified": bool(trainer.trainer_is_verified),
        "open_withdrawal": current,
        "methods": methods,
        "refusal": refusal,
    }


# --------------------------------------------------------------- payout methods
def _lock_trainer(trainer_id):
    from users.models import CustomUser

    return CustomUser.objects.select_for_update(no_key=True).get(pk=trainer_id)


def add_method(trainer, *, kind, address="", address_confirm="", holder_name="",
               agent_id=None, make_default=False) -> PayoutMethod:
    """Save a payout method. Raises `Refused`; see `PayoutMethodListView`."""
    from wallet.models import AgentProfile

    if kind not in MethodKind.values:
        raise Refused("payout_method_kind_invalid",
                      _("Choose ShamCash or cash from an agent."),
                      kinds=list(MethodKind.values))
    fields = {"kind": kind}
    if kind == MethodKind.SHAMCASH:
        address, confirm = normalise_address(address), normalise_address(address_confirm)
        holder = str(holder_name or "").strip()
        if not address:
            raise Refused("payout_address_required",
                          _("Type your ShamCash account address."))
        if not address_is_valid(address):
            raise Refused("payout_address_invalid",
                          _("That does not look like a ShamCash account address."))
        if address != confirm:
            raise Refused("payout_address_mismatch",
                          _("The two addresses you typed do not match."))
        if not holder:
            raise Refused("payout_holder_name_required",
                          _("Type the name on the ShamCash account."))
        if len(holder) > 120:
            raise Refused("payout_holder_name_too_long",
                          _("The account holder's name is too long."), max_length=120)
        fields.update(shamcash_address=address, holder_name=holder,
                      address_digest=address_digest(address))
    else:
        agent = (AgentProfile.objects.filter(pk=agent_id, is_listed=True, status="active")
                 .first() if str(agent_id or "").isdigit() else None)
        if agent is None:
            raise Refused("payout_agent_unavailable",
                          _("Choose an agent from the list of agents."))
        fields.update(agent=agent)

    with transaction.atomic():
        _lock_trainer(trainer.pk)
        existing = PayoutMethod.objects.filter(trainer=trainer)
        # Under every key a stored row can carry (S-232): the unique constraint compares
        # one digest, so an address saved before a key rotation is refused here, under
        # the trainer's lock, or it would be saved twice.
        duplicate = (existing.filter(kind=kind,
                                     address_digest__in=address_digests(address))
                     if kind == MethodKind.SHAMCASH
                     else existing.filter(kind=kind, agent=fields["agent"])).first()
        if duplicate is not None:
            raise Refused("payout_method_exists", _("You have already saved this method."),
                          status=409, id=duplicate.pk)
        if existing.count() >= methods_max():
            raise Refused("payout_methods_limit",
                          _("You have saved as many payout methods as you can. Delete "
                            "one first."), status=409, max=methods_max())
        first = not existing.exists()
        if make_default and not first:
            existing.filter(is_default=True).update(is_default=False)
        return PayoutMethod.objects.create(trainer=trainer, is_default=first or make_default,
                                           **fields)


def _own_method(trainer, method_id, *, lock=False) -> PayoutMethod:
    qs = PayoutMethod.objects.filter(trainer=trainer)
    if lock:
        qs = qs.select_for_update()
    method = qs.filter(pk=method_id).first() if str(method_id).isdigit() else None
    if method is None:
        raise Refused("payout_method_not_found", _("No such payout method."), status=404)
    return method


def remove_method(trainer, method_id) -> None:
    """Delete one of the trainer's methods. A withdrawal already made with it keeps its
    own copy of the destination. If it was the default, the newest remaining becomes
    the default, so a trainer with methods always has one."""
    with transaction.atomic():
        _lock_trainer(trainer.pk)
        method = _own_method(trainer, method_id, lock=True)
        was_default = method.is_default
        method.delete()
        if was_default:
            nxt = (PayoutMethod.objects.filter(trainer=trainer)
                   .order_by("-created_at", "-id").first())
            if nxt is not None:
                nxt.is_default = True
                nxt.save(update_fields=["is_default", "updated_at"])


def set_default(trainer, method_id) -> PayoutMethod:
    with transaction.atomic():
        _lock_trainer(trainer.pk)
        method = _own_method(trainer, method_id, lock=True)
        if not method.is_default:
            (PayoutMethod.objects.filter(trainer=trainer, is_default=True)
             .update(is_default=False))
            method.is_default = True
            method.save(update_fields=["is_default", "updated_at"])
        return method


# ------------------------------------------------------------------ requesting
def _next_number(now) -> str:
    """"WD-2026-09-23-041": the local date and the request's place in that day.

    Under a transaction-scoped advisory lock, so two requests cannot count the same
    day at once; the unique column is the backstop. Nothing deletes a withdrawal, so
    the count is the last number used."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [_NUMBER_LOCK])
    prefix = f"WD-{timezone.localdate(now):%Y-%m-%d}-"
    used = Withdrawal.objects.filter(number__startswith=prefix).count()
    return f"{prefix}{used + 1:03d}"


def _event(withdrawal, *, from_state, to_state, actor, role, evidence) -> WithdrawalEvent:
    return WithdrawalEvent.objects.create(
        withdrawal=withdrawal, from_state=from_state or "", to_state=to_state,
        actor=actor, actor_role=role, evidence=evidence or {})


def _ledger_metadata(withdrawal, purpose) -> dict:
    return {"purpose": purpose, "withdrawal_id": withdrawal.pk,
            "withdrawal_number": withdrawal.number}


def _destination(method) -> dict:
    if method.kind == MethodKind.SHAMCASH:
        return {"destination_kind": method.kind,
                "destination_address": method.shamcash_address,
                "destination_holder": method.holder_name}
    return {"destination_kind": method.kind, "destination_agent": method.agent}


def request_withdrawal(trainer, *, amount, method_id=None, idempotency_key: str):
    """Take `amount` out of the trainer's wallet into payout clearing, as a new
    withdrawal. Returns `(withdrawal, replayed)`.

    Raises `Refused` (the view answers its code) or `wallet.idempotency.
    IdempotencyConflict`. A replay of a request that landed — same key, same amount,
    same method — answers that withdrawal before any guardrail is read, so the retry
    of a request that succeeded is never told "you already have one open".
    """
    from users.coaching import lock_wallets
    from wallet import idempotency
    from wallet.models import IdempotencyKey, move_funds_atomic
    from wallet.settling import window
    from wallet.utils import get_payout_clearing_wallet

    amount = Decimal(amount)
    # The method AS NAMED, not as resolved: a replay must match its first request even
    # if the default has moved since.
    digest = idempotency.fingerprint(op="withdrawal", amount=amount,
                                     method=str(method_id) if method_id not in (None, "")
                                     else "default")
    now = timezone.now()

    with transaction.atomic():
        me = _lock_trainer(trainer.pk)
        if IdempotencyKey.objects.filter(created_by=trainer, key=idempotency_key).exists():
            # A finished repeat answers its withdrawal; a key reused for a different
            # request raises `IdempotencyConflict`. A key still in flight cannot be seen
            # here: the first request holds this trainer's row until it commits.
            _record, replay = idempotency.reserve(trainer, idempotency_key, digest)
            return Withdrawal.objects.get(pk=replay["withdrawal_id"], trainer=trainer), True

        # Read off the LOCKED row: a verification revoked a moment ago counts.
        if not me.trainer_is_verified:
            raise Refused("trainer_not_verified",
                          _("Only verified trainers can withdraw. Finish your "
                            "verification first."), status=403)
        if method_id in (None, ""):
            method = (PayoutMethod.objects.filter(trainer=trainer, is_default=True)
                      .select_related("agent").first())
            if method is None:
                raise Refused("payout_method_required",
                              _("Add a payout method first."), status=409)
        else:
            method = _own_method(trainer, method_id)
        if method.kind == MethodKind.AGENT_CASH and not (
                method.agent.is_listed and method.agent.status == "active"):
            raise Refused("payout_agent_unavailable",
                          _("This agent no longer takes payouts. Choose another method."),
                          status=409)

        wallet = trainer_wallet(trainer)
        currency = wallet.currency
        floor = minimum(currency)
        if floor is None or amount < floor:
            raise Refused("withdrawal_below_minimum",
                          _("This is below the smallest amount you can withdraw."),
                          minimum=money(floor),
                          currency=currency)

        current = open_withdrawal(trainer.pk)
        if current is not None:
            raise Refused("withdrawal_already_open",
                          _("You already have a withdrawal in progress. One request at a "
                            "time."), status=409,
                          withdrawal={"id": current.pk, "number": current.number,
                                      "state": current.state})
        cap = daily_cap(currency)
        left = max(ZERO, cap - requested_today(trainer.pk, currency, now))
        if amount > left:
            raise Refused("daily_limit_exceeded",
                          _("This is more than you can withdraw today."), status=429,
                          daily_cap=money(cap), daily_cap_left=money(left), currency=currency)

        clearing = get_payout_clearing_wallet(currency)
        locked = lock_wallets(trainer.pk, [wallet, clearing])
        split = window(locked[wallet.pk], now)
        if amount > split.balance:
            raise Refused("insufficient_balance", _("Insufficient wallet balance."),
                          status=402, currency=currency)
        if amount > split.available:
            raise Refused("amount_exceeds_available",
                          _("Part of your balance is still settling and cannot be "
                            "withdrawn yet."), status=409,
                          available=money(split.available), settling=money(split.settling),
                          settling_until=(split.settling_until.isoformat()
                                          if split.settling_until else None),
                          currency=currency)

        record, _replay = idempotency.reserve(trainer, idempotency_key, digest)
        withdrawal = Withdrawal.objects.create(
            number=_next_number(now), trainer=trainer, amount=amount, currency=currency,
            state=State.REQUESTED, method=method, requested_at=now,
            **_destination(method))
        tx = move_funds_atomic(locked[wallet.pk], locked[clearing.pk], amount,
                               actor_id=trainer.pk, tx_type="transfer", currency=currency,
                               money_type=MoneyType.WALLET_MOVE,
                               metadata=_ledger_metadata(withdrawal, "withdrawal_request"))
        withdrawal.hold_reference = tx.reference_id
        withdrawal.save(update_fields=["hold_reference", "updated_at"])
        _event(withdrawal, from_state="", to_state=State.REQUESTED, actor=trainer,
               role=ActorRole.TRAINER,
               evidence={"ledger": tx.reference_id, "idempotency_key": idempotency_key,
                         "method_id": method.pk})
        idempotency.complete(record, {"withdrawal_id": withdrawal.pk})
        _notify_on_commit("requested", withdrawal)
    logger.info("Withdrawal %s requested by trainer %s", withdrawal.number, trainer.pk)
    return withdrawal, False


# ------------------------------------------------------------------ transitions
def _return_money(withdrawal, actor_id, purpose):
    """Clearing -> the trainer's wallet. Raises `HoldReversed` when the request leg was
    already reversed: decided AFTER both wallets are locked, which a reversal of that
    leg needs too, so one committed a moment ago is seen and one in flight is waited
    for."""
    from users.coaching import lock_wallets
    from wallet.models import move_funds_atomic
    from wallet.utils import get_payout_clearing_wallet

    clearing = get_payout_clearing_wallet(withdrawal.currency)
    wallet = trainer_wallet(withdrawal.trainer)
    locked = lock_wallets(actor_id, [clearing, wallet])
    _refuse_if_hold_reversed(withdrawal)
    tx = move_funds_atomic(locked[clearing.pk], locked[wallet.pk], withdrawal.amount,
                           actor_id=actor_id, tx_type="transfer",
                           currency=withdrawal.currency, money_type=MoneyType.WALLET_MOVE,
                           metadata=_ledger_metadata(withdrawal, purpose))
    return tx.reference_id


def cancel(withdrawal_id, trainer) -> Withdrawal:
    """The trainer takes back a withdrawal nobody has approved yet. Idempotent: a
    cancelled withdrawal is answered as it is. Refused `withdrawal_hold_reversed` (409)
    when its request leg was reversed outside this module: the money is already back in
    the balance, and returning it again would pay it twice."""
    with transaction.atomic():
        withdrawal = (Withdrawal.objects.select_for_update()
                      .filter(pk=withdrawal_id, trainer=trainer).first()
                      if str(withdrawal_id).isdigit() else None)
        if withdrawal is None:
            raise Refused("withdrawal_not_found", _("No such withdrawal."), status=404)
        if withdrawal.state == State.CANCELLED:
            return withdrawal
        if withdrawal.state != State.REQUESTED:
            raise Refused("withdrawal_not_cancellable",
                          _("This withdrawal is already being processed and can no longer "
                            "be cancelled."), status=409, state=withdrawal.state)
        try:
            reference = _return_money(withdrawal, trainer.pk, "withdrawal_returned")
        except HoldReversed as exc:
            raise Refused("withdrawal_hold_reversed",
                          _("This withdrawal's money is already back in your balance. "
                            "Contact support to close it."), status=409,
                          state=withdrawal.state) from exc
        withdrawal.state, withdrawal.cancelled_at = State.CANCELLED, timezone.now()
        withdrawal.return_reference = reference
        withdrawal.save(update_fields=["state", "cancelled_at", "return_reference",
                                       "updated_at"])
        _event(withdrawal, from_state=State.REQUESTED, to_state=State.CANCELLED,
               actor=trainer, role=ActorRole.TRAINER, evidence={"ledger": reference})
    return withdrawal


def _locked_for_operator(withdrawal_id) -> Withdrawal:
    withdrawal = (Withdrawal.objects.select_for_update(of=("self",))
                  .select_related("trainer").filter(pk=withdrawal_id).first())
    if withdrawal is None:
        raise TransitionRefused("withdrawal_not_found", "No such withdrawal.")
    return withdrawal


def _refuse_from(withdrawal, allowed, verb):
    if withdrawal.state not in allowed:
        raise TransitionRefused(
            "invalid_transition",
            f"{withdrawal.number} is {withdrawal.state}; it cannot be {verb}.")


def _note(note) -> str:
    return str(note or "").strip()[:MAX_NOTE]


def _refuse_unpayable_trainer(withdrawal, verb) -> None:
    """Refuse unless the trainer is, NOW, an active account and verified (spec section
    8, "Only verified trainers can withdraw"). Read off the trainer's row taken FOR KEY
    SHARE after the withdrawal row — see "Approve and sent re-read the trainer" in the
    module docstring for the order and the mode."""
    from users.models import CustomUser

    meta = CustomUser._meta
    if connection.vendor == "postgresql":
        quote = connection.ops.quote_name
        with connection.cursor() as cur:
            cur.execute(
                f"SELECT {quote(meta.get_field('is_active').column)}, "
                f"{quote(meta.get_field('trainer_is_verified').column)} "
                f"FROM {quote(meta.db_table)} WHERE {quote(meta.pk.column)} = %s "
                f"FOR KEY SHARE", [withdrawal.trainer_id])
            is_active, verified = cur.fetchone()
    else:
        is_active, verified = (CustomUser.objects.filter(pk=withdrawal.trainer_id)
                               .values_list("is_active", "trainer_is_verified").get())
    if not is_active:
        raise TransitionRefused(
            "trainer_inactive",
            f"The trainer's account is suspended, so it cannot be {verb}. Fail it to "
            f"return the money to their wallet.")
    if not verified:
        raise TransitionRefused(
            "trainer_not_verified",
            f"The trainer is no longer verified (only verified trainers withdraw), so it "
            f"cannot be {verb}. Fail it to return the money to their wallet.")


def _refuse_reversed_for_operator(withdrawal) -> None:
    try:
        _refuse_if_hold_reversed(withdrawal)
    except HoldReversed as exc:
        raise TransitionRefused("ledger_refused", exc.console_message()) from exc


def approve(withdrawal_id, operator, note="") -> Withdrawal:
    """`requested` -> `approved`. Refused `trainer_inactive` / `trainer_not_verified`
    when the trainer may not be paid now, and `ledger_refused` when the request leg was
    reversed; `fail` stays open for both."""
    with transaction.atomic():
        withdrawal = _locked_for_operator(withdrawal_id)
        if withdrawal.state == State.APPROVED:
            return withdrawal
        _refuse_from(withdrawal, (State.REQUESTED,), "approved")
        _refuse_unpayable_trainer(withdrawal, "approved")
        _refuse_reversed_for_operator(withdrawal)
        withdrawal.state, withdrawal.approved_at = State.APPROVED, timezone.now()
        withdrawal.save(update_fields=["state", "approved_at", "updated_at"])
        _event(withdrawal, from_state=State.REQUESTED, to_state=State.APPROVED,
               actor=operator, role=ActorRole.OPERATOR, evidence={"note": _note(note)})
    return withdrawal


def mark_sent(withdrawal_id, operator, transaction_id="", note="") -> Withdrawal:
    """The operator sent it. A ShamCash withdrawal needs the ShamCash transaction id,
    which one withdrawal only may carry; a cash one may carry the agent's reference.
    Refused like `approve` when the trainer may not be paid now or the request leg was
    reversed."""
    transaction_id = str(transaction_id or "").strip()
    if len(transaction_id) > 64:
        raise TransitionRefused("transaction_id_too_long",
                                "The transaction id is longer than 64 characters.")
    with transaction.atomic():
        withdrawal = _locked_for_operator(withdrawal_id)
        if withdrawal.state == State.SENT:
            if transaction_id and transaction_id != (withdrawal.shamcash_transaction_id or ""):
                raise TransitionRefused(
                    "already_sent",
                    f"{withdrawal.number} was already sent with transaction "
                    f"{withdrawal.shamcash_transaction_id or '(none)'}.")
            return withdrawal
        _refuse_from(withdrawal, (State.APPROVED,), "marked sent")
        _refuse_unpayable_trainer(withdrawal, "marked sent")
        _refuse_reversed_for_operator(withdrawal)
        if withdrawal.destination_kind == MethodKind.SHAMCASH and not transaction_id:
            raise TransitionRefused("transaction_id_required",
                                    "Record the ShamCash transaction id it was sent with.")
        if transaction_id and Withdrawal.objects.filter(
                shamcash_transaction_id=transaction_id).exclude(pk=withdrawal.pk).exists():
            raise TransitionRefused("transaction_id_in_use",
                                    f"Transaction {transaction_id} already paid another "
                                    f"withdrawal.")
        withdrawal.state, withdrawal.sent_at = State.SENT, timezone.now()
        withdrawal.shamcash_transaction_id = transaction_id or None
        try:
            with transaction.atomic():
                withdrawal.save(update_fields=["state", "sent_at", "shamcash_transaction_id",
                                               "updated_at"])
        except IntegrityError as exc:
            # Another withdrawal took the id between the check and the write.
            raise TransitionRefused("transaction_id_in_use",
                                    f"Transaction {transaction_id} already paid another "
                                    f"withdrawal.") from exc
        _event(withdrawal, from_state=State.APPROVED, to_state=State.SENT, actor=operator,
               role=ActorRole.OPERATOR,
               evidence={"shamcash_transaction_id": transaction_id, "note": _note(note)})
    return withdrawal


def confirm(withdrawal_id, operator, note="") -> Withdrawal:
    """It arrived. The money leaves payout clearing, and the platform; a ShamCash
    address that received it is marked verified (the design's «محقّق»).

    Not refused for a trainer no longer verified or active: the money already left
    (see the module docstring). Refused `ledger_refused` when the request leg was
    reversed — decided after clearing is locked, which a reversal of that leg needs
    too — because paying it out of clearing again takes another trainer's money."""
    from users.coaching import lock_wallets
    from wallet.models import move_funds_atomic
    from wallet.utils import get_payout_clearing_wallet

    with transaction.atomic():
        withdrawal = _locked_for_operator(withdrawal_id)
        if withdrawal.state == State.CONFIRMED:
            return withdrawal
        _refuse_from(withdrawal, (State.SENT,), "confirmed")
        clearing = get_payout_clearing_wallet(withdrawal.currency)
        locked = lock_wallets(getattr(operator, "pk", None), [clearing])
        _refuse_reversed_for_operator(withdrawal)
        try:
            tx = move_funds_atomic(
                locked[clearing.pk], None, withdrawal.amount,
                actor_id=getattr(operator, "pk", None), tx_type="transfer",
                currency=withdrawal.currency, money_type=MoneyType.WALLET_MOVE,
                metadata={**_ledger_metadata(withdrawal, "withdrawal_paid_out"),
                          "trainer_id": withdrawal.trainer_id})
        except ValueError as exc:
            # Clearing holds every open withdrawal, so this is a ledger out of step
            # (a hand edit, a restore): nothing moved, and the operator is told.
            raise TransitionRefused("ledger_refused",
                                    f"The ledger refused it: {exc}. Nothing moved.") from exc
        now = timezone.now()
        withdrawal.state, withdrawal.confirmed_at = State.CONFIRMED, now
        withdrawal.payout_reference = tx.reference_id
        withdrawal.save(update_fields=["state", "confirmed_at", "payout_reference",
                                       "updated_at"])
        if (withdrawal.destination_kind == MethodKind.SHAMCASH
                and withdrawal.method_id is not None):
            PayoutMethod.objects.filter(
                pk=withdrawal.method_id, verified_at__isnull=True,
                address_digest__in=address_digests(withdrawal.destination_address),
            ).update(verified_at=now)
        _event(withdrawal, from_state=State.SENT, to_state=State.CONFIRMED, actor=operator,
               role=ActorRole.OPERATOR,
               evidence={"ledger": tx.reference_id, "note": _note(note)})
        _notify_on_commit("confirmed", withdrawal)
    return withdrawal


def fail(withdrawal_id, operator, reason) -> Withdrawal:
    """It could not be paid. The money goes back to the trainer's wallet. The reason is
    shown to the trainer, so it is required. Allowed whoever the trainer now is — it is
    the operator's move for a trainer who may no longer be paid — and refused
    `ledger_refused` when the request leg was reversed, whose money is already back."""
    reason = _note(reason)
    if not reason:
        raise TransitionRefused("reason_required",
                                "Say why it failed: the trainer is shown the reason.")
    with transaction.atomic():
        withdrawal = _locked_for_operator(withdrawal_id)
        if withdrawal.state == State.FAILED:
            return withdrawal
        _refuse_from(withdrawal, OPEN_STATES, "failed")
        before = withdrawal.state
        try:
            reference = _return_money(withdrawal, getattr(operator, "pk", None),
                                      "withdrawal_returned")
        except HoldReversed as exc:
            raise TransitionRefused("ledger_refused", exc.console_message()) from exc
        except ValueError as exc:
            raise TransitionRefused("ledger_refused",
                                    f"The ledger refused it: {exc}. Nothing moved.") from exc
        withdrawal.state, withdrawal.failed_at = State.FAILED, timezone.now()
        withdrawal.return_reference, withdrawal.failure_reason = reference, reason
        withdrawal.save(update_fields=["state", "failed_at", "return_reference",
                                       "failure_reason", "updated_at"])
        _event(withdrawal, from_state=before, to_state=State.FAILED, actor=operator,
               role=ActorRole.OPERATOR, evidence={"ledger": reference, "reason": reason})
        _notify_on_commit("failed", withdrawal)
    return withdrawal


# ------------------------------------------------------------------ the pushes
def _notify_on_commit(what: str, withdrawal) -> None:
    """Spec section 7: "Withdrawal requested, paid, failed": push, in-app inbox AND
    email, after the commit. The event's listener writes the inbox notice
    (`notifications/listeners/payout_listeners.py`, `related_object_id` = the
    withdrawal), and `NotificationService._dispatch_channels` sends the push and queues
    the email of the same kind (`notifications/channels/email.py`: `withdrawal_requested`,
    `withdrawal_paid` on `confirmed`, `withdrawal_failed` with the operator's reason),
    once per withdrawal and step, rendered and sent by the worker
    (`notifications.send_email`). The email goes to an active trainer with a verified
    address whose preferences let email through for that kind, and states the fact
    only: the request number and the amount, no link. `approved` and `sent` notify
    nobody."""
    from notifications.domain.dispatcher import emit_event
    from notifications.domain.payout_events import (WithdrawalFailedEvent,
                                                    WithdrawalPaidEvent,
                                                    WithdrawalRequestedEvent)

    common = {"recipient_id": withdrawal.trainer_id, "withdrawal_id": withdrawal.pk,
              "number": withdrawal.number}
    if what == "requested":
        event = WithdrawalRequestedEvent(**common)
    elif what == "confirmed":
        event = WithdrawalPaidEvent(**common)
    else:
        event = WithdrawalFailedEvent(**common, reason=withdrawal.failure_reason)
    transaction.on_commit(lambda: emit_event(event), robust=True)
