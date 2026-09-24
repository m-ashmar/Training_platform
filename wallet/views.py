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
from training_platform.query_params import body_dict, int_param
from wallet import idempotency


User = get_user_model()


def audit(event_type: str, request, payload: dict):
    WalletAuditLog.objects.create(
        event_type=event_type,
        actor=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
        request_id=getattr(request, "id", None),
        ip_address=request.META.get("REMOTE_ADDR"),
        user_agent=request.META.get("HTTP_USER_AGENT"),
        path=request.path,
        payload=payload or {},
    )


def _lock_agent_profile(agent_profile):
    """Take a row lock on the agent's profile.

    Serializes one agent's concurrent top-ups against each other. Raises
    TransactionManagementError if the caller forgot the enclosing atomic block,
    which is the loud failure we want rather than a silently unlocked read.
    """
    return AgentProfile.objects.select_for_update().get(pk=agent_profile.pk)


def _topup_limit_error(agent_profile, actor, amount, request, currency):
    """
    Enforce agent daily/monthly top-up caps FAIL-CLOSED. A limit of 0 means
    "no top-ups permitted", not unlimited. Returns a Response on breach, else None:
    429 `daily_limit_exceeded` or `monthly_limit_exceeded`. A cap is not the rate
    limiter — waiting out a `Retry-After` does not lift it — so it does not answer the
    limiter's `throttled` (the owner, 2026-09-24: every refusal names its own code).

    PER CURRENCY (SIDE_BUGS S-059). The totals count only this currency's top-ups and
    are held against this currency's caps (`AgentProfile.limits_for`). They used to add
    pounds and dollars together against one number, so a cap an operator set in pounds
    let the same number of dollars through — about 13,000 times the value.

    CONCURRENCY CONTRACT: `agent_profile` must already be locked by
    `_lock_agent_profile`, and the ledger write this guards must happen in the SAME
    transaction. These two aggregates are a read; on their own they are a
    check-then-act. Ten concurrent top-ups of 400 against a 1000/day cap each read a
    total that predated the other nine, all passed, and minted 3200 — 3.2x the cap.
    Holding the profile lock from here through move_funds_atomic is what makes the
    aggregate below true at the moment the money moves.
    """
    limits = agent_profile.limits_for(currency)
    if limits is None:
        # A currency this agent has no caps for is refused, never treated as open.
        limits = (0, 0)
    daily_limit, monthly_limit = limits
    today = timezone.localdate()
    month_start = today.replace(day=1)
    # RANGES on the column, not a function of it. `created_at__date=` compiles to
    # `(created_at AT TIME ZONE 'Asia/Damascus')::date = ...`, which no btree on
    # `created_at` can serve — so both of these, which run on every top-up, scanned every
    # row the agent had ever written: one agent with 10,000 ledger rows read all 10,000
    # to find 500, and the scan never resets, so it gets slower for the agent's whole
    # lifetime. A half-open range over the same instants uses `tx_actor_type_time_idx`.
    day_start = timezone.make_aware(
        datetime.datetime.combine(today, datetime.time.min))
    tomorrow = day_start + datetime.timedelta(days=1)
    month_from = timezone.make_aware(
        datetime.datetime.combine(month_start, datetime.time.min))
    day_total = Transaction.objects.filter(
        actor=actor, tx_type="topup", currency=currency,
        created_at__gte=day_start, created_at__lt=tomorrow,
    ).aggregate(Sum("amount"))["amount__sum"] or 0
    month_total = Transaction.objects.filter(
        actor=actor, tx_type="topup", currency=currency, created_at__gte=month_from,
    ).aggregate(Sum("amount"))["amount__sum"] or 0
    if day_total + amount > daily_limit:
        audit("wallet.topup.limit_block", request, {"scope": "daily", "currency": currency, "attempt": float(amount), "used": float(day_total)})
        return Response({"error": _("Daily limit exceeded"), "code": "daily_limit_exceeded"},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
    if month_total + amount > monthly_limit:
        audit("wallet.topup.limit_block", request, {"scope": "monthly", "currency": currency, "attempt": float(amount), "used": float(month_total)})
        return Response({"error": _("Monthly limit exceeded"), "code": "monthly_limit_exceeded"},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
    return None


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
    """
    from .utils import user_id_from_account_number

    user_id = user_id_from_account_number(identifier)
    if user_id is not None:
        return get_object_or_404(User, id=user_id)
    return get_object_or_404(User, email=identifier)


class WalletBalanceView(APIView):
    """`GET /api/wallet/balance/` — the caller's own wallet.

    Answers `{id, owner, owner_type, balance, currency, created_at, updated_at, held,
    held_for, account_number}` (see `WalletSerializer`). `held` is money in escrow for
    a coaching month — out of the balance, not spent — and `held_for` names the
    trainer it is held for; both are `0.00`/null for anyone who is not a client with a
    live hold. `account_number` is what the TopUp screen tells the client to give an
    agent.

    A read of what the caller already has, with no price and no way to buy anything,
    so it takes no `X-App-Channel` and is identical on every build.
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


class WalletTransactionsView(generics.ListAPIView):
    """Transaction history for the caller's own wallet.

    Was an APIView returning a bare JSON array hard-sliced at [:200]: the only list
    endpoint in the API without the {count, next, previous, results} envelope, and a
    user with more than 200 transactions could never reach the older ones — there was
    no next link and nothing said the list had been truncated. It is a financial
    ledger; silently hiding rows is not acceptable.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = TransactionSerializer

    def get_queryset(self):
        wallet, _created = Wallet.objects.get_or_create(
            owner=self.request.user,
            defaults={"owner_type": getattr(self.request.user, "user_type", "client")},
        )
        self._wallet = wallet
        return (
            Transaction.objects
            .filter(Q(source_wallet=wallet) | Q(destination_wallet=wallet))
            # ...__owner too: the serializer reads `source_wallet.owner.username`,
            # which is one hop past the end of this chain, so the ledger cost two
            # extra queries PER ROW — 50 queries for a 20-row page of the financial
            # history screen.
            .select_related("source_wallet__owner", "destination_wallet__owner")
            .order_by("-created_at", "-id")  # total order: -created_at alone repeats rows across pages
        )

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
        response = super().list(request, *args, **kwargs)
        # Audit the access, not the row count: counting used to run a second COUNT
        # query over the whole ledger on every view.
        audit("wallet.transactions.view", request, {"wallet_id": self._wallet.id})
        return response


class WalletTransactionDetailView(generics.RetrieveAPIView):
    """`GET /api/wallet/transactions/{reference_id}/` — one row of the caller's ledger.

    The same shape as a row of the list (`TransactionSerializer`), so the activity
    detail sheet needs no second contract: `kind`, `reason`, `direction`,
    `signed_amount`, `counterparty`, the amount and currency, `money_type`, the
    reference and the raw `metadata`.

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
    is suspended is not sent a customer. `?city=` filters, case-insensitively, to the
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
        qs = (AgentProfile.objects
              .filter(is_listed=True, status="active")
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

    200 `{reference_id, balance, currency}`. Refusals (branch on `code`):

    | Status | `code` | Meaning |
    |---|---|---|
    | 400 | `validation_error` | a body field is missing or malformed; a missing or unknown `currency` is `field_errors.currency` |
    | 401 | `not_authenticated` / `token_not_valid` | no JWT, or a JWT that is not valid (the framework's answer) |
    | 401 | `agent_auth_invalid` | `X-Agent-Auth` is missing, malformed, stale, names no active key, or its timestamp or signature does not match the body. One code for all of them, on purpose: the code adds no way to tell an unknown key from a bad signature |
    | 403 | `agents_only` | the caller is not an agent |
    | 403 | `agent_ip_not_allowed` | the request did not come from an address on the agent's allow-list |
    | 403 | `agent_not_active` | the agent's profile is suspended |
    | 404 | `not_found` | `client_identifier` names nobody |
    | 400 | `target_not_a_client` | it names an account that is not a client |
    | 409 | `currency_mismatch` | the client trades in the other currency; `client_currency` and `topup_currency` say which |
    | 429 | `daily_limit_exceeded` / `monthly_limit_exceeded` | the agent's cap FOR THAT CURRENCY; waiting does not lift it before the day or month turns |
    | 409 | `request_in_flight` | the first request with this `idempotency_key` is still running; retry when it finishes |
    | 422 | `idempotency_key_reused` | the key was used for a different request; retrying never helps |
    | 429 | `throttled` | the rate limiter (`Retry-After`) |

    Every code above that is not the framework's was the status default (`bad_request`,
    `not_authenticated`, `permission_denied`, `conflict`, `unprocessable`, `throttled`)
    until the owner's decision of 2026-09-24.

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

        ip = request.META.get("REMOTE_ADDR")
        if api_key.agent.ip_allowlist and ip not in api_key.agent.ip_allowlist:
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
        with transaction.atomic():
            agent_profile = _lock_agent_profile(agent_profile)
            if agent_profile.status != "active":
                return Response({"error": _("Agent not active"), "code": "agent_not_active"},
                                status=status.HTTP_403_FORBIDDEN)

            limit_error = _topup_limit_error(agent_profile, request.user, amount, request,
                                             currency)
            if limit_error:
                return limit_error

            # --- Reserve idempotency key only after validation passes ---
            try:
                idem, replay = idempotency.reserve(
                    request.user, idem_key,
                    idempotency.fingerprint(op="topup", client=client.id, amount=amount,
                                            currency=currency),
                )
            except idempotency.IdempotencyConflict as exc:
                return _idempotency_conflict(exc)
            if replay is not None:
                audit("wallet.topup.idempotent_hit", request, {"key": idem_key})
                return Response(replay, status=status.HTTP_200_OK)

            # Never `wallet_for(..., currency)`: arriving money does not choose a
            # person's currency. `currency=` makes the ledger refuse a wallet that
            # changed unit since the check above.
            client_wallet = wallet_for(client, "client")
            tx = move_funds_atomic(
                None,
                client_wallet,
                amount,
                actor_id=request.user.id,
                tx_type="topup",
                currency=currency,
                money_type=MoneyType.WALLET_MOVE,
                metadata={"agent_id": request.user.id, "api_key_id": getattr(api_key, "key_id", None)},
            )
            # refresh balance from DB to reflect updated value
            client_wallet.refresh_from_db()
            resp = {"reference_id": tx.reference_id, "balance": str(client_wallet.balance),
                    "currency": tx.currency}
            idempotency.complete(idem, resp)

        audit("wallet.topup.success", request, {"reference_id": tx.reference_id, "client_id": client.id, "amount": float(amount), "currency": currency})
        return Response(resp, status=status.HTTP_200_OK)


class ClientTransferToTrainerView(APIView):
    """`POST /api/wallet/client/transfer/` — a client sends money to a trainer.

    Body: `trainer_id`, `amount`, `idempotency_key`. Headers: `X-App-Channel`.

    200 `{reference_id, client_balance, trainer_balance}`. Refusals (branch on `code`):

    | Status | `code` | Meaning |
    |---|---|---|
    | 400 | `app_channel_required` | no known `X-App-Channel` |
    | 403 | `not_available_on_channel` | this build may not move wallet money |
    | 400 | `validation_error` | a body field is missing or malformed (`field_errors`) |
    | 403 | `clients_only` | the caller is not a client (the code the coaching and diet routes answer for the same fact) |
    | 404 | `not_found` | `trainer_id` names no trainer |
    | 409 | `currency_mismatch` | the two trade in different currencies (nothing is converted on a transfer); `client_currency` and `trainer_currency` say which |
    | 402 | `insufficient_balance` | the client's wallet holds less than `amount`; `currency` is its currency |
    | 400 | `transfer_refused` | the ledger refused the movement for any other reason; nothing moved |
    | 409 | `request_in_flight` | the first request with this `idempotency_key` is still running |
    | 422 | `idempotency_key_reused` | the key was used for a different request |
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

        serializer = TransferRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # --- Pre-side-effect validation before reserving the idempotency key ---
        if getattr(request.user, "user_type", None) != "client":
            return Response({"error": _("Only clients can initiate transfers"),
                             "code": "clients_only"}, status=status.HTTP_403_FORBIDDEN)

        trainer = get_object_or_404(User, id=serializer.validated_data["trainer_id"], user_type="trainer")

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

        idem_key = serializer.validated_data["idempotency_key"]
        try:
            idem, replay = idempotency.reserve(
                request.user, idem_key,
                idempotency.fingerprint(
                    op="transfer", trainer=trainer.id,
                    amount=serializer.validated_data["amount"],
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
            tx = move_funds_atomic(client_wallet, trainer_wallet, amount, actor_id=request.user.id, tx_type="transfer",
                                   money_type=MoneyType.WALLET_MOVE, metadata={"trainer_id": trainer.id})
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
        trainer_wallet.refresh_from_db()
        resp = {"reference_id": tx.reference_id, "client_balance": str(client_wallet.balance), "trainer_balance": str(trainer_wallet.balance)}
        idempotency.complete(idem, resp)

        audit("wallet.transfer.success", request, {"reference_id": tx.reference_id, "trainer_id": trainer.id, "amount": float(amount)})
        return Response(resp, status=status.HTTP_200_OK)


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

        try:
            tx = move_funds_atomic(
                original.destination_wallet, original.source_wallet, original.amount,
                actor_id=request.user.id, tx_type="reversal",
                # The kind of money that is going back is the kind that came.
                money_type=original.money_type,
                metadata={"original_reference": original.reference_id,
                          "reason": serializer.validated_data.get("reason", "")},
                reverses=original)
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


class AgentApiKeyCreateView(APIView):
    permission_classes = [IsAuthenticated, IsAgent]

    def post(self, request):
        """
        Allows an authenticated agent to create a new API key.
        Returns key_id and secret (shown ONCE — never stored in plaintext).
        The SHA-256 hash of the secret is stored in DB for HMAC verification.

        Refusals: 401 (the framework's), 403 `agents_only` — the caller is not an agent.
        """
        import hashlib
        agent_profile = ensure_agent_profile(request.user)
        key_id = uuid.uuid4().hex[:16]
        # Generate high-entropy secret (64 hex chars = 256 bits)
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        # Non-signing lookup/compat digest; the raw secret is stored encrypted (Fernet).
        hashed_key = hashlib.sha256(secret.encode("utf-8")).hexdigest()

        # Deactivate, then create, ATOMICALLY and under the agent's own row lock. The
        # "single active key policy" this comment describes was enforced by nothing: an
        # unlocked `update()` followed by a `create()` with no transaction and no unique
        # index, so two concurrent POSTs both deactivated and both created — the agent
        # then held two valid signing credentials, and revoking the one they knew about
        # left the other live. A partial unique index now makes the database say it too.
        with transaction.atomic():
            AgentProfile.objects.select_for_update().filter(pk=agent_profile.pk).first()
            prev_ids = list(AgentAPIKey.objects.filter(
                agent=agent_profile, is_active=True).values_list("key_id", flat=True))
            if prev_ids:
                AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).update(
                    is_active=False)

            AgentAPIKey.objects.create(
                agent=agent_profile, key_id=key_id, hashed_key=hashed_key,
                secret_ciphertext=encrypt_secret(secret), is_active=True,
            )
        if prev_ids:
            audit("wallet.agent.apikey.rotated", request, {"deactivated": prev_ids})
        audit("wallet.agent.apikey.create", request, {"key_id": key_id})
        # Return secret ONCE — client must store it securely
        return Response({"key_id": key_id, "secret": secret}, status=status.HTTP_201_CREATED)


class AgentApiKeyStatusView(APIView):
    """Returns whether the authenticated agent has at least one active API key.

    Does not expose secrets. Useful for onboarding flow in mobile app.

    Refusals: 401 (the framework's), 403 `agents_only` — the caller is not an agent.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def get(self, request):
        agent_profile = ensure_agent_profile(request.user)
        has_active = AgentAPIKey.objects.filter(agent=agent_profile, is_active=True).exists()
        return Response({"has_active": has_active})


class AgentApiKeyEnsureView(APIView):
    """Ensures the authenticated agent has an active API key.

    If none exists, creates one and returns key_id (not secret). If exists, returns the current active key_id.

    Refusals: 401 (the framework's), 403 `agents_only` — the caller is not an agent.
    """
    permission_classes = [IsAuthenticated, IsAgent]

    def post(self, request):
        import hashlib
        agent_profile = ensure_agent_profile(request.user)
        # `-created_at, -id`: which key counts as current must not depend on
        # which row Postgres happens to return when two share a timestamp.
        api_key = AgentAPIKey.objects.filter(
            agent=agent_profile, is_active=True).order_by("-created_at", "-id").first()
        if api_key:
            return Response({"created": False})
        # Create without returning secret (kept server-side only, encrypted at rest)
        key_id = uuid.uuid4().hex[:16]
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        hashed_key = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        AgentAPIKey.objects.create(
            agent=agent_profile, key_id=key_id, hashed_key=hashed_key,
            secret_ciphertext=encrypt_secret(secret), is_active=True,
        )
        audit("wallet.agent.apikey.ensure", request, {"key_id": key_id})
        return Response({"created": True}, status=status.HTTP_201_CREATED)

class AgentTopUpProxyView(APIView):
    """Server-side signing proxy for agent top-ups.

    Mobile calls this endpoint with JWT; the server signs using the stored secret and executes the top-up flow directly.

    Body: `client_identifier`, `amount`, `currency` ("SYP" | "USD" — the currency of the
    CASH the agent took, required), `idempotency_key`. Headers: `X-App-Channel`.

    200 `{reference_id, balance, currency}`. Refusals (branch on `code`) — the same
    rules as `AgentTopUpView`, less the signature:

    | Status | `code` | Meaning |
    |---|---|---|
    | 400 | `app_channel_required` | no known `X-App-Channel` |
    | 403 | `not_available_on_channel` | this build may not move wallet money |
    | 400 | `validation_error` | a body field is missing or malformed; a missing or unknown `currency` is `field_errors.currency` |
    | 403 | `agents_only` | the caller is not an agent |
    | 403 | `agent_not_active` | the agent's profile is suspended |
    | 404 | `not_found` | `client_identifier` names nobody |
    | 400 | `target_not_a_client` | it names an account that is not a client |
    | 409 | `currency_mismatch` | the client trades in the other currency; `client_currency` and `topup_currency` say which |
    | 429 | `daily_limit_exceeded` / `monthly_limit_exceeded` | the agent's cap FOR THAT CURRENCY |
    | 409 | `request_in_flight` | the first request with this `idempotency_key` is still running |
    | 422 | `idempotency_key_reused` | the key was used for a different request |
    | 429 | `throttled` | the rate limiter (`Retry-After`) |

    `agents_only`, `agent_not_active`, `target_not_a_client`, the two cap codes and the
    two idempotency codes answered only their status defaults until the owner's
    decision of 2026-09-24.
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
        # removed). Security rests on JWT auth, agent status, the fail-closed
        # top-up caps, and idempotency.
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
        with transaction.atomic():
            agent_profile = _lock_agent_profile(agent_profile)
            if agent_profile.status != "active":
                return Response({"error": _("Agent not active"), "code": "agent_not_active"},
                                status=status.HTTP_403_FORBIDDEN)

            limit_error = _topup_limit_error(agent_profile, request.user, amount, request,
                                             currency)
            if limit_error:
                return limit_error

            # --- Reserve idempotency key only after validation passes ---
            try:
                idem, replay = idempotency.reserve(
                    request.user, idempotency_key,
                    idempotency.fingerprint(op="topup", client=client.id, amount=amount,
                                            currency=currency),
                )
            except idempotency.IdempotencyConflict as exc:
                return _idempotency_conflict(exc)
            if replay is not None:
                audit("wallet.topup.idempotent_hit", request, {"key": idempotency_key})
                return Response(replay, status=status.HTTP_200_OK)

            client_wallet = wallet_for(client, "client")   # see AgentTopUpView
            tx = move_funds_atomic(None, client_wallet, amount, actor_id=request.user.id, tx_type="topup",
                                   currency=currency,
                                   money_type=MoneyType.WALLET_MOVE, metadata={"agent_id": request.user.id})
            client_wallet.refresh_from_db()
            resp = {"reference_id": tx.reference_id, "balance": str(client_wallet.balance),
                    "currency": tx.currency}
            idempotency.complete(idem, resp)

        audit("wallet.topup.success", request, {"reference_id": tx.reference_id, "client_id": client.id, "amount": float(amount), "currency": currency})
        return Response(resp, status=status.HTTP_200_OK)

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



