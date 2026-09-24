"""A trainer's withdrawals and payout methods (BACKEND_BUGS B-098; the spec, section 8
"Trainer payouts"; the design is `15-trainer-money/TrainerWithdraw`,
`TrainerWithdrawStatus` and `TrainerPayoutMethod`).

Every route is for TRAINERS: anyone else is answered 403 `not_a_trainer`, the code every
trainer route answers for that. Every refusal is `{error, code, ...}`; branch on `code`.

What differs by build: only the two routes that MOVE money — asking for a withdrawal
and cancelling one — call `require_purchase_allowed(request, WALLET_MOVE)` first, so a
build that may not move wallet money (`can_transfer_wallet` false: the first iOS build,
by the spec's table) is answered 403 `not_available_on_channel`, and a request that
declares no known `X-App-Channel` 400 `app_channel_required`. The setup says so in
advance in `refusal`. Everything else — reading, and managing payout methods — is the
trainer's own record, answered identically on every build.
"""
from django.db.models import Prefetch
from django.utils.translation import gettext as _
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from policy.exceptions import AppChannelRequired, NotAvailableOnChannel
from policy.money import MoneyType
from policy.services import require_purchase_allowed
from training_platform.query_params import body_dict
from wallet import idempotency
from wallet.throttles import ChargingRateThrottle

from . import services
from .models import PayoutMethod, Withdrawal, WithdrawalEvent
from .serializers import (PayoutMethodSerializer, WithdrawalRequestSerializer,
                          WithdrawalSerializer)


def _not_a_trainer():
    """403 `not_a_trainer` — the caller is not a trainer."""
    return Response({"error": _("This endpoint is only for trainers"),
                     "code": "not_a_trainer"}, status=status.HTTP_403_FORBIDDEN)


def _refused(exc: services.Refused):
    return Response({"error": str(exc.message), "code": exc.code, **exc.extra},
                    status=exc.status)


def _is_trainer(request) -> bool:
    return getattr(request.user, "user_type", None) == "trainer"


_money = services.money


def _withdrawals():
    return (Withdrawal.objects
            .select_related("destination_agent", "destination_agent__user")
            .prefetch_related(Prefetch("events",
                                       queryset=WithdrawalEvent.objects.order_by("created_at",
                                                                                 "id"))))


class WithdrawalSetupView(APIView):
    """`GET /api/v1/trainer/withdrawals/setup/` — what the withdraw screen draws before
    an amount is chosen (`TrainerWithdraw`).

    200 `{currency, balance, available, settling, settling_until, hold_days,
    in_withdrawal, minimum, daily_cap, daily_cap_left, fee, verified, open_withdrawal,
    methods, refusal}`:

    - `available` = `balance - settling`: what a withdrawal may take now. `settling` is
      coaching money released in the last `hold_days` days, not withdrawable yet;
      `settling_until` when the next of it becomes available (ISO 8601, or null).
      `in_withdrawal` is the open withdrawal's money, which already left `balance`.
    - `minimum`, `daily_cap`, `daily_cap_left` in `currency`; `fee` is always `0.00`
      (the design's «لا رسوم من المنصة على السحب»).
    - `open_withdrawal`: the one in progress (a withdrawal body, see the detail route)
      or null — one at a time.
    - `methods`: the saved payout methods, default first (see `payout-methods/`).
    - `refusal`: the code a request made now would be refused with whatever the amount,
      or null — `app_channel_required` / `not_available_on_channel` (THIS BUILD may not
      move wallet money), `trainer_not_verified`, `withdrawal_already_open`,
      `payout_method_required`, `daily_limit_exceeded`, `withdrawal_below_minimum`
      (less is available than the minimum). A hint: the request makes its own checks.

    Headers: `X-App-Channel` (only `refusal` depends on it). Refusals: 401 (the
    framework's), 403 `not_a_trainer`.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_trainer(request):
            return _not_a_trainer()
        build_refusal = None
        try:
            require_purchase_allowed(request, MoneyType.WALLET_MOVE)
        except (AppChannelRequired, NotAvailableOnChannel) as exc:
            build_refusal = exc.default_code
        data = services.setup_for(request.user, build_refusal=build_refusal)
        until = data["settling_until"]
        return Response({
            **{key: _money(data[key]) for key in ("balance", "available", "settling",
                                                  "in_withdrawal", "minimum", "daily_cap",
                                                  "daily_cap_left", "fee")},
            "currency": data["currency"],
            "settling_until": until.isoformat() if until else None,
            "hold_days": data["hold_days"],
            "verified": data["verified"],
            "open_withdrawal": (WithdrawalSerializer(
                _withdrawals().get(pk=data["open_withdrawal"].pk)).data
                if data["open_withdrawal"] is not None else None),
            "methods": PayoutMethodSerializer(data["methods"], many=True).data,
            "refusal": data["refusal"],
        })


class WithdrawalListView(generics.ListAPIView):
    """`GET`/`POST /api/v1/trainer/withdrawals/`.

    **GET** — the trainer's withdrawals, newest first, in the paginated envelope
    `{count, next, previous, results}`; each result is a withdrawal body (see the detail
    route). Refusals: 401, 403 `not_a_trainer`.

    **POST** — ask for a withdrawal. Headers: `X-App-Channel`. Body: `amount` (in the
    trainer's currency), `method_id` (one of their payout methods; the default one when
    left out), `idempotency_key` (the caller's own, as on every wallet write: a retry of
    a request that landed answers 200 with the SAME withdrawal and moves nothing).

    The amount leaves the wallet AT ONCE, into payout clearing, so it cannot be spent or
    withdrawn twice; if the withdrawal fails or is cancelled it comes back. 201 with the
    withdrawal body (`state: "requested"`, a number like `WD-2026-09-23-041`), and the
    push `withdrawal_requested`.

    | Status | `code` | Meaning |
    |---|---|---|
    | 400 | `app_channel_required` | no known `X-App-Channel` |
    | 403 | `not_available_on_channel` | this build may not move wallet money (the first iOS build) |
    | 403 | `not_a_trainer` | the caller is not a trainer |
    | 400 | `validation_error` | `amount` or `idempotency_key` missing or malformed (`field_errors`) |
    | 403 | `trainer_not_verified` | only verified trainers withdraw (spec section 8); verify first (`/api/v1/trainer/verification/`) |
    | 409 | `payout_method_required` | no `method_id` and no default method: add one |
    | 404 | `payout_method_not_found` | `method_id` is not one of the caller's methods |
    | 409 | `payout_agent_unavailable` | the method's agent is no longer listed or active |
    | 400 | `withdrawal_below_minimum` | below the minimum; `minimum`, `currency` |
    | 409 | `withdrawal_already_open` | one request at a time; `withdrawal` `{id, number, state}` is the open one |
    | 429 | `daily_limit_exceeded` | over what is left of today's cap; `daily_cap`, `daily_cap_left`, `currency`. Waiting does not lift it before the day turns (the agent cap's code, same meaning) |
    | 402 | `insufficient_balance` | more than the balance; `currency` |
    | 409 | `amount_exceeds_available` | within the balance, but part of it is settling; `available`, `settling`, `settling_until`, `currency` |
    | 409 | `request_in_flight` | the first request with this key is still running |
    | 422 | `idempotency_key_reused` | the key was used for a different request |
    | 429 | `throttled` | the rate limiter (`Retry-After`) |
    """
    permission_classes = [IsAuthenticated]
    serializer_class = WithdrawalSerializer

    def get_throttles(self):
        # Only the request moves money; reading the list is not rate-limited as a charge.
        if self.request.method == "POST":
            return [ChargingRateThrottle()]
        return super().get_throttles()

    def get_queryset(self):
        return _withdrawals().filter(trainer=self.request.user).order_by("-requested_at",
                                                                         "-id")

    def list(self, request, *args, **kwargs):
        if not _is_trainer(request):
            return _not_a_trainer()
        return super().list(request, *args, **kwargs)

    def post(self, request):
        # Before anything else: a build that may not move wallet money is told so with
        # the same code whatever it sent (policy/services.py).
        require_purchase_allowed(request, MoneyType.WALLET_MOVE)
        if not _is_trainer(request):
            return _not_a_trainer()
        body = WithdrawalRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            withdrawal, replayed = services.request_withdrawal(
                request.user, amount=body.validated_data["amount"],
                method_id=body.validated_data.get("method_id"),
                idempotency_key=body.validated_data["idempotency_key"])
        except services.Refused as exc:
            return _refused(exc)
        except idempotency.IdempotencyConflict as exc:
            from wallet.views import _idempotency_conflict

            return _idempotency_conflict(exc)
        return Response(WithdrawalSerializer(_withdrawals().get(pk=withdrawal.pk)).data,
                        status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED)


class WithdrawalDetailView(APIView):
    """`GET /api/v1/trainer/withdrawals/{id}/` — one of the caller's withdrawals
    (`TrainerWithdrawStatus`).

    200 `{id, number, amount, currency, state, destination, method_id,
    transfer_reference, failure_reason, can_cancel, requested_at, approved_at, sent_at,
    confirmed_at, failed_at, cancelled_at, timeline}`. `state` goes `requested` →
    `approved` → `sent` → `confirmed` (arrived), or `failed` (the money is back in the
    balance, `failure_reason` says why), or `cancelled` by the trainer while
    `requested`. `destination` `{kind, address, holder_name, agent}` is where it goes as
    it was when requested. `timeline` `[{state, at}]`, oldest first.

    Refusals: 401, 403 `not_a_trainer`, 404 `withdrawal_not_found` (not the caller's, or
    none — the two are one answer). Identical on every build.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, withdrawal_id):
        if not _is_trainer(request):
            return _not_a_trainer()
        withdrawal = _withdrawals().filter(pk=withdrawal_id, trainer=request.user).first()
        if withdrawal is None:
            return Response({"error": _("No such withdrawal."),
                             "code": "withdrawal_not_found"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(WithdrawalSerializer(withdrawal).data)


class WithdrawalCancelView(APIView):
    """`POST /api/v1/trainer/withdrawals/{id}/cancel/` — take back a withdrawal nobody
    has approved yet. The money comes back to the balance at once. Headers:
    `X-App-Channel`. No body.

    200 with the withdrawal body (`state: "cancelled"`); cancelling one already
    cancelled answers the same 200 and moves nothing.

    | Status | `code` | Meaning |
    |---|---|---|
    | 400 | `app_channel_required` | no known `X-App-Channel` |
    | 403 | `not_available_on_channel` | this build may not move wallet money |
    | 403 | `not_a_trainer` | the caller is not a trainer |
    | 404 | `withdrawal_not_found` | not the caller's, or none |
    | 409 | `withdrawal_not_cancellable` | it is past `requested`; `state` says where it is |
    | 409 | `withdrawal_hold_reversed` | support already put this withdrawal's money back in the balance, so cancelling would return it twice; nothing moved, and support closes it. `state` as above |
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, withdrawal_id):
        # It moves money (clearing -> the trainer's wallet), so the build is asked first.
        require_purchase_allowed(request, MoneyType.WALLET_MOVE)
        if not _is_trainer(request):
            return _not_a_trainer()
        try:
            withdrawal = services.cancel(withdrawal_id, request.user)
        except services.Refused as exc:
            return _refused(exc)
        return Response(WithdrawalSerializer(_withdrawals().get(pk=withdrawal.pk)).data)


class PayoutMethodListView(APIView):
    """`GET`/`POST /api/v1/trainer/payout-methods/` — where the trainer's money goes
    (`TrainerPayoutMethod`).

    **GET** — `{results: [method]}`, default first. A method is `{id, kind, address,
    holder_name, agent, is_default, verified, verified_at, created_at}`: `kind`
    `shamcash` (an `address`, grouped in fours, and its `holder_name`) or `agent_cash`
    (cash collected from `agent`, the listing shape of `GET /api/wallet/agents/`).
    `verified` is true once money sent to it arrived.

    **POST** — save one. Body for ShamCash: `{kind: "shamcash", address,
    address_confirm, holder_name, is_default?}` — the address typed twice. For cash:
    `{kind: "agent_cash", agent_id, is_default?}` — `agent_id` from
    `GET /api/wallet/agents/`. An ADDRESS only: no ShamCash password, PIN or code is
    ever asked for, and a body carrying one is not read. The first method is the
    default; `is_default: true` makes a later one the default. 201 with the method.

    | Status | `code` | Meaning |
    |---|---|---|
    | 403 | `not_a_trainer` | the caller is not a trainer |
    | 400 | `payout_method_kind_invalid` | `kind` is neither; `kinds` lists them |
    | 400 | `payout_address_required` | no address |
    | 400 | `payout_address_invalid` | not 8-64 letters and digits once spaces and dashes are removed |
    | 400 | `payout_address_mismatch` | the two addresses differ |
    | 400 | `payout_holder_name_required` | no holder name |
    | 400 | `payout_holder_name_too_long` | over `max_length` (120) |
    | 400 | `payout_agent_unavailable` | `agent_id` names no listed, active agent |
    | 409 | `payout_method_exists` | this address (or agent) is already saved; `id` is that method |
    | 409 | `payout_methods_limit` | the trainer already keeps `max` methods |

    A change applies to NEW requests: a withdrawal keeps the destination it was asked
    with. Identical on every build.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_trainer(request):
            return _not_a_trainer()
        methods = (PayoutMethod.objects.filter(trainer=request.user)
                   .select_related("agent", "agent__user")
                   .order_by("-is_default", "-created_at", "-id"))
        return Response({"results": PayoutMethodSerializer(methods, many=True).data})

    def post(self, request):
        if not _is_trainer(request):
            return _not_a_trainer()
        body = body_dict(request)
        try:
            method = services.add_method(
                request.user, kind=str(body.get("kind") or ""),
                address=body.get("address") or "",
                address_confirm=body.get("address_confirm") or "",
                holder_name=body.get("holder_name") or "",
                agent_id=body.get("agent_id"),
                make_default=body.get("is_default") in (True, "true", "True", "1", 1))
        except services.Refused as exc:
            return _refused(exc)
        return Response(PayoutMethodSerializer(method).data, status=status.HTTP_201_CREATED)


class PayoutMethodDetailView(APIView):
    """`DELETE /api/v1/trainer/payout-methods/{id}/` — forget one. 204. If it was the
    default, the newest remaining method becomes the default. A withdrawal already asked
    with it keeps its own copy of the destination.

    Refusals: 401, 403 `not_a_trainer`, 404 `payout_method_not_found`. Identical on
    every build.
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, method_id):
        if not _is_trainer(request):
            return _not_a_trainer()
        try:
            services.remove_method(request.user, method_id)
        except services.Refused as exc:
            return _refused(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PayoutMethodDefaultView(APIView):
    """`POST /api/v1/trainer/payout-methods/{id}/default/` — make this the default. No
    body. 200 with the method (`is_default: true`); already the default is the same 200.

    Refusals: 401, 403 `not_a_trainer`, 404 `payout_method_not_found`. Identical on
    every build.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, method_id):
        if not _is_trainer(request):
            return _not_a_trainer()
        try:
            method = services.set_default(request.user, method_id)
        except services.Refused as exc:
            return _refused(exc)
        method = PayoutMethod.objects.select_related("agent", "agent__user").get(pk=method.pk)
        return Response(PayoutMethodSerializer(method).data)
