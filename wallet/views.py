import datetime

from django.db import transaction
from django.db.models import Sum, Q
from django.conf import settings
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status, generics
from django.utils.translation import gettext as _
from .models import (AlreadyReversed, Wallet, Transaction, AgentProfile, AgentAPIKey,
                     WalletAuditLog, IdempotencyKey, move_funds_atomic,
                     ensure_agent_profile, wallet_for)
from .serializers import (
    WalletSerializer,
    TransactionSerializer,
    TopUpRequestSerializer,
    TransferRequestSerializer,
    ReversalRequestSerializer,
    AgentTopUpProxyRequestSerializer,
    AgentListingSerializer,
)
from .permissions import IsAgent, IsAdmin
from .security import (
    parse_agent_auth_header,
    verify_hmac_signature,
    is_fresh_timestamp,
    decrypt_secret,
    encrypt_secret,
)
from .throttles import ChargingRateThrottle
import uuid
from policy.money import MoneyType
from policy.services import require_purchase_allowed
from training_platform.middleware import get_recordable_client_ip
from training_platform.query_params import body_dict, int_param
from wallet import idempotency


User = get_user_model()


def audit(event_type: str, request, payload: dict):
    WalletAuditLog.objects.create(
        event_type=event_type,
        actor=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
        request_id=getattr(request, "id", None),
        # The client, not the proxy: behind Fly REMOTE_ADDR is the edge proxy, so every
        # row recorded the same address. One helper decides it for the whole app.
        ip_address=get_recordable_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT"),
        path=request.path,
        payload=payload or {},
    )


def _normalised_allowlist(entries):
    """An agent's allow-list as normalised address strings; entries that are not a single
    address are kept verbatim, so they still match exactly what they matched before."""
    import ipaddress

    out = set()
    for entry in entries or []:
        text = str(entry).strip()
        try:
            out.add(str(ipaddress.ip_address(text)))
        except ValueError:
            out.add(text)
    return out


#: What each cap refusal says, by `wallet.agents.CapRefusal.scope`. The first two are
#: the words they always had.
def _cap_message(scope):
    return {
        "per_transaction": _("This amount is more than one top-up may be."),
        "daily": _("Daily limit exceeded"),
        "monthly": _("Monthly limit exceeded"),
        "receivable": _("You hold as much cash for the platform as your ceiling allows. "
                        "Settle with the platform to top up again."),
    }[scope]


def _topup_limit_error(agent_profile, actor, amount, request, currency):
    """
    Enforce the agent's top-up caps FAIL-CLOSED. A limit of 0 means "no top-ups
    permitted", not unlimited. Returns a Response on breach, else None: 429 with
    `per_transaction_limit_exceeded`, `daily_limit_exceeded`, `monthly_limit_exceeded`
    or `receivable_ceiling_exceeded`, checked in that order (`wallet.agents`). A cap is
    not the rate limiter — waiting out a `Retry-After` does not lift it — so it does not
    answer the limiter's `throttled` (the owner, 2026-09-24: every refusal names its own
    code).

    Every one of the four carries `limit`, `used`, `remaining` and `currency` beside
    `error` and `code` (BACKEND_BUGS B-124; additive): the cap, what is already used
    against it (today, this month, or the cash held unsettled; null for the per-top-up
    cap, which nothing uses up), what is left of it, and its currency — so the app can
    tell "you reached your cap" from "this amount is more than what is left".

    The per-top-up cap and the receivable ceiling are plan step 9 (the spec, section 8
    "Agents"): a top-up that would take the cash the agent holds for the platform past
    their ceiling is refused until an operator records a settlement.

    PER CURRENCY (SIDE_BUGS S-059). The totals count only this currency's top-ups and
    are held against this currency's caps (`AgentProfile.limits_for`). They used to add
    pounds and dollars together against one number, so a cap an operator set in pounds
    let the same number of dollars through — about 13,000 times the value.

    CONCURRENCY CONTRACT: `agent_profile` must already be locked by
    `wallet.agents.lock_profile`, and the ledger write this guards must happen in the SAME
    transaction. These two aggregates are a read; on their own they are a
    check-then-act. Ten concurrent top-ups of 400 against a 1000/day cap each read a
    total that predated the other nine, all passed, and minted 3200 — 3.2x the cap.
    Holding the profile lock from here through move_funds_atomic is what makes the
    aggregate below true at the moment the money moves.

    The counting is `wallet.agents.usage_of` — RANGES on `created_at`, never a function
    of it, so `tx_actor_type_time_idx` serves them (a filter on the column's date
    scanned every row the agent had ever written) — and `GET wallet/agent/me/` reads the
    same function, so the screen and the refusal never disagree. `actor` is the agent's user,
    whose rows `usage_of` counts through the profile.
    """
    from . import agents

    usage = agents.usage_of(agent_profile, currency)
    refusal = usage.refusal(amount)
    if refusal is None:
        return None
    audit("wallet.topup.limit_block", request,
          {"scope": refusal.scope, "currency": currency, "attempt": float(amount),
           "limit": float(refusal.limit),
           "used": float(refusal.used) if refusal.used is not None else None})
    return Response(refusal.body(_cap_message(refusal.scope)),
                    status=status.HTTP_429_TOO_MANY_REQUESTS)


def _topup_currency_mismatch(client, currency):
    """409 `currency_mismatch` when the cash is not in the client's currency, else None.

    An agent top-up carried no currency, so the typed number was minted in whatever the
    client's wallet held: a client who had chosen dollars and handed over 500,000
    pounds of cash received 500,000 DOLLARS (SIDE_BUGS S-059). The agent now says which
    currency the cash is in, and money in the other one is refused rather than
    relabelled. Arriving money never chooses a person's currency either: a client who
    has not picked dollars is a pound client until they do (`PUT /api/wallet/currency/`,
    allowed while the wallet is empty).
    """
    from .models import currency_for

    theirs = currency_for(client)
    if theirs == currency:
        return None
    return Response({"error": _("This client's wallet is in a different currency."),
                     "code": "currency_mismatch",
                     "client_currency": theirs, "topup_currency": currency},
                    status=status.HTTP_409_CONFLICT)


def _idempotency_conflict(exc):
    """Turn an idempotency clash into the response the caller can act on.

    The two cases need different answers. A first attempt still running is a 409 the
    caller may retry once it finishes. A key reused for a *different* request is a 422:
    retrying will never help, and answering it with the earlier request's stored
    receipt — which is what this code used to do — tells the caller money moved when
    none did.

    409 `request_in_flight` (the code `users` answers for the same fact) and 422
    `idempotency_key_reused`. Both answered only their status defaults, `conflict` and
    `unprocessable`, until the owner's decision of 2026-09-24. `idempotency_key_reused`
    is NOT the set, workout and chat writes' 409 `idempotency_conflict`, although both
    are about an id: there the id is the OTHER person's record, so the caller's write
    never happened and the app sends again under a new id; here the key is the caller's
    OWN, already spent on a different request of theirs that may have moved money, so
    sending again under a new key could move it twice. Two facts, two recoveries, two
    codes (the review of B-092 undid a merge of the two before it shipped).
    """
    if exc.in_flight:
        return Response({"error": _("Duplicate request"), "code": "request_in_flight"},
                        status=status.HTTP_409_CONFLICT)
    return Response(
        {"error": _("This idempotency key was already used for a different request."),
         "code": "idempotency_key_reused"},
        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


def _agent_auth_refused(message):
    """401 `agent_auth_invalid`: the `X-Agent-Auth` signature did not authenticate.

    Each failure keeps the message it always had; the CODE is one for all of them, so it
    adds no way for a caller without the key to tell an unknown key from a bad signature.
    The status default it replaces, `not_authenticated`, tells the app to send the user
    to the login screen, which cannot fix a signature (the owner, 2026-09-24).
    """
    return Response({"error": message, "code": "agent_auth_invalid"},
                    status=status.HTTP_401_UNAUTHORIZED)


def _client_named(identifier) -> "User":
    """The client an agent is topping up, however they named them.

    Three spellings, all of them things an agent actually types: the account number
    the client reads off the TopUp screen ("YG-204-781", in any case and with or
    without its dashes), a bare user id, or an email address. The account number is
    the one the screen shows, so it had to be accepted — before this, an agent who
    typed what the client showed them got a 404 and the client was told to find their
    email address instead.

    Raises `Http404` for anything that names nobody, exactly as the two `get_object_or_404`
    calls it replaces did, so the agent endpoints' behaviour on a bad identifier is
    unchanged. The identifier is inside the signed message on the HMAC path, so
    widening what it may say does not widen what an unauthenticated caller can do.

    A SUSPENDED OR ERASED account names nobody here (BACKEND_BUGS B-118): it is found
    only among the people `users.visibility` lets anyone see — the one predicate every
    public surface asks — and answers the same `404 not_found` a missing account does,
    so a top-up never credits it and the answer does not reveal that it exists. It used
    to be credited: the lookup had no `is_active` filter, while the public profile the
    app confirms the match through hides those accounts.
    """
    from users.visibility import public_people

    from .utils import user_id_from_account_number

    people = public_people(User.objects.all())
    user_id = user_id_from_account_number(identifier)
    if user_id is not None:
        return get_object_or_404(people, id=user_id)
    return get_object_or_404(people, email=identifier)


def _perform_topup(request, agent_profile, *, client, amount, currency, idem_key,
                   api_key=None):
    """The part of an agent top-up both endpoints share: ONE transaction, the agent's
    profile locked for its whole span (see `_topup_limit_error`'s concurrency contract).

    `api_key` is the key that signed the request on the signed endpoint. On the app's
    endpoint it is None and the agent's active key stands in: the server signs for the
    app, and a key the agent revoked stops the top-up (403 `agent_key_revoked`,
    BACKEND_BUGS B-117) until a new one is paired.

    In order: a replay of a FINISHED top-up is answered with its receipt before any cap
    is read (the cap its own money filled must not refuse it); then the agent's status,
    the revoked key, the caps and the receivable ceiling; then the key is reserved and
    the money moves. With the money, in the same transaction: the receivable entry (the
    cash the agent now holds for the platform), the key's last signature, and — once it
    commits — the client's push (BACKEND_BUGS B-115).

    200 `{reference_id, balance, currency, receipt_number, remaining_today}`: the last
    two are additive (plan step 9) — the receipt's number (AgentReceipt «رقم الإيصال»)
    and what is left of today's cap in this currency after this top-up.
    """
    from . import agents

    digest = idempotency.fingerprint(op="topup", client=client.id, amount=amount,
                                     currency=currency)
    with transaction.atomic():
        agent_profile = agents.lock_profile(agent_profile)
        replay = idempotency.finished(request.user, idem_key, digest)
        if replay is not None:
            audit("wallet.topup.idempotent_hit", request, {"key": idem_key})
            return Response(replay, status=status.HTTP_200_OK)
        if agent_profile.status != "active":
            return Response({"error": _("Agent not active"), "code": "agent_not_active"},
                            status=status.HTTP_403_FORBIDDEN)
        if api_key is None:
            if agents.key_revoked(agent_profile):
                return Response(
                    {"error": _("Your signing key was revoked. Pair this device again "
                                "to top up."),
                     "code": "agent_key_revoked"},
                    status=status.HTTP_403_FORBIDDEN)
            api_key = agents.active_key(agent_profile)

        limit_error = _topup_limit_error(agent_profile, request.user, amount, request,
                                         currency)
        if limit_error:
            return limit_error

        # --- Reserve idempotency key only after validation passes ---
        try:
            idem, replay = idempotency.reserve(request.user, idem_key, digest)
        except idempotency.IdempotencyConflict as exc:
            return _idempotency_conflict(exc)
        if replay is not None:
            audit("wallet.topup.idempotent_hit", request, {"key": idem_key})
            return Response(replay, status=status.HTTP_200_OK)

        # Never `wallet_for(..., currency)`: arriving money does not choose a person's
        # currency. `currency=` makes the ledger refuse a wallet that changed unit since
        # the view's check.
        client_wallet = wallet_for(client, "client")
        metadata = {"agent_id": request.user.id}
        if api_key is not None:
            metadata["api_key_id"] = api_key.key_id
        tx = move_funds_atomic(None, client_wallet, amount, actor_id=request.user.id,
                               tx_type="topup", currency=currency,
                               money_type=MoneyType.WALLET_MOVE, metadata=metadata)
        entry = agents.post_topup(agent_profile, tx)
        if api_key is not None:
            agents.mark_signed(api_key)
        client_wallet.refresh_from_db()
        resp = {"reference_id": tx.reference_id, "balance": str(client_wallet.balance),
                "currency": tx.currency, "receipt_number": entry.receipt_number,
                "remaining_today": agents.money(
                    agents.usage_of(agent_profile, currency).remaining_today)}
        idempotency.complete(idem, resp)
        agents.notify_client_on_commit(tx, client, agent_profile)

    audit("wallet.topup.success", request, {"reference_id": tx.reference_id,
                                             "client_id": client.id,
                                             "amount": float(amount), "currency": currency})
    return Response(resp, status=status.HTTP_200_OK)


class WalletBalanceView(APIView):
    """`GET /api/wallet/balance/` — the caller's own wallet.

    Headers: `Authorization: Bearer <access>`. No body, no query parameters.

    Answers 200 `{id, owner, owner_type, balance, currency, created_at, updated_at, held,
    held_for, account_number, currency_locked, currency_options, held_for_trainer,
    held_for_trainer_gross, awaiting_first_program, held_for_trainer_items,
    owed_to_trainer, owed_to_trainer_items}` (see
    `WalletSerializer`, which documents every key). `held` is money in escrow for
    a coaching month — out of the balance, not spent — and `held_for` names the
    trainer it is held for; both are `0.00`/null for anyone who is not a client with a
    live hold. `account_number` is what the TopUp screen tells the client to give an
    agent.

    For a TRAINER (BACKEND_BUGS B-096), the other side of the same escrow:
    `held_for_trainer` (what the trainer will receive, net of the platform's
    commission, when each held month 1 is released by the first program; in this
    wallet's currency), `held_for_trainer_gross` (the clients' fees for those months),
    `awaiting_first_program` (how many clients are waiting for their first program) and
    `held_for_trainer_items` (one per such client, newest first). `"0.00"`, `"0.00"`,
    `0` and `[]` for anyone else. The items are not ledger rows and never appear in
    `transactions/`: nothing is written for the trainer until the money is released.

    Also for a TRAINER (the payouts review, 2026-09-24): `owed_to_trainer` and
    `owed_to_trainer_items` — a month already paid out whose trainer's share is still in
    escrow, owed, because it crosses a currency and the operator's rate was missing or
    stale when it was paid (month 1 or a renewal). It is not held and not yet a ledger
    row; when the share is paid the item goes and a `trainer_fee_payout` row arrives.
    Items `{period_id, relation_id, client, status: "payout_owed", kind:
    "trainer_fee_payout", reason, month, owed_since, starts_at, amount, commission,
    commission_rate, net, currency, charged_amount, charged_currency, estimated}`,
    newest first. `"0.00"` and `[]` for anyone else. The four held keys above keep
    their meaning: nothing owed is counted in them.

    Refusals: 401 `not_authenticated` / `token_not_valid` (the framework's).

    A read of what the caller already has, with no price and no way to buy anything,
    so it takes no `X-App-Channel` and is identical on every build — the trainer's
    held money included: it is money a client already paid (spec §3: the balance and
    the history are visible on every build).

    Also `available`, `settling`, `settling_until` and `in_withdrawal` (BACKEND_BUGS
    B-097; `wallet.settling.WalletSettlingFields`): every row of a kind in
    `wallet.settling.SETTLING_KINDS` — a coaching payout (`trainer_fee_payout`) and a
    program sale (`routine_product_sale`; BACKEND_BUGS B-138) — is `settling` for
    `PAYOUT_HOLD_DAYS` after its release, `available` is `balance - settling` (what a
    withdrawal may take), `settling_until` is when the next settling payout becomes
    available (ISO 8601, or null), and `in_withdrawal` is the money of the trainer's
    open withdrawal, which has already left `balance`.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet, _created = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": getattr(request.user, "user_type", "client")})
        audit("wallet.balance.view", request, {"wallet_id": wallet.id})
        return Response(WalletSerializer(wallet).data)


class WalletCurrencyView(APIView):
    """`GET`/`PUT /api/wallet/currency/` — the ONE currency this person trades in.

    The owner (2026-09-21): a person chooses Syrian pounds or US dollars and is held to
    it — "we force him to it" — and every service is then priced and charged to them in
    that currency, whatever the operator priced it in. There is no second field for
    this: the person's currency IS `Wallet.currency`, so the app reads it here or from
    `GET /api/wallet/balance/`, which answers the same three keys.

    GET answers `{currency, currency_locked, currency_options}`.

    PUT takes `{"currency": "SYP"|"USD"}` and answers the same three keys with 200.
    It is allowed only while the wallet is untouched — no balance and not one ledger
    leg. After that the currency is a fact every transaction already written depends
    on, and changing it would re-denominate them: the answer is 409 with
    `code: "currency_locked"`. Moving an account that already holds money is an
    exchange of the whole balance at a recorded rate, which the owner has not asked
    for.

    Also refused while the wallet is untouched but money priced in its currency is on
    its way — a plan checkout still open (or completed and not yet paid out), a
    coaching request or invitation still pending, or a coaching hold: 409 with
    `code: "currency_locked_open_checkout"` and `currency`. Switching then made the
    payment land in a wallet of the other currency and be recorded with no ledger leg.
    Answer the checkout or the request first.

    Codes: `currency_required` (400), `currency_unsupported` (400),
    `currency_locked` (409), `currency_locked_open_checkout` (409).

    A statement of what the caller holds, with no price and nothing to buy, so it
    takes no `X-App-Channel` and is identical on every build.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import wallet_is_untouched

        wallet, _created = Wallet.objects.get_or_create(
            owner=request.user,
            defaults={"owner_type": getattr(request.user, "user_type", "client")})
        return Response(self._body(wallet, wallet_is_untouched(wallet)))

    def put(self, request):
        from .currencies import CURRENCY_CODES
        from .models import CurrencyChangeBlocked, CurrencyLocked, set_currency_for

        wanted = (body_dict(request).get("currency") or "").upper()
        if not wanted:
            return Response({"error": _("Choose a currency."),
                             "code": "currency_required"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            wallet = set_currency_for(request.user, wanted,
                                      getattr(request.user, "user_type", "client"))
        except CurrencyLocked as exc:
            return Response({"error": _("Your currency cannot be changed once your "
                                        "wallet has been used."),
                             "code": exc.code, "currency": exc.currency},
                            status=status.HTTP_409_CONFLICT)
        except CurrencyChangeBlocked as exc:
            return Response({"error": _("Your currency cannot be changed while a payment "
                                        "or a coaching request is open."),
                             "code": exc.code, "currency": exc.currency},
                            status=status.HTTP_409_CONFLICT)
        except ValueError:
            return Response({"error": _("That currency is not available."),
                             "code": "currency_unsupported",
                             "currency_options": list(CURRENCY_CODES)},
                            status=status.HTTP_400_BAD_REQUEST)
        audit("wallet.currency.set", request,
              {"wallet_id": wallet.id, "currency": wallet.currency})
        return Response(self._body(wallet, True))

    @staticmethod
    def _body(wallet, unlocked):
        from .currencies import CURRENCY_CODES

        return {"currency": wallet.currency,
                "currency_locked": not unlocked,
                "currency_options": list(CURRENCY_CODES)}


def _ledger_filter_refused(exc):
    """400 with the refusal's own code: `bad_date` (+ `field`), `date_range_reversed`,
    `date_range_too_long` (+ `max_months`), `bad_money_type` (+ `money_type_options`),
    `bad_kind` (+ `kind_options`). See `wallet.ledger_reads`."""
    return Response({"error": exc.message, "code": exc.code, **exc.extra},
                    status=status.HTTP_400_BAD_REQUEST)


def _own_ledger(user):
    """`(wallet, every row of it)`: the caller's wallet, made if missing, and its rows
    newest first in a total order."""
    wallet, _created = Wallet.objects.get_or_create(
        owner=user, defaults={"owner_type": getattr(user, "user_type", "client")})
    rows = (
        Transaction.objects
        .filter(Q(source_wallet=wallet) | Q(destination_wallet=wallet))
        # ...__owner too: the serializer reads `source_wallet.owner.username`,
        # which is one hop past the end of this chain, so the ledger cost two
        # extra queries PER ROW — 50 queries for a 20-row page of the financial
        # history screen.
        .select_related("source_wallet__owner", "destination_wallet__owner")
        .order_by("-created_at", "-id")  # total order: -created_at alone repeats rows across pages
    )
    return wallet, rows


class WalletTransactionsView(generics.ListAPIView):
    """`GET /api/wallet/transactions/` — the caller's own ledger, newest first.

    Headers: `Authorization: Bearer <access>`. Answers 200 with the paginated envelope
    `{count, next, previous, results}` (`?page=`, `?page_size=`, §3), each row a
    `TransactionSerializer` row (`kind`, `reason`, `direction`, `signed_amount`,
    `counterparty`, amount, currency, `money_type`, reference, metadata).

    WHAT A ROW NAMES (BACKEND_BUGS B-184). Only what it already names to the reader:
    `actor` is the reader's own id or the `counterparty`'s, else null;
    `source_wallet` / `destination_wallet` is the reader's own wallet id on their side,
    null on the other; `source_owner` / `destination_owner` on the other side is the
    `counterparty` or null — never the platform's escrow, revenue or payout-clearing
    account (`platform_escrow_syp` used to be served on every coaching row), never a
    program's buyer; `metadata` carries only `wallet.activity.READER_METADATA_KEYS`
    (`title`, `product_title`, `routine_name`, `product_id`, `month`, `withdrawal_id`,
    `withdrawal_number`, `original_reference`) — no other account's id, no `purpose`
    (read `kind`), and never an operator's `reason` for a reversal or refund. Every key
    is still there; the console's exports read every stored column as written.

    Optional filters (BACKEND_BUGS B-101; `wallet.ledger_reads` has the rules), all
    ANDed, and without any of them the call is exactly what it was:

    * `since=YYYY-MM-DD`, `until=YYYY-MM-DD` — inclusive days in the server's zone.
    * `money_type=` — one or several of `PLATFORM_SUB`, `COACHING_1TO1`,
      `ROUTINE_PRODUCT`, `WALLET_MOVE` (repeat the parameter or separate with commas;
      several mean any of them).
    * `kind=` — one or several of the `kind` values a row carries
      (`wallet.activity.KINDS`), matched exactly as each row's `kind` is derived.

    Refusals (400, each its own code): `bad_date` (+ `field`: a date that is not
    `YYYY-MM-DD` or lies outside 2000..2999), `date_range_reversed` (`since` after
    `until`), `bad_money_type` (+ `money_type_options`), `bad_kind` (+ `kind_options`);
    401 `not_authenticated` / `token_not_valid`.

    For the monthly chart and the breakdown by kind of money, do not page this: read
    `transactions/summary/`.

    Was an APIView returning a bare JSON array hard-sliced at [:200]: the only list
    endpoint in the API without the {count, next, previous, results} envelope, and a
    user with more than 200 transactions could never reach the older ones — there was
    no next link and nothing said the list had been truncated. It is a financial
    ledger; silently hiding rows is not acceptable.

    A row that moved a trainer's withdrawal money, read by that trainer, carries
    `withdrawal`: `{id, number, state, arrived, destination_kind, destination_label}` —
    the withdrawal's state NOW and where the money went (the last four characters of a
    ShamCash address, or the agent's shop name); `null` on every other row
    (`payouts/ledger_fields.py`, BACKEND_BUGS B-125). One query per page.

    A row that moved a routine product's money — the buyer's
    `routine_product_purchase`, the trainer's `routine_product_sale`, a refund —
    carries `product`: `{id, title}`, the program, its title in the request's language
    (the other one when that is empty); `null` on every other row
    (`routine/ledger_fields.py`, BACKEND_BUGS B-169). One query per page.

    Each row also carries `available_at` and `settling` (BACKEND_BUGS B-097;
    `wallet.settling.TransactionSettlingFields`): on a row of a kind in
    `wallet.settling.SETTLING_KINDS` read by the trainer it paid — a coaching payout
    (`trainer_fee_payout`) or a program sale (`routine_product_sale`; BACKEND_BUGS
    B-138) — when its money becomes withdrawable and whether that is still ahead;
    `null` / `false` on every other row. A trainer's withdrawal reads as `kind`
    `withdrawal`, and its money coming back as `withdrawal_returned`.

    And `note` (BACKEND_BUGS B-163; `wallet.transfer_notes`): on a client's payment to a
    trainer, read by either of the two from their own row (`trainer_transfer` for the
    client, `transfer_received` for the trainer), the line the client wrote to the
    trainer, or null when there is none or it was erased with an account; null on
    every other row. One query per page.

    A record of money that already moved: no channel header, identical on every build.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = TransactionSerializer

    def get_queryset(self):
        wallet, rows = _own_ledger(self.request.user)
        self._wallet = wallet
        filters = getattr(self, "_filters", None)
        if filters is not None:
            rows = filters.apply(rows, wallet)
        return rows

    def get_serializer_context(self):
        """The reader's own wallet, so the serializer can say which way each row went.

        `get_queryset` has already run by the time DRF builds the context for a list,
        but not for a plain `get_serializer(...)`, so the wallet is resolved here
        rather than read off `self._wallet`. `ledger_owner_ids` is looked up once per
        response instead of once per row — escrow is the other side of every coaching
        movement, so every row would otherwise ask.
        """
        from .activity import ledger_owner_ids

        context = super().get_serializer_context()
        wallet = getattr(self, "_wallet", None)
        if wallet is None:
            wallet, _created = Wallet.objects.get_or_create(
                owner=self.request.user,
                defaults={"owner_type": getattr(self.request.user, "user_type", "client")},
            )
            self._wallet = wallet
        context["wallet"] = wallet
        context["ledger_owner_ids"] = ledger_owner_ids()
        return context

    def list(self, request, *args, **kwargs):
        from .ledger_reads import LedgerFilterRefused, parse_filters

        try:
            self._filters = parse_filters(request.query_params)
        except LedgerFilterRefused as exc:
            return _ledger_filter_refused(exc)
        response = super().list(request, *args, **kwargs)
        # Audit the access, not the row count: counting used to run a second COUNT
        # query over the whole ledger on every view.
        audit("wallet.transactions.view", request, {"wallet_id": self._wallet.id})
        return response


class WalletTransactionsSummaryView(APIView):
    """`GET /api/wallet/transactions/summary/` — the caller's own ledger summed by
    calendar month and by kind of money (BACKEND_BUGS B-101).

    TrainerEarnings' monthly chart («أرباحك الشهرية») and «من أين أرباح أيلول» (coaching,
    the trainer's share of plans, programs sold) without paging six months of rows:
    two aggregate queries (plus the wallet and the platform's ledger owners) whatever
    the number of rows.

    Headers: `Authorization: Bearer <access>`. Query, all optional:
    `since=YYYY-MM-DD` / `until=YYYY-MM-DD` (inclusive, server's zone; default: this
    month and the five before it, up to today), `money_type=` and `kind=` exactly as on
    `transactions/`. The window may touch at most 12 calendar months.

    200 `{currency, since, until, money_type, kind, months, totals}`:
    `currency` is the wallet's, and every amount is in it (a string, two decimals).
    `months` lists every calendar month the window touches, oldest first, even an empty
    one: `{month: "2026-09", in, out, net, count, by_money_type}`; `by_money_type` always
    has the four keys `PLATFORM_SUB`, `COACHING_1TO1`, `ROUTINE_PRODUCT`, `WALLET_MOVE`,
    each `{in, out, net, count, people}`. `totals` is the same over the whole window,
    with `people` counted across it. `in` reached this wallet, `out` left it, `net` =
    `in` - `out`; `count` is ledger rows; `people` is how many different people those
    rows were with (the row's `counterparty`; escrow and the platform are nobody). For a
    trainer, `COACHING_1TO1.in` is coaching income and its `people` the clients it came
    from, `PLATFORM_SUB.in` their share of clients' plans, `ROUTINE_PRODUCT.in` program
    sales. `since`/`until` echo the window used; `money_type`/`kind` the filters.

    Refusals (400, each its own code): `bad_date` (+ `field`), `date_range_reversed`,
    `date_range_too_long` (+ `max_months`: 12), `bad_money_type`
    (+ `money_type_options`), `bad_kind` (+ `kind_options`); 401 `not_authenticated` /
    `token_not_valid`.

    A record of money that already moved: no channel header, identical on every build.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .ledger_reads import (LedgerFilterRefused, monthly_summary, parse_filters,
                                   summary_window)

        try:
            filters = summary_window(parse_filters(request.query_params))
        except LedgerFilterRefused as exc:
            return _ledger_filter_refused(exc)
        wallet, rows = _own_ledger(request.user)
        body = monthly_summary(rows, wallet, filters)
        audit("wallet.transactions.summary", request, {"wallet_id": wallet.id})
        return Response(body)


class WalletTransactionDetailView(generics.RetrieveAPIView):
    """`GET /api/wallet/transactions/{reference_id}/` — one row of the caller's ledger.

    The same shape as a row of the list (`TransactionSerializer`), so the activity
    detail sheet needs no second contract: `kind`, `reason`, `direction`,
    `signed_amount`, `counterparty`, the amount and currency, `money_type`, the
    reference and `metadata` — naming only what the row already names to the reader,
    exactly as a row of the list does (BACKEND_BUGS B-184; `WalletTransactionsView`).

    Looked up by `reference_id` — the ledger's own public identifier, which is what
    every row, receipt and support conversation already quotes — and scoped to the
    caller's wallet on both sides, so a reference belonging to someone else answers
    `404 not_found`, byte-identical to one that does not exist.

    A record of money that has already moved: no channel header, identical on every
    build.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = TransactionSerializer
    lookup_field = "reference_id"

    def get_queryset(self):
        wallet, _created = Wallet.objects.get_or_create(
            owner=self.request.user,
            defaults={"owner_type": getattr(self.request.user, "user_type", "client")},
        )
        self._wallet = wallet
        return (
            Transaction.objects
            .filter(Q(source_wallet=wallet) | Q(destination_wallet=wallet))
            .select_related("source_wallet__owner", "destination_wallet__owner")
        )

    def get_serializer_context(self):
        from .activity import ledger_owner_ids

        context = super().get_serializer_context()
        context["wallet"] = getattr(self, "_wallet", None)
        context["ledger_owner_ids"] = ledger_owner_ids()
        return context

    def retrieve(self, request, *args, **kwargs):
        try:
            response = super().retrieve(request, *args, **kwargs)
        except Http404:
            return Response({"error": _("No such transaction"), "code": "not_found"},
                            status=status.HTTP_404_NOT_FOUND)
        audit("wallet.transaction.view", request,
              {"reference_id": kwargs.get("reference_id")})
        return response


class AgentDirectoryView(generics.ListAPIView):
    """`GET /api/wallet/agents/` — the agents a client can hand cash to.

    "وكلاء قريبون" on the TopUp screen. Answers the paginated envelope with
    `{id, name, city, area, hours, phone}` per agent: the shop's name, the district,
    the opening hours as the operator wrote them, and the line to call.

    Only agents an operator has LISTED (`is_listed`) and whose account is `active`
    appear; an agent who has not agreed to be published is not published, and one who
    is suspended is not sent a customer. "Active" is both the agent profile's `status`
    AND the person's account (`users/visibility.py`): an agent who deleted their account,
    or whose account was suspended, stayed listed — shop name, city and phone — while
    no top-up they could make would ever reach a client (R4, 2026-11-23). `?city=` filters, case-insensitively, to the
    city heading the screen shows.

    Paying an agent cash is not an in-app purchase of anything — it is how money gets
    into a wallet in a country where cards do not work — and the spec says exactly
    that of the store builds: "top-up is done with an agent in person, so there is
    nothing to hide". So this takes no channel header and answers the same list on
    every build. Authenticated: the directory is for customers, not for scraping.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = AgentListingSerializer

    def get_queryset(self):
        from users.visibility import public_q

        qs = (AgentProfile.objects
              .filter(is_listed=True, status="active")
              .filter(public_q("user"))
              .select_related("user")
              .order_by("city", "display_name", "id"))
        city = self.request.query_params.get("city")
        if city:
            qs = qs.filter(city__iexact=city.strip())
        return qs


class AgentTopUpView(APIView):
    """`POST /api/wallet/agent/topup/` — an agent's server credits a client's wallet
    with cash taken in person, signed with the agent's key (`X-Agent-Auth`).

    Body: `client_identifier` (account number "YG-204-781", user id or email),
    `amount`, `currency` ("SYP" | "USD" — the currency of the CASH, required),
    `idempotency_key`, `timestamp`, `signature`. The signed message is
    `client_identifier|amount|timestamp|idempotency_key|currency`.

    200 `{reference_id, balance, currency, receipt_number, remaining_today}` (the last
    two since plan step 9: see `_perform_topup`). A replay of a finished request with
    the same `idempotency_key` and body answers the same 200 again, before any cap is
    read. Refusals (branch on `code`):

    | Status | `code` | Meaning |
    |---|---|---|
    | 400 | `validation_error` | a body field is missing or malformed; a missing or unknown `currency` is `field_errors.currency` |
    | 401 | `not_authenticated` / `token_not_valid` | no JWT, or a JWT that is not valid (the framework's answer) |
    | 401 | `agent_auth_invalid` | `X-Agent-Auth` is missing, malformed, stale, names no active key (a revoked key is not active), names a key that is not the caller's, or its timestamp or signature does not match the body. One code for all of them, on purpose: the code adds no way to tell an unknown key from a bad signature |
    | 403 | `agents_only` | the caller is not an agent |
    | 403 | `agent_ip_not_allowed` | the request did not come from an address on the agent's allow-list |
    | 403 | `agent_not_active` | the agent's profile is suspended |
    | 404 | `not_found` | `client_identifier` names nobody — or a suspended or erased account, which answers exactly as a missing one (BACKEND_BUGS B-118) |
    | 400 | `target_not_a_client` | it names an account that is not a client |
    | 409 | `currency_mismatch` | the client trades in the other currency; `client_currency` and `topup_currency` say which |
    | 429 | `per_transaction_limit_exceeded` | more than one top-up may be in that currency |
    | 429 | `daily_limit_exceeded` / `monthly_limit_exceeded` | the agent's cap FOR THAT CURRENCY; waiting does not lift it before the day or month turns |
    | 429 | `receivable_ceiling_exceeded` | the cash the agent holds for the platform, unsettled, would pass their ceiling; an operator's settlement lifts it |
    | 409 | `request_in_flight` | the first request with this `idempotency_key` is still running; retry when it finishes |
    | 422 | `idempotency_key_reused` | the key was used for a different request; retrying never helps |
    | 429 | `throttled` | the rate limiter (`Retry-After`) |

    The four cap refusals also carry `limit`, `used` (null for the per-top-up cap),
    `remaining` and `currency` (BACKEND_BUGS B-124). Every code above that is not the
    framework's was the status default (`bad_request`, `not_authenticated`,
    `permission_denied`, `conflict`, `unprocessable`, `throttled`) until the owner's
    decision of 2026-09-24.

    A top-up posts the cash to the agent's receivable, stamps the signing key's
    `last_signature_at`, and pushes the client `wallet_topped_up` (API_CONTRACT §4).

    Server-to-server: no build is involved, so nothing here depends on one.
    """
    permission_classes = [IsAuthenticated, IsAgent]
    throttle_classes = [ChargingRateThrottle]

    def post(self, request):
        serializer = TopUpRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        auth_header = request.META.get("HTTP_X_AGENT_AUTH")
        parsed = parse_agent_auth_header(auth_header)
        if not parsed:
            audit("wallet.topup.auth_failed", request, {"reason": "invalid_or_stale_auth"})
            return _agent_auth_refused(_("Invalid agent auth"))

        # Freshness is checked on the SIGNED timestamp, and the two must be the same one.
        # The header's timestamp was the one checked and the body's was the one signed, so
        # they were independent: an attacker who captured a valid top-up replayed the
        # identical body and signature with the header's timestamp rewritten to now. The
        # signature still verified — the signed message had not changed — and freshness
        # passed. The only thing standing in the way was the IdempotencyKey row, which the
        # retention job deletes after 30 days, after which the same capture mints funds
        # again, indefinitely.
        signed_timestamp = str(serializer.validated_data["timestamp"])
        if str(parsed.timestamp) != signed_timestamp:
            audit("wallet.topup.auth_failed", request, {"reason": "timestamp_mismatch"})
            return _agent_auth_refused(_("Invalid agent auth"))
        if not is_fresh_timestamp(signed_timestamp):
            audit("wallet.topup.auth_failed", request, {"reason": "invalid_or_stale_auth"})
            return _agent_auth_refused(_("Invalid agent auth"))

        try:
            api_key = AgentAPIKey.objects.select_related("agent").get(key_id=parsed.key_id, is_active=True)
        except AgentAPIKey.DoesNotExist:
            audit("wallet.topup.auth_failed", request, {"reason": "unknown_key"})
            return _agent_auth_refused(_("Invalid credentials"))
        # The key must be the CALLER's. It was looked up by id alone, so one agent's JWT
        # with another agent's key and secret topped up under the first agent's caps and
        # receivable while stamping the other's key as having signed (plan step 9).
        if api_key.agent.user_id != request.user.id:
            audit("wallet.topup.auth_failed", request, {"reason": "key_not_callers"})
            return _agent_auth_refused(_("Invalid credentials"))

        # The client's address as the trusted proxy chain gives it. REMOTE_ADDR is the
        # proxy behind Fly, so this refused every agent with an allow-list (and allowing
        # the proxy's address instead would have let everyone through). Compared
        # normalised, so `2001:DB8::1` in the list matches `2001:db8::1` on the wire.
        ip = get_recordable_client_ip(request)
        if api_key.agent.ip_allowlist and (ip is None or ip not in _normalised_allowlist(
                api_key.agent.ip_allowlist)):
            audit("wallet.topup.denied_ip", request, {"ip": ip})
            return Response({"error": _("IP not allowed"), "code": "agent_ip_not_allowed"},
                            status=status.HTTP_403_FORBIDDEN)

        # Verify HMAC against the RAW secret (recovered from encrypted storage) —
        # never against the stored digest, which would itself be a bearer signer.
        raw_secret = decrypt_secret(api_key.secret_ciphertext) if api_key.secret_ciphertext else None
        # The currency is SIGNED: it decides what the amount is worth, so an unsigned
        # one would let the same signature mint dollars instead of pounds.
        message = f"{serializer.validated_data['client_identifier']}|{serializer.validated_data['amount']}|{serializer.validated_data['timestamp']}|{serializer.validated_data['idempotency_key']}|{serializer.validated_data['currency']}"
        if not raw_secret or not verify_hmac_signature(raw_secret, message, parsed.signature):
            audit("wallet.topup.auth_failed", request, {"reason": "bad_signature"})
            return _agent_auth_refused(_("Signature verification failed"))

        # --- Pre-side-effect validation (runs BEFORE reserving the idempotency
        #     key, so a validation failure never permanently bricks retries) ---
        ident = serializer.validated_data["client_identifier"]
        client = _client_named(ident)
        if getattr(client, "user_type", None) != "client":
            return Response({"error": _("Target must be a client"),
                             "code": "target_not_a_client"},
                            status=status.HTTP_400_BAD_REQUEST)

        agent_profile = ensure_agent_profile(request.user)
        amount = serializer.validated_data["amount"]
        currency = serializer.validated_data["currency"]
        idem_key = serializer.validated_data["idempotency_key"]
        mismatch = _topup_currency_mismatch(client, currency)
        if mismatch is not None:
            return mismatch

        # Cap check and ledger write share ONE transaction, with the agent's profile
        # locked for its whole span. See _topup_limit_error's concurrency contract.
        return _perform_topup(request, agent_profile, client=client, amount=amount,
                              currency=currency, idem_key=idem_key, api_key=api_key)


class ClientTransferToTrainerView(APIView):
    """`POST /api/wallet/client/transfer/` — a client pays a trainer from the wallet
    (the design's SendMoney: «للرسوم الإضافية أو الجلسات الخاصة التي اتفقتما عليها»).

    Body: `trainer_id`, `amount`, `idempotency_key`, and optionally `note` (BACKEND_BUGS
    B-163) — a line to the trainer of at most 140 characters ("جلسة الجمعة الإضافية").
    Headers: `X-App-Channel`.

    200 `{reference_id, client_balance, trainer_balance}`. `reference_id` is the
    client's own ledger row (the one `wallet/transactions/{reference_id}/` opens);
    `client_balance` is the caller's wallet after the payment. `trainer_balance` is NOT
    the trainer's wallet: it is what THIS payment gave the trainer — `amount` less the
    platform's commission, the `amount` of the trainer's `transfer_received` row ("0.00"
    when the commission took it all). It was the trainer's whole balance, which told
    any client the trainer's earnings for 0.01 (the review of wave 1, 2026-10-07); the
    key is kept because the contract is additive. A replay of a request answered before
    the change answers what it answered then.

    A COACHING PAYMENT (the owner, 2026-09-28; BACKEND_BUGS B-163). The client pays
    `amount`; the platform keeps its coaching commission (`COACHING_COMMISSION_RATE`,
    20%, copied onto the transfer when it is made) and the trainer receives the rest at
    once — «تأخذ المنصة عمولتها منه لا منك». Written exactly as a coaching month is
    paid out (`users.coaching.pay_out`): client -> escrow `amount`, escrow -> trainer
    the share, escrow -> platform revenue the commission (`coaching_commission`), all
    `COACHING_1TO1`, in one transaction (`wallet.models.CoachingTransfer` records the
    three). The client's ledger shows one row, `trainer_transfer`, of `amount`; the
    trainer's one row, `transfer_received`, of their share — the kinds they always
    were. The share is not held back: it is withdrawable at once (not a settling kind).
    Transfers made before this were one WALLET_MOVE row, client -> trainer, and stay so.

    The note is shown to both people as `note` on their own row of `wallet/transactions/`
    and `/{reference_id}/`. It is user-written text, so it is checked against the
    banned terms (`400 content_not_allowed`, nothing moves) and kept beside the ledger,
    never in it: it is erased with either person's account, while the ledger is kept.

    A BLOCK CLOSES IT (`moderation.services`; the review of wave 1, 2026-10-07). A
    transfer is a line of text and a name in front of the other person, so when either
    of the two has blocked the other it is refused, `403 user_blocked`, before anything
    moves or the key is reserved — the answer `client/request-trainer/`, `follows/` and
    `notifications/message-client/` give, the same in both directions. And a note sent
    before the block is not shown while it stands: `note` reads null on both rows
    (`wallet/transfer_notes.py`), as an invitation's note does.

    Refusals (branch on `code`):

    | Status | `code` | Meaning |
    |---|---|---|
    | 400 | `app_channel_required` | no known `X-App-Channel` |
    | 403 | `not_available_on_channel` | this build may not move wallet money, or may not pay for coaching (both are asked: the money moved is coaching money) |
    | 400 | `validation_error` | a body field is missing or malformed (`field_errors`; a `note` over 140 characters is one) |
    | 403 | `clients_only` | the caller is not a client (the code the coaching and diet routes answer for the same fact) |
    | 404 | `not_found` | `trainer_id` names no trainer |
    | 403 | `user_blocked` | either of the two has blocked the other (it does not say which); nothing moved |
    | 409 | `currency_mismatch` | the two trade in different currencies (nothing is converted on a transfer); `client_currency` and `trainer_currency` say which |
    | 400 | `content_not_allowed` | the `note` holds a banned term (`field_errors.note`); nothing moved |
    | 402 | `insufficient_balance` | the client's wallet holds less than `amount`; `currency` is its currency |
    | 400 | `transfer_refused` | the ledger refused the movement for any other reason; nothing moved |
    | 409 | `request_in_flight` | the first request with this `idempotency_key` is still running |
    | 422 | `idempotency_key_reused` | the key was used for a different request (a different `note` is a different request) |
    | 429 | `throttled` | the rate limiter (`Retry-After`) |

    `clients_only`, `transfer_refused`, `request_in_flight` and `idempotency_key_reused`
    answered only their status defaults until the owner's decision of 2026-09-24.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ChargingRateThrottle]

    def post(self, request):
        # Before anything else: a build that may not move wallet money is told so with
        # the same code whatever it sent. See policy/services.py.
        require_purchase_allowed(request, MoneyType.WALLET_MOVE)
        # And the money it moves is coaching money (B-163): a build that may not pay
        # for coaching may not pay a trainer this way either. Decided by the build only.
        require_purchase_allowed(request, MoneyType.COACHING_1TO1)

        serializer = TransferRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # --- Pre-side-effect validation before reserving the idempotency key ---
        if getattr(request.user, "user_type", None) != "client":
            return Response({"error": _("Only clients can initiate transfers"),
                             "code": "clients_only"}, status=status.HTTP_403_FORBIDDEN)

        trainer = get_object_or_404(User, id=serializer.validated_data["trainer_id"], user_type="trainer")

        # A block closes this channel (the review of wave 1): the note and the client's
        # name both land in front of the trainer, so a blocked pair cannot transfer at
        # all. Before the key is reserved and before any wallet is touched; one answer
        # whichever side blocked (`moderation.exceptions.UserIsBlocked`).
        from moderation.exceptions import UserIsBlocked
        from moderation.services import is_blocked

        if is_blocked(request.user, trainer):
            raise UserIsBlocked()

        # Before the key is reserved: a transfer converts nothing, so two people in
        # different currencies cannot send each other money, and the app is told so
        # with a code rather than a bare 400 (SIDE_BUGS S-160).
        from .models import currency_for
        client_currency, trainer_currency = currency_for(request.user), currency_for(trainer)
        if client_currency != trainer_currency:
            return Response({"error": _("You and this trainer use different currencies."),
                             "code": "currency_mismatch",
                             "client_currency": client_currency,
                             "trainer_currency": trainer_currency},
                            status=status.HTTP_409_CONFLICT)

        # The note is user-written text the trainer will read (B-163): the banned terms
        # refuse it before the key is reserved, so a corrected note can reuse the key.
        note = (serializer.validated_data.get("note") or "").strip()
        from moderation.ugc_filter import refuse_banned_text

        refuse_banned_text({"note": note})

        idem_key = serializer.validated_data["idempotency_key"]
        try:
            idem, replay = idempotency.reserve(
                request.user, idem_key,
                idempotency.fingerprint(
                    op="transfer", trainer=trainer.id,
                    amount=serializer.validated_data["amount"],
                    # Only when there is one: a transfer without a note hashes exactly
                    # as it did before notes existed, so a retry across a release still
                    # replays instead of answering 422.
                    **({"note": note} if note else {}),
                ),
            )
        except idempotency.IdempotencyConflict as exc:
            return _idempotency_conflict(exc)
        if replay is not None:
            audit("wallet.transfer.idempotent_hit", request, {"key": idem_key})
            return Response(replay, status=status.HTTP_200_OK)

        client_wallet, _created = Wallet.objects.get_or_create(owner=request.user, defaults={"owner_type": "client"})
        trainer_wallet, _created = Wallet.objects.get_or_create(owner=trainer, defaults={"owner_type": "trainer"})

        from .models import CurrencyMismatch, InsufficientBalance

        amount = serializer.validated_data["amount"]
        try:
            charge, share = _pay_trainer(request.user, trainer, client_wallet,
                                         trainer_wallet, amount, note)
        except InsufficientBalance:
            idempotency.release(idem)
            return Response({"error": _("Insufficient wallet balance."),
                             "code": "insufficient_balance",
                             "currency": client_wallet.currency},
                            status=status.HTTP_402_PAYMENT_REQUIRED)
        except CurrencyMismatch:
            # A wallet changed unit between the check above and the lock.
            idempotency.release(idem)
            return Response({"error": _("You and this trainer use different currencies."),
                             "code": "currency_mismatch",
                             "client_currency": currency_for(request.user),
                             "trainer_currency": currency_for(trainer)},
                            status=status.HTTP_409_CONFLICT)
        except ValueError as e:
            # No funds moved (atomic rollback) — release the key so the caller can retry.
            idempotency.release(idem)
            return Response({"error": _("Request could not be completed."),
                             "code": "transfer_refused"}, status=status.HTTP_400_BAD_REQUEST)
        client_wallet.refresh_from_db()
        # `trainer_balance` is this payment's share, never the trainer's wallet: the
        # payer has no business reading another person's balance (see the docstring).
        resp = {"reference_id": charge.reference_id,
                "client_balance": str(client_wallet.balance),
                "trainer_balance": str(share)}
        idempotency.complete(idem, resp)

        # Never the note: the audit log is kept through an erasure, the note is not.
        audit("wallet.transfer.success", request, {"reference_id": charge.reference_id, "trainer_id": trainer.id, "amount": float(amount)})
        return Response(resp, status=status.HTTP_200_OK)


def _pay_trainer(client, trainer, client_wallet, trainer_wallet, amount, note):
    """A client's direct payment to a trainer, written as a coaching payout (BACKEND_BUGS
    B-163; see `ClientTransferToTrainerView` and `wallet.models.CoachingTransfer`).

    Returns `(charge, share)`: the client's leg, and what the trainer received (the
    response's `trainer_balance`). Raises what `move_funds_atomic` raises, with nothing
    moved. The three legs carry `coaching_transfer` ("charge" / "payout" /
    "commission"), and the two after the charge `transfer_reference` (the charge's), so
    a row says by itself that it is one leg of a transfer: the generic admin reversal
    reads that and never takes one leg alone (`_coaching_transfer_leg`).

    Calls `move_funds_atomic` through this module, like the view always did.
    """
    from users.coaching import commission_rate, lock_wallets, split

    from .models import CoachingTransfer
    from .utils import get_escrow_wallet, get_platform_revenue_wallet

    money = client_wallet.currency
    rate = commission_rate()
    commission, share = split(amount, rate)
    common = {"trainer_id": trainer.id, "client_id": client.id}
    kind = MoneyType.COACHING_1TO1
    with transaction.atomic():
        escrow = get_escrow_wallet(money)
        platform = get_platform_revenue_wallet(money)
        # The client's user row, then every wallet in ascending id, all at once: the
        # lock order of a coaching renewal (`users/coaching.py`).
        lock_wallets(client.id, [client_wallet, escrow, trainer_wallet, platform])
        charge = move_funds_atomic(
            client_wallet, escrow, amount, actor_id=client.id, tx_type="transfer",
            currency=money, money_type=kind,
            metadata={**common, "coaching_transfer": "charge", "rate": str(rate)})
        legs = {"transfer_reference": charge.reference_id}
        payout = fee = None
        if share > 0:
            payout = move_funds_atomic(
                escrow, trainer_wallet, share, actor_id=client.id, tx_type="transfer",
                currency=money, money_type=kind,
                metadata={**common, "coaching_transfer": "payout", **legs})
        if commission > 0:
            fee = move_funds_atomic(
                escrow, platform, commission, actor_id=client.id, tx_type="transfer",
                currency=money, money_type=kind,
                metadata={"purpose": "coaching_commission", "rate": str(rate), **common,
                          "coaching_transfer": "commission", **legs})
        CoachingTransfer.objects.create(
            client=client, trainer=trainer, amount=amount, currency=money,
            commission_rate=rate, commission=commission, trainer_share=share,
            charge_reference=charge.reference_id,
            payout_reference=payout.reference_id if payout else "",
            commission_reference=fee.reference_id if fee else "", note=note)
    return charge, share


def _coaching_transfer_leg(original):
    """409 `coaching_transfer_leg` when `original` is the trainer's or the platform's leg
    of a client's coaching transfer (B-163), else None. Decided on what the row says of
    itself, which never changes. The CHARGE — the client's leg, the reference the
    transfer answered — is not refused: reversing it puts the whole transfer back
    (`_reverse_coaching_transfer`)."""
    metadata = original.metadata if isinstance(original.metadata, dict) else {}
    if metadata.get("coaching_transfer") not in ("payout", "commission"):
        return None
    return Response({"error": _("This is one leg of a payment to a trainer and cannot be "
                                "reversed on its own. Reverse the payment by its "
                                "reference instead."),
                     "code": "coaching_transfer_leg",
                     "transfer_reference": metadata.get("transfer_reference")},
                    status=status.HTTP_409_CONFLICT)


def _reverse_coaching_transfer(charge, *, actor_id, reason):
    """Put a client's coaching transfer back, every leg together, in one transaction:
    the trainer's share and the platform's commission back to escrow, then the whole
    amount back to the client. Returns the reversal of the charge. Raises what
    `move_funds_atomic` raises (`InsufficientBalance` when the trainer has spent the
    share, `AlreadyReversed`), with nothing moved."""
    from users.coaching import lock_wallets

    from .models import CoachingTransfer

    record = CoachingTransfer.objects.filter(charge_reference=charge.reference_id).first()
    if record is not None:
        refs = [r for r in (record.payout_reference, record.commission_reference) if r]
        legs = Transaction.objects.filter(reference_id__in=refs)
    else:
        # Written in the same transaction as the legs, so never missing; if it were,
        # the legs still name their charge, and the charge is never reversed alone.
        legs = Transaction.objects.filter(
            metadata__transfer_reference=charge.reference_id,
            metadata__coaching_transfer__in=["payout", "commission"])
    legs = list(legs.select_related("source_wallet", "destination_wallet").order_by("id"))
    escrow = charge.destination_wallet
    common = {"reason": reason}
    with transaction.atomic():
        lock_wallets(actor_id, [charge.source_wallet, escrow,
                                *(leg.destination_wallet for leg in legs)])
        for leg in legs:
            move_funds_atomic(leg.destination_wallet, escrow, leg.amount, actor_id=actor_id,
                              tx_type="reversal", reverses=leg, currency=leg.currency,
                              money_type=leg.money_type,
                              metadata={"original_reference": leg.reference_id, **common})
        return move_funds_atomic(escrow, charge.source_wallet, charge.amount,
                                 actor_id=actor_id, tx_type="reversal", reverses=charge,
                                 currency=charge.currency, money_type=charge.money_type,
                                 metadata={"original_reference": charge.reference_id,
                                           **common})


def _program_sale_leg(original):
    """409 `routine_product_leg` when `original` is a leg of a routine product's sale (or
    of its refund), else None (SIDE_BUGS S-237; see `AdminReversalView`).

    Read off what the row says of itself, which never changes: a `purchase_id` in its
    metadata (every leg `routine/products.py` writes carries one) or a `routine_product_`
    purpose, so a leg whose purchase id went missing is refused all the same."""
    metadata = original.metadata or {}
    purpose = metadata.get("purpose")
    purchase_id = metadata.get("purchase_id")
    if not purchase_id and not (isinstance(purpose, str)
                                and purpose.startswith("routine_product_")):
        return None
    from routine.models import RoutinePurchase

    purchase = None
    if isinstance(purchase_id, int) or (isinstance(purchase_id, str) and purchase_id.isdigit()):
        purchase = (RoutinePurchase.objects.filter(pk=int(purchase_id))
                    .values("id", "status", "paid_with").first())
    return Response({"error": _("This is one leg of a program sale and cannot be reversed "
                                "on its own. Refund the purchase instead."),
                     "code": "routine_product_leg", "purchase": purchase},
                    status=status.HTTP_409_CONFLICT)


def _plan_payment_leg(original):
    """409 `plan_payment_leg` when `original` is the ledger row of a plan paid from the
    wallet (Road to 10 S2.13), else None (see `AdminReversalView`).

    That row is the WHOLE payment: reversed on its own, the money went back while the
    payment stayed `completed` and the plan stayed active — a free plan. Undoing it is
    the payment's refund, which takes back the cover and puts this row back together.
    Read off what the row says of itself, which never changes."""
    metadata = original.metadata or {}
    if metadata.get("paid_with") != "wallet" or not metadata.get("original_payment_id"):
        return None
    return Response({"error": _("This is a plan paid from the wallet and cannot be "
                                "reversed on its own. Refund the payment instead."),
                     "code": "plan_payment_leg",
                     "payment_id": str(metadata["original_payment_id"])},
                    status=status.HTTP_409_CONFLICT)


class AdminReversalView(APIView):
    """`POST /api/wallet/admin/reversal/` — support puts one ledger row back.

    Body: `reference_id`, `idempotency_key`, optional `reason`. 200 `{reference_id}`.
    Refusals (branch on `code`):

    | Status | `code` | Meaning |
    |---|---|---|
    | 403 | `admins_only` | the caller is not an operator |
    | 400 | `validation_error` | a body field is missing or malformed (`field_errors`) |
    | 404 | `not_found` | no transaction has that `reference_id` |
    | 409 | `already_reversed` | it has been reversed already |
    | 409 | `not_reversible` | it is itself a reversal |
    | 409 | `exchange_leg` | it is one half of a currency exchange (see below); `exchange_reference` names the pair |
    | 409 | `withdrawal_leg` | it is one leg of a trainer's withdrawal (see below); `withdrawal` `{id, number, state}` names it, or is null if no withdrawal names the row any more |
    | 409 | `routine_product_leg` | it is one leg of a routine product's sale or of its refund (see below); `purchase` `{id, status, paid_with}` names it, or is null if no purchase has that id any more |
    | 409 | `coaching_transfer_leg` | it is the trainer's or the platform's leg of a client's payment to a trainer (see below); `transfer_reference` is the payment's own reference, which reverses it whole |
    | 409 | `plan_payment_leg` | it is the ledger row of a plan paid from the wallet (Road to 10 S2.13): refund the payment (`payment_id`) instead, which also takes back the cover |
    | 400 | `insufficient_balance` | the wallet that received the money no longer holds it, so it cannot be taken back |
    | 400 | `currency_mismatch` | a wallet on either side changed currency since the row was written |
    | 400 | `reversal_refused` | the ledger refused the reversal for any other reason; nothing moved |
    | 409 | `request_in_flight` | the first request with this `idempotency_key` is still running |
    | 422 | `idempotency_key_reused` | the key was used for a different request |

    The three 400s were one `bad_request` until the owner's decision of 2026-09-24,
    which gave each reason its own code; nothing else about them changed.

    ONE LEG OF AN EXCHANGE IS REFUSED (SIDE_BUGS S-161). A cross-currency coaching
    payout is two rows — a debit out of escrow in the client's currency and a credit
    into the trainer's wallet in theirs — tied by `exchange_reference`. Reversing only
    the credit burned the trainer's money while the escrow debit stayed burned too;
    reversing only the debit minted escrow money whose trainer side was never taken
    back; and either way the coaching month still read as paid. Undoing an exchange is
    a decision about both legs and the month, and this endpoint does not make it.

    NO LEG OF A WITHDRAWAL IS REVERSED (the payouts review, 2026-09-24). A withdrawal
    writes three legs (`payouts/services.py`): the request (trainer -> payout
    clearing), the return (clearing -> trainer, on fail or cancel) and the payout
    (clearing -> off the platform). Reversing the request leg put the money back while
    the withdrawal stayed open, and its cancel or fail then paid the trainer a second
    time out of clearing, which holds every other trainer's open withdrawal; reversing
    the return stranded the trainer's money in clearing with nothing open; reversing
    the payout minted money into clearing. Undoing a withdrawal is `fail` (or the
    trainer's `cancel`) in the payouts console, which moves the money once and records
    it on the withdrawal.

    NO LEG OF A PROGRAM SALE IS REVERSED ON ITS OWN (SIDE_BUGS S-237). A routine
    product's sale is three legs (`routine/products.py`): the money coming in (the
    buyer's wallet -> escrow for a wallet sale, or minted into escrow from ShamCash for a
    checkout), escrow -> the trainer's share, escrow -> the platform's commission.
    Reversing the checkout's mint burned the shared per-currency escrow — which backs
    every other client's coaching hold — while the purchase stayed confirmed and both
    shares stayed paid, and the proper refund then refused with "already reversed by
    hand"; reversing one share alone did the same to the other side. Any row whose
    metadata names a `purchase_id`, or whose `purpose` is a `routine_product_*` one, is
    refused: undoing a sale is the purchase's refund (`/dj-admin/routine/
    routinepurchase/`, «refund») or, for a checkout, its payment's refund, which reverses
    every leg together and revokes the buyer's copy.

    A CLIENT'S PAYMENT TO A TRAINER IS REVERSED WHOLE (BACKEND_BUGS B-163). Since the
    owner made it a coaching payment (2026-09-28) it is three legs, like a coaching
    payout: client -> escrow, escrow -> trainer the share, escrow -> platform the
    commission (`ClientTransferToTrainerView`). Reversing its `reference_id` — the
    client's leg, the reference the client was given — puts every leg back in one
    transaction (the share and the commission to escrow, the amount to the client) and
    answers the client's leg's reversal; `insufficient_balance` when the trainer no
    longer holds the share, and nothing moves. The trainer's leg or the commission
    alone is `409 coaching_transfer_leg`: either alone would leave escrow paying for the
    other. A transfer made before (one WALLET_MOVE row, client -> trainer) is reversed
    as it always was.

    A reversed AGENT top-up also comes off the agent's receivable (the cash they hold
    for the platform; plan step 9), in the same transaction: the agent hands the cash
    back to the client, and their ledger shows a `reversal` row.
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request):
        serializer = ReversalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # --- Pre-side-effect validation before reserving the idempotency key ---
        original = get_object_or_404(Transaction, reference_id=serializer.validated_data["reference_id"])

        # A cheap early answer for the ordinary case. It is NOT the guard — this read and
        # the write below are different transactions with no lock between them, and two
        # administrators hitting Reverse on the same reference both passed it and both
        # moved money (measured: one 100.00 transfer came out of the trainer's wallet
        # twice). The real guard is inside `move_funds_atomic`, which locks the original
        # and re-checks, backed by a OneToOne on `Transaction.reverses`.
        if Transaction.objects.filter(reverses=original).exists():
            return Response({"error": _("Transaction has already been reversed"),
                             "code": "already_reversed"}, status=status.HTTP_409_CONFLICT)
        if original.tx_type == "reversal":
            return Response({"error": _("A reversal cannot itself be reversed"),
                             "code": "not_reversible"}, status=status.HTTP_409_CONFLICT)
        if original.exchange_reference:
            return Response({"error": _("This is one half of a currency exchange and "
                                        "cannot be reversed on its own."),
                             "code": "exchange_leg",
                             "exchange_reference": original.exchange_reference},
                            status=status.HTTP_409_CONFLICT)
        # Decided on what the row says of itself, which never changes, so this read is
        # the guard and not only an early answer (see above).
        from payouts.services import withdrawal_of_leg

        is_leg, withdrawal = withdrawal_of_leg(original)
        if is_leg:
            return Response({"error": _("This is one leg of a trainer's withdrawal and "
                                        "cannot be reversed on its own. Fail or cancel "
                                        "the withdrawal instead."),
                             "code": "withdrawal_leg",
                             "withdrawal": ({"id": withdrawal.pk, "number": withdrawal.number,
                                             "state": withdrawal.state}
                                            if withdrawal is not None else None)},
                            status=status.HTTP_409_CONFLICT)
        # The same: decided on what the row says of itself (S-237).
        sale_leg = _program_sale_leg(original)
        if sale_leg is not None:
            return sale_leg
        # And one leg of a client's payment to a trainer (B-163): only the whole of it.
        transfer_leg = _coaching_transfer_leg(original)
        if transfer_leg is not None:
            return transfer_leg
        # And a plan paid from the wallet (S2.13): its one row is the whole payment.
        plan_leg = _plan_payment_leg(original)
        if plan_leg is not None:
            return plan_leg

        idem_key = serializer.validated_data["idempotency_key"]
        try:
            idem, replay = idempotency.reserve(
                request.user, idem_key,
                idempotency.fingerprint(op="reversal", original=original.reference_id),
            )
        except idempotency.IdempotencyConflict as exc:
            return _idempotency_conflict(exc)
        if replay is not None:
            audit("wallet.reversal.idempotent_hit", request, {"key": idem_key})
            return Response(replay, status=status.HTTP_200_OK)

        # An AGENT top-up that is reversed comes off the cash the agent holds for the
        # platform (plan step 9): the agent hands it back to the client. The agent's
        # profile is locked BEFORE the money moves, in the order a top-up takes the same
        # rows (profile, then user row and wallets), and the entry is written in the same
        # transaction as the reversal.
        from . import agents

        agent_profile = agents.profile_of_topup(original)
        try:
            with transaction.atomic():
                if agent_profile is not None:
                    agent_profile = agents.lock_profile(agent_profile)
                if (original.metadata or {}).get("coaching_transfer") == "charge":
                    # A client's payment to a trainer (B-163): every leg goes back.
                    tx = _reverse_coaching_transfer(
                        original, actor_id=request.user.id,
                        reason=serializer.validated_data.get("reason", ""))
                else:
                    tx = move_funds_atomic(
                        original.destination_wallet, original.source_wallet,
                        original.amount, actor_id=request.user.id, tx_type="reversal",
                        # The kind of money that is going back is the kind that came.
                        money_type=original.money_type,
                        metadata={"original_reference": original.reference_id,
                                  "reason": serializer.validated_data.get("reason", "")},
                        reverses=original)
                if agent_profile is not None:
                    agents.post_reversal(agent_profile, tx)
        except AlreadyReversed:
            # The other caller won the lock. No money moved here.
            idempotency.release(idem)
            audit("wallet.reversal.already", request, {"original": original.reference_id})
            return Response({"error": _("Transaction has already been reversed"),
                             "code": "already_reversed"}, status=status.HTTP_409_CONFLICT)
        except ValueError as e:
            # `InsufficientBalance` and `CurrencyMismatch` are ValueErrors: the same 400
            # as ever, each with the code that says which (the owner, 2026-09-24).
            from .models import CurrencyMismatch, InsufficientBalance

            idempotency.release(idem)
            code = ("insufficient_balance" if isinstance(e, InsufficientBalance)
                    else "currency_mismatch" if isinstance(e, CurrencyMismatch)
                    else "reversal_refused")
            return Response({"error": _("Request could not be completed."), "code": code},
                            status=status.HTTP_400_BAD_REQUEST)
        resp = {"reference_id": tx.reference_id}
        idempotency.complete(idem, resp)
        audit("wallet.reversal.success", request, {"reference_id": tx.reference_id, "original": original.reference_id})
        return Response(resp, status=status.HTTP_200_OK)


def _device_name(request) -> str:
    """The optional `device_name` a pairing call may carry ("Samsung A54"), trimmed to
    what the column holds. A label, never checked against anything."""
    value = body_dict(request).get("device_name")
    return str(value).strip()[:80] if isinstance(value, (str, int, float)) else ""


def _mint_key(agent_profile, device_name=""):
    """A new active key for `agent_profile`: `(row, secret)`. The caller holds the
    profile lock and has deactivated any other active key. Pairing again lifts a revoke
    (`key_revoked_at`): top-ups resume with the new key."""
    import hashlib

    key_id = uuid.uuid4().hex[:16]
    # High-entropy secret (64 hex chars = 256 bits).
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    # Non-signing lookup/compat digest; the raw secret is stored encrypted (Fernet).
    hashed_key = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    row = AgentAPIKey.objects.create(
        agent=agent_profile, key_id=key_id, hashed_key=hashed_key,
        secret_ciphertext=encrypt_secret(secret), is_active=True, device_name=device_name)
    if agent_profile.key_revoked_at is not None:
        AgentProfile.objects.filter(pk=agent_profile.pk).update(key_revoked_at=None)
        agent_profile.key_revoked_at = None
    return row, secret


class AgentApiKeyCreateView(APIView):
    """`POST /api/wallet/agent/apikey/create/` — pair (or re-pair) this device: a NEW
    signing key, the old one deactivated (AgentKey «جدّد المفتاح»).

    Body (optional): `device_name` — the device, as AgentKey shows it ("Samsung A54").

    201 `{key_id, secret, fingerprint, device_name, paired_at}`. `secret` is shown ONCE
    and never stored in plaintext (Fernet at rest). `fingerprint` is "7F3A · 91C2 ·
    0B4E" (the first twelve hex characters of `key_id`), `paired_at` ISO 8601. Creating
    a key after `revoke/` lets top-ups resume.

    Refusals: 401 (the framework's), 403 `agents_only` — the caller is not an agent.
    Identical on every build.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def post(self, request):
        from . import agents

        agent_profile = ensure_agent_profile(request.user)
        device_name = _device_name(request)

        # Deactivate, then create, ATOMICALLY and under the agent's own row lock. The
        # "single active key policy" this comment describes was enforced by nothing: an
        # unlocked `update()` followed by a `create()` with no transaction and no unique
        # index, so two concurrent POSTs both deactivated and both created — the agent
        # then held two valid signing credentials, and revoking the one they knew about
        # left the other live. A partial unique index now makes the database say it too.
        with transaction.atomic():
            agent_profile = agents.lock_profile(agent_profile)
            prev_ids = list(AgentAPIKey.objects.filter(
                agent=agent_profile, is_active=True).values_list("key_id", flat=True))
            if prev_ids:
                AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).update(
                    is_active=False)
            row, secret = _mint_key(agent_profile, device_name)
        if prev_ids:
            audit("wallet.agent.apikey.rotated", request, {"deactivated": prev_ids})
        audit("wallet.agent.apikey.create", request, {"key_id": row.key_id})
        # Return secret ONCE — client must store it securely
        body = agents.key_body(row)
        body.pop("last_signature_at")
        return Response({**body, "secret": secret}, status=status.HTTP_201_CREATED)


class AgentApiKeyStatusView(APIView):
    """`GET /api/wallet/agent/apikey/status/` — the pairing AgentKey draws.

    200 `{has_active, key_id, fingerprint, device_name, paired_at, last_signature_at,
    revoked_at, topups_stopped}` (all but `has_active` since BACKEND_BUGS B-117,
    additive). The key fields describe the ACTIVE key and are null when there is none:
    `fingerprint` "7F3A · 91C2 · 0B4E", `device_name` as the app sent it when pairing,
    `paired_at` when the key was made, `last_signature_at` when it last signed a top-up
    (the signed endpoint, or the app's own top-up, which the server signs with it).
    `revoked_at` is when the agent revoked without a replacement (null once a new key is
    paired); `topups_stopped` is true while that stops top-ups (`403 agent_key_revoked`).
    Never a secret.

    Refusals: 401 (the framework's), 403 `agents_only` — the caller is not an agent.
    Identical on every build.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def get(self, request):
        from . import agents

        agent_profile = ensure_agent_profile(request.user)
        key = agents.active_key(agent_profile)
        return Response({"has_active": key is not None, **agents.key_body(key),
                         "revoked_at": (agent_profile.key_revoked_at.isoformat()
                                        if agent_profile.key_revoked_at else None),
                         "topups_stopped": agents.key_revoked(agent_profile)})


class AgentApiKeyEnsureView(APIView):
    """`POST /api/wallet/agent/apikey/ensure/` — make sure the agent has an active key.

    Body (optional): `device_name` — the device asking ("Samsung A54").

    201 `{created: true, key_id, fingerprint, device_name, paired_at}` when it made one
    (the secret stays server-side, encrypted, for the app's top-ups the server signs);
    200 `{created: false, key_id, fingerprint, device_name, paired_at}` naming the
    current active key otherwise. `key_id` and `fingerprint` since BACKEND_BUGS B-116:
    the docstring promised `key_id` and the code answered only `created`. Making a key
    after `revoke/` lets top-ups resume.

    ONE AGENT, ONE DEVICE (BACKEND_BUGS B-145; the owner, 2026-09-28). The active key
    belongs to the device it was paired on. A `device_name` is recorded on an active key
    that has none yet; the same name (case aside) or no name at all is answered 200 as
    above; a DIFFERENT name is another phone asking, and it is refused, never relabelled
    and never told it is paired: 409 `key_paired_elsewhere` with `device_name` (the
    device the key is paired on), `fingerprint` and `paired_at`. Nothing changes. The
    agent moves the key on purpose with `create/` (AgentKey «جدّد المفتاح»), which pairs
    the asking device and deactivates the old key.

    Refusals: 401 (the framework's), 403 `agents_only` — the caller is not an agent;
    409 `key_paired_elsewhere`. Identical on every build.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def post(self, request):
        from . import agents

        agent_profile = ensure_agent_profile(request.user)
        device_name = _device_name(request)
        # Under the profile lock, so two first calls cannot both create (the partial
        # unique index would turn the loser into a 500).
        with transaction.atomic():
            agent_profile = agents.lock_profile(agent_profile)
            api_key = agents.active_key(agent_profile)
            created = api_key is None
            if created:
                api_key, _secret = _mint_key(agent_profile, device_name)
            elif device_name and not api_key.device_name:
                AgentAPIKey.objects.filter(pk=api_key.pk).update(device_name=device_name)
                api_key.device_name = device_name
            elif (device_name
                  and device_name.casefold() != api_key.device_name.strip().casefold()):
                # Another phone (B-145): refused, and nothing changes.
                paired = agents.key_body(api_key)
                audit("wallet.agent.apikey.ensure_refused", request,
                      {"key_id": api_key.key_id})
                return Response(
                    {"error": _("Your signing key is paired with another device. Pair "
                                "this device to move the key to it."),
                     "code": "key_paired_elsewhere",
                     "device_name": paired["device_name"],
                     "fingerprint": paired["fingerprint"],
                     "paired_at": paired["paired_at"]},
                    status=status.HTTP_409_CONFLICT)
        body = agents.key_body(api_key)
        body.pop("last_signature_at")
        if created:
            audit("wallet.agent.apikey.ensure", request, {"key_id": api_key.key_id})
            return Response({"created": True, **body}, status=status.HTTP_201_CREATED)
        return Response({"created": False, **body})


class AgentApiKeyRevokeView(APIView):
    """`POST /api/wallet/agent/apikey/revoke/` — AgentKey «أبطل المفتاح وأوقف الشحن»:
    revoke the signing key WITHOUT a replacement, and stop top-ups (BACKEND_BUGS B-117).

    No body. Every active key of the agent is deactivated (the signed endpoint then
    answers `401 agent_auth_invalid` for it) and the app's own top-ups answer `403
    agent_key_revoked` until a new key is paired with `create/` or `ensure/`. For a lost
    or stolen phone: whoever holds it can no longer top up in the agent's name. Revoking
    again changes nothing but the time.

    200 `{has_active: false, revoked_at, revoked_key_ids}`.

    Refusals: 401 (the framework's), 403 `agents_only` — the caller is not an agent.
    Identical on every build.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def post(self, request):
        from . import agents

        agent_profile = ensure_agent_profile(request.user)
        now = timezone.now()
        with transaction.atomic():
            agent_profile = agents.lock_profile(agent_profile)
            revoked = list(AgentAPIKey.objects.filter(
                agent=agent_profile, is_active=True).values_list("key_id", flat=True))
            AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).update(
                is_active=False)
            AgentProfile.objects.filter(pk=agent_profile.pk).update(key_revoked_at=now)
        audit("wallet.agent.apikey.revoked", request, {"deactivated": revoked})
        return Response({"has_active": False, "revoked_at": now.isoformat(),
                         "revoked_key_ids": revoked})


class AgentTopUpProxyView(APIView):
    """`POST /api/wallet/agent/topup/proxy/` — the agent APP's top-up (AgentTopUp): JWT
    only; the server signs with the agent's stored key and runs the top-up flow.

    Body: `client_identifier`, `amount`, `currency` ("SYP" | "USD" — the currency of the
    CASH the agent took, required), `idempotency_key`. Headers: `X-App-Channel`.

    200 `{reference_id, balance, currency, receipt_number, remaining_today}` (the last
    two since plan step 9: the receipt's number, and what is left of today's cap in this
    currency). A replay of a finished request with the same `idempotency_key` and body
    answers the same 200 again, before any cap is read. Refusals (branch on `code`) —
    the same rules as `AgentTopUpView`, less the signature:

    | Status | `code` | Meaning |
    |---|---|---|
    | 400 | `app_channel_required` | no known `X-App-Channel` |
    | 403 | `not_available_on_channel` | this build may not move wallet money |
    | 400 | `validation_error` | a body field is missing or malformed; a missing or unknown `currency` is `field_errors.currency` |
    | 403 | `agents_only` | the caller is not an agent |
    | 403 | `agent_not_active` | the agent's profile is suspended |
    | 403 | `agent_key_revoked` | the agent revoked their signing key (`apikey/revoke/`) and has not paired a new one; `apikey/create/` or `ensure/` lifts it |
    | 404 | `not_found` | `client_identifier` names nobody — or a suspended or erased account, which answers exactly as a missing one (BACKEND_BUGS B-118) |
    | 400 | `target_not_a_client` | it names an account that is not a client |
    | 409 | `currency_mismatch` | the client trades in the other currency; `client_currency` and `topup_currency` say which |
    | 429 | `per_transaction_limit_exceeded` | more than one top-up may be in that currency |
    | 429 | `daily_limit_exceeded` / `monthly_limit_exceeded` | the agent's cap FOR THAT CURRENCY |
    | 429 | `receivable_ceiling_exceeded` | the cash the agent holds for the platform, unsettled, would pass their ceiling; an operator's settlement lifts it |
    | 409 | `request_in_flight` | the first request with this `idempotency_key` is still running |
    | 422 | `idempotency_key_reused` | the key was used for a different request |
    | 429 | `throttled` | the rate limiter (`Retry-After`) |

    The four cap refusals also carry `limit`, `used` (null for the per-top-up cap),
    `remaining` and `currency` (BACKEND_BUGS B-124): the cap, what is used against it,
    what is left, in which currency. `agents_only`, `agent_not_active`,
    `target_not_a_client`, the two older cap codes and the two idempotency codes answered
    only their status defaults until the owner's decision of 2026-09-24.

    A top-up posts the cash to the agent's receivable, stamps the active key's
    `last_signature_at`, and pushes the client `wallet_topped_up` (API_CONTRACT §4).
    WALLET_MOVE: `require_purchase_allowed` decides by the AGENT's build, the request
    that moves the money: `can_transfer_wallet`, on for `apk`, `web` and `play` and OFF
    for the first `ios` build by the spec's table (`policy/defaults.py`), which answers
    403 `not_available_on_channel` above. It said "every build allows it", which the
    table never did (BACKEND_BUGS B-199; which of the two is meant is the owner's
    question, and until he answers the flag stands). The client being credited sends
    no request: their build is never read, and a client on any build is topped up by an
    agent on a build that allows it. The signed `agent/topup/` reads no build at all.
    Pinned by tests/test_b199_wallet_move_by_build.py.
    """
    permission_classes = [IsAuthenticated, IsAgent]
    throttle_classes = [ChargingRateThrottle]

    def post(self, request):
        # The app-facing way to move money, so it answers to the build's policy like the
        # client transfer does. The HMAC endpoint is server-to-server and has no build.
        require_purchase_allowed(request, MoneyType.WALLET_MOVE)

        serializer = AgentTopUpProxyRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # JWT + IsAgent is the SOLE authentication control for the mobile agent
        # flow, by design: the signing secret stays server-side and is never placed
        # on the device. No client-supplied HMAC is involved here (previously this
        # endpoint self-signed a request and never verified it — that dead code is
        # removed). Security rests on JWT auth, agent status, the revoke switch, the
        # fail-closed top-up caps and ceiling, and idempotency.
        agent_profile = ensure_agent_profile(request.user)

        client_identifier = serializer.validated_data["client_identifier"]
        amount = serializer.validated_data["amount"]
        currency = serializer.validated_data["currency"]
        idempotency_key = serializer.validated_data["idempotency_key"]

        # --- Pre-side-effect validation before reserving the idempotency key ---
        client = _client_named(client_identifier)
        if getattr(client, "user_type", None) != "client":
            return Response({"error": _("Target must be a client"),
                             "code": "target_not_a_client"},
                            status=status.HTTP_400_BAD_REQUEST)
        mismatch = _topup_currency_mismatch(client, currency)
        if mismatch is not None:
            return mismatch

        # Same serialization as the signed endpoint. This path needs only a JWT, so it
        # is the easier of the two to race: an agent's own app can fire ten parallel
        # requests with no signing at all.
        return _perform_topup(request, agent_profile, client=client, amount=amount,
                              currency=currency, idem_key=idempotency_key)


# ----------------------------------------------------------------------------------
# The agent's own reads (plan step 9; BACKEND_BUGS B-113, B-114). Records of money that
# already moved and the agent's own caps: no price, nothing to buy, so no channel header
# and identical on every build.
# ----------------------------------------------------------------------------------

class AgentMeView(APIView):
    """`GET /api/wallet/agent/me/` — the agent's own account (AgentHome, AgentHistory's
    «حدودك», AgentMe; BACKEND_BUGS B-113).

    Headers: `Authorization: Bearer <access>`. No body, no query.

    200:

        {user_id,
         profile: {display_name, city, area, opening_hours, public_phone, status,
                   is_listed},
         status, can_top_up, blocked_reason, next_settlement_on, currency, today,
         time_zone,
         currencies: {"SYP": {...}, "USD": {...}}}

    `status` is the agent's own status, `AgentProfile.status` (`active`, `suspended`,
    `banned`) — the same value as `profile.status`, kept at the top level too (BACKEND_BUGS
    B-148). There is no account-level status beside it: a disabled account never reaches
    this route (401 `user_inactive`). `can_top_up` is false
    and `blocked_reason` names the code a top-up would be refused with when the ACCOUNT
    stops it: `agent_not_active`, or `agent_key_revoked` (revoked, no new key yet); null
    otherwise. `next_settlement_on` (YYYY-MM-DD) is the next settlement as an operator
    set it in the console, while it is today or later — NOTHING SCHEDULES SETTLEMENTS,
    so it is null unless a person wrote one; the app shows no date then.

    `currency` (BACKEND_BUGS B-144) is the agent's own wallet currency
    (`wallet.models.currency_for`), so the app picks `currencies[currency]` to draw
    without reading `wallet/balance/`. Nothing on the agent path depends on it: a
    top-up's currency is the cash's, checked against the CLIENT's.

    `today` (YYYY-MM-DD) and `time_zone` (the IANA name, "Asia/Damascus") are the
    server's day and zone (BACKEND_BUGS B-146): the day `count_today` / `used_today`
    count and the day `since` / `until` on `agent/ledger/` are read in. A phone in
    another zone sends `today` as `since` to agree with the caps. The ledger's,
    receipts' and settlements' timestamps (and the key's `paired_at` /
    `last_signature_at`) are written with the server's offset (+03:00), like
    `wallet/transactions/` rows: the same instants they always were.

    One object per currency the platform trades in, every amount a string in that
    currency: `per_transaction_limit` (null: none of its own), `daily_limit`,
    `monthly_limit`, `used_today`, `used_this_month`, `count_today`,
    `count_this_month` (top-ups), `remaining_today`, `remaining_this_month`,
    `receivable` (cash the agent holds for the platform, unsettled), `receivable_ceiling`
    (past it top-ups stop; the monthly cap when none is set), `receivable_room`,
    `max_topup` (the largest top-up allowed right now; `0.00` when anything stops it),
    `blocked_reason` (the code even the smallest top-up in this currency would get:
    the account's, or `per_transaction_limit_exceeded`, `daily_limit_exceeded`,
    `monthly_limit_exceeded`, `receivable_ceiling_exceeded`; null when one could go
    through), `last_settlement` (`{number, amount, currency, receivable_before,
    receivable_after, note, settled_at}` or null). The counts are the same rows the caps
    count, so this never disagrees with a refusal. A currency with a `0` daily cap (no
    dollar caps are set by default) reads `blocked_reason: daily_limit_exceeded`.

    Refusals: 401 `not_authenticated` / `token_not_valid`; 403 `agents_only` (not an
    agent — the code every agent route answers).
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def get(self, request):
        from . import agents

        profile = ensure_agent_profile(request.user)
        return Response(agents.account_body(profile))


#: The `kind` values of the agent's ledger.
AGENT_LEDGER_KINDS = ("topup", "settlement", "reversal")


def _agent_ledger_filters(params):
    """`(since, until, kinds, currencies)` from the query, or `LedgerFilterRefused`.

    The rules of `wallet.ledger_reads` (inclusive `YYYY-MM-DD` days in the server's
    zone; repeat a parameter or separate values with commas; several mean any)."""
    from .currencies import CURRENCY_CODES
    from .ledger_reads import LedgerFilterRefused, _date, _values

    since, until = _date(params, "since"), _date(params, "until")
    if since is not None and until is not None and since > until:
        raise LedgerFilterRefused("date_range_reversed", _("since must not be after until."))
    kinds = _values(params, "kind")
    for value in kinds:
        if value not in AGENT_LEDGER_KINDS:
            raise LedgerFilterRefused("bad_kind", _("That kind of activity is unknown."),
                                      kind_options=list(AGENT_LEDGER_KINDS))
    currencies = [value.upper() for value in _values(params, "currency")]
    for value in currencies:
        if value not in CURRENCY_CODES:
            raise LedgerFilterRefused("currency_unsupported",
                                      _("That currency is not available."),
                                      currency_options=list(CURRENCY_CODES))
    return since, until, kinds, currencies


class AgentLedgerView(generics.ListAPIView):
    """`GET /api/wallet/agent/ledger/` — the agent's own ledger: the top-ups it made,
    the settlements recorded against the cash it holds, and any top-up reversed
    (AgentHistory «حركات أيلول»; BACKEND_BUGS B-114). Newest first, in a total order.

    A top-up is written to the CLIENT's wallet, so `wallet/transactions/` (the caller's
    own wallet) never showed an agent its own work; this reads the agent's
    `agent_receivable` account instead, where every one of them is posted.

    Headers: `Authorization: Bearer <access>`. Query, all optional and ANDed:
    `since=YYYY-MM-DD`, `until=YYYY-MM-DD` (inclusive, server's zone), `kind=`
    (`topup`, `settlement`, `reversal`), `currency=` (`SYP`, `USD`); `?page=`,
    `?page_size=` (§3).

    200 `{count, next, previous, results}`, each row `{id, kind, amount, currency,
    receivable_after, created_at, reference_id, receipt_number, client, settlement}`:
    `amount` SIGNED — `+` a top-up (cash the agent now holds for the platform), `-` a
    settlement or a reversed top-up; `receivable_after` the cash held after the row;
    `reference_id` the ledger reference of a top-up or reversal (null for a settlement);
    `receipt_number` "TP-2026-09-23-0091" on a top-up (null otherwise); `client` `{id,
    name, account_number}` on a top-up or reversal (null on a settlement); `settlement`
    `{number, amount, currency, receivable_before, receivable_after, note, settled_at}`
    on a settlement (null otherwise).

    Also (BACKEND_BUGS B-147, additive): `reverses_reference_id` — on a `reversal` row,
    the reference of the top-up it took back, which `receipts/{reference_id}/` opens (the
    row's own `reference_id` is the reversal's, which has no receipt); null on every
    other row. `reversed` — on a `topup` row, whether support has since reversed it;
    null on a settlement or reversal row. Read in the page's one query.

    Refusals: 400 `bad_date` (+ `field`), `date_range_reversed`, `bad_kind`
    (+ `kind_options`), `currency_unsupported` (+ `currency_options`; the code
    `wallet/currency/` answers for the same fact); 401; 403 `agents_only`.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def get_serializer_class(self):
        from .serializers import AgentLedgerEntrySerializer

        return AgentLedgerEntrySerializer

    def get_queryset(self):
        from .ledger_reads import start_of
        from .models import AgentReceivableEntry

        profile = ensure_agent_profile(self.request.user)
        rows = (AgentReceivableEntry.objects.filter(agent=profile)
                .select_related("transaction__destination_wallet__owner",
                                "transaction__source_wallet__owner", "settlement",
                                # B-147: the link between a top-up and its reversal,
                                # both ways, in the same query.
                                "transaction__reverses", "transaction__reversal")
                .order_by("-created_at", "-id"))
        since, until, kinds, currencies = getattr(self, "_filters", (None, None, [], []))
        if since is not None:
            rows = rows.filter(created_at__gte=start_of(since))
        if until is not None:
            rows = rows.filter(created_at__lt=start_of(until + datetime.timedelta(days=1)))
        if kinds:
            rows = rows.filter(kind__in=kinds)
        if currencies:
            rows = rows.filter(currency__in=currencies)
        return rows

    def list(self, request, *args, **kwargs):
        from .ledger_reads import LedgerFilterRefused

        try:
            self._filters = _agent_ledger_filters(request.query_params)
        except LedgerFilterRefused as exc:
            return _ledger_filter_refused(exc)
        return super().list(request, *args, **kwargs)


class AgentSettlementListView(generics.ListAPIView):
    """`GET /api/wallet/agent/settlements/` — the settlements an operator recorded
    against the cash this agent held for the platform (AgentMe «التسويات مع المنصة»;
    BACKEND_BUGS B-114). Newest first, in a total order; `?page=`, `?page_size=`.

    Each row `{number, amount, currency, receivable_before, receivable_after, note,
    settled_at}`: "ST-2026-09-19-0007", what was handed over, the cash held before and
    after, the operator's note, and when. The same settlements appear as `settlement`
    rows of `agent/ledger/`.

    Refusals: 401; 403 `agents_only`.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def get_serializer_class(self):
        from .serializers import AgentSettlementSerializer

        return AgentSettlementSerializer

    def get_queryset(self):
        from .models import AgentSettlement

        profile = ensure_agent_profile(self.request.user)
        return AgentSettlement.objects.filter(agent=profile).order_by("-created_at", "-id")


class AgentReceiptView(APIView):
    """`GET /api/wallet/agent/receipts/{reference_id}/` — the receipt of one top-up THIS
    agent made (AgentReceipt; BACKEND_BUGS B-114), by the ledger reference its top-up
    answered.

    200 `{receipt_number, reference_id, amount, currency, created_at, client, agent,
    remaining_today, client_notified, reversed}`: `receipt_number` "TP-2026-09-23-0091";
    `client` `{id, name, account_number}` (who was credited — never their balance);
    `agent` `{id, name, city, area}` (the shop, «مكتب الفرات · المزة»);
    `remaining_today` what is left of today's cap in the receipt's currency NOW;
    `client_notified` whether the client's `wallet_topped_up` notice was written
    («ووصله إشعار»; BACKEND_BUGS B-115) — false until the worker has run; `reversed`
    whether support has since reversed the top-up.

    Refusals: 404 `not_found` — a reference that is not a top-up this agent made,
    byte-identical to one that does not exist (another agent's receipt is never shown);
    401; 403 `agents_only`.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def get(self, request, reference_id):
        from . import agents
        from .models import AgentReceivableEntry

        profile = ensure_agent_profile(request.user)
        entry = (AgentReceivableEntry.objects
                 .select_related("agent__user", "transaction__destination_wallet__owner")
                 .filter(agent=profile, kind="topup",
                         transaction__reference_id=reference_id).first())
        if entry is None:
            return Response({"error": _("No such transaction"), "code": "not_found"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(agents.receipt_body(entry))


class AdminAuditExportView(APIView):
    """`GET /api/wallet/admin/audit/export/?start=&end=&agent_id=&user_id=&verify=1`.

    Refusals: 401 (the framework's), 403 `admins_only` — the caller is not an operator,
    400 `validation_error` — `agent_id` or `user_id` is not a whole number
    (`field_errors` names it).
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        start = request.query_params.get("start")
        end = request.query_params.get("end")
        # Both land in `actor_id`, an integer column: a non-numeric value reached
        # Postgres and came back as a 500 for what is the caller's typo.
        agent_id = int_param(request.query_params, "agent_id")
        user_id = int_param(request.query_params, "user_id")
        qs = WalletAuditLog.objects.all()
        if start:
            qs = qs.filter(created_at__gte=start)
        if end:
            qs = qs.filter(created_at__lte=end)
        if agent_id:
            qs = qs.filter(actor_id=agent_id)
        if user_id:
            qs = qs.filter(actor_id=user_id)
        data = list(qs.values("event_type", "actor_id", "ip_address", "request_id", "path", "payload", "created_at", "prev_hash", "entry_hash")[:10000])
        resp = {"count": len(data), "results": data}
        # Optional integrity check of the full chain: ?verify=1
        if request.query_params.get("verify") in ("1", "true", "True"):
            is_valid, first_bad_id = WalletAuditLog.verify_chain()
            resp["chain_valid"] = is_valid
            resp["first_tampered_id"] = first_bad_id
        return Response(resp)


#: What a single top-up must reach to count as "large" when nobody has set a threshold
#: for its currency. It is the one number this alert ever had, kept as it was rather
#: than invented per currency: `WALLET_LARGE_TOPUP_THRESHOLDS` (`{"SYP": ..., "USD":
#: ...}`) is where the owner's numbers go.
DEFAULT_LARGE_TOPUP_THRESHOLD = 1000


def large_topup_thresholds() -> dict:
    """currency -> the amount one top-up must reach to be flagged."""
    from .currencies import CURRENCY_CODES

    configured = getattr(settings, "WALLET_LARGE_TOPUP_THRESHOLDS", None) or {}
    return {code: configured.get(code, DEFAULT_LARGE_TOPUP_THRESHOLD)
            for code in CURRENCY_CODES}


class AdminSuspiciousActivityView(APIView):
    """`GET /api/wallet/admin/alerts/suspicious/?days=` — heuristics for an operator.

    `large_topups` and `rapid_topups` count CASH top-ups only, and `large_topups` is
    judged and totalled per currency, each against its own threshold
    (`large_topup_thresholds`); its entries carry `currency`. The mint leg of a plan
    payment is also a `topup` row, and counting it flagged every client who bought a
    plan, while one threshold for both currencies let a dollar top-up worth millions of
    pounds through unflagged (SIDE_BUGS S-162).

    Refusals: 401 (the framework's), 403 `admins_only` — the caller is not an operator,
    400 `validation_error` — `days` is not a whole number (one outside 1..365 is
    clamped, not refused).
    """
    permission_classes = [IsAuthenticated, IsAdmin]

    def get(self, request):
        window_days = int_param(request.query_params, "days", default=1, minimum=1, maximum=365)
        since = timezone.now() - timezone.timedelta(days=window_days)
        alerts = []
        # Heuristics: multiple failed auths, rapid topups, large amounts
        failed_auths = WalletAuditLog.objects.filter(event_type__icontains="auth_failed", created_at__gte=since).values("actor_id").annotate(cnt=Sum(1))
        for row in failed_auths:
            if row["cnt"] and row["cnt"] >= 5:
                alerts.append({"type": "failed_auth_spike", "actor_id": row["actor_id"], "count": row["cnt"]})
        # Cash top-ups: not the mint leg of a plan payment.
        topups = (Transaction.objects.filter(tx_type="topup", created_at__gte=since)
                  .exclude(metadata__has_key="original_payment_id"))
        large = Q()
        for code, threshold in large_topup_thresholds().items():
            large |= Q(currency=code, amount__gte=threshold)
        large_topups = (topups.filter(large).values("actor_id", "currency")
                        .annotate(total=Sum("amount")).order_by("actor_id", "currency"))
        for row in large_topups:
            alerts.append({"type": "large_topups", "actor_id": row["actor_id"],
                           "currency": row["currency"], "total": float(row["total"] or 0)})
        rapid_topups = topups.values("actor_id").annotate(cnt=Sum(1))
        for row in rapid_topups:
            if row["cnt"] and row["cnt"] >= 20:
                alerts.append({"type": "rapid_topups", "actor_id": row["actor_id"], "count": row["cnt"]})
        return Response({"alerts": alerts})



