"""Checkout sessions: how the web payment hub buys (the payments spec, section 5).

    GET, POST  /api/v1/checkout/sessions/        CheckoutSessionsView
    GET        /api/v1/checkout/sessions/{id}/   CheckoutSessionDetailView

(Both spellings of the collection answer, with and without the final slash: the spec
names it without.)

A SESSION IS A `subscription.Payment` — the mapping of the spec's session and its states
onto the row is `PaymentService`'s docstring. Nothing here writes a payment: a plan's
session is opened by `PaymentService.start_payment` / `start_renewal`, the calls the
app's Pay and Renew buttons make (one way to open a plan checkout), and a routine
product's by `PaymentService.start_product_checkout`; both open through the payment step
(`subscription/payment_step.py`, the spec's section 6). They are confirmed, held for
review, expired, rejected and refunded exactly as every other checkout is — the pollers,
the webhook, "check my payment" (`POST /api/subscription/v1/payments/{id}/reconcile/`),
the review queue and the console.

Whatever renders the hub — the Flutter web build or a separate site — is built from
these docstrings. Every refusal names its own `code`; nothing here is cached
(`training_platform/cache_config.py` lists none of it).
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from policy.channels import channel_of
from policy.exceptions import AppChannelRequired, NotAvailableOnChannel
from policy.money import STORE_BILLED_TYPES, MoneyType
from policy.services import require_purchase_allowed
from training_platform.query_params import body_dict
from wallet.exchange import ExchangeRateUnavailable, refusal_body
from wallet.throttles import ChargingRateThrottle

from .models import OPEN_PAYMENT_STATUSES, Payment, Subscription, SubscriptionPlan, paid_by
from .payment_step import PaymentStepUnavailable, payment_step
from .services.payment_gateways import CurrencyNotChargeable, PaymentGatewayError
from .services.payment_service import (
    By, CheckoutOpen, PaymentError, PaymentService, PlanChanged, SubscriptionAlreadyActive,
)
from .utils import checkout_expires_at

logger = logging.getLogger(__name__)

#: The kinds of money a checkout session sells: the two digital goods (`policy.money.
#: STORE_BILLED_TYPES`). Coaching and wallet money are paid from the wallet, never here.
SESSION_MONEY_TYPES = tuple(sorted(STORE_BILLED_TYPES))

#: `Payment.status` -> the session's state, in the spec's words (section 7): OPEN,
#: NEEDS_REVIEW, CONFIRMED, REJECTED, EXPIRED, REFUNDED. MATCHING is never stored: it
#: happens inside one poll and ends in the same transaction.
SESSION_STATES = {
    'pending': 'open', 'processing': 'open', 'authorized': 'open',
    'needs_review': 'needs_review',
    'completed': 'confirmed',
    'rejected': 'rejected',
    'expired': 'expired', 'failed': 'expired', 'cancelled': 'expired',
    'refunded': 'refunded',
}


class SessionRequestSerializer(serializers.Serializer):
    money_type = serializers.ChoiceField(choices=[(t, t) for t in SESSION_MONEY_TYPES])
    item_id = serializers.IntegerField(min_value=1)


def _may_buy(request, money_type) -> bool:
    """Whether THIS BUILD may buy this kind of money — the one test for showing a
    session's price, reference and payment target. By build only (rule zero)."""
    try:
        require_purchase_allowed(request, money_type)
    except (AppChannelRequired, NotAvailableOnChannel):
        return False
    return True


def _sessions_of(user):
    """The caller's sessions: their checkouts that were offered (they have a reference).
    Audit rows a cancellation writes have none and are not sessions."""
    return (Payment.objects.filter(paid_by(user))
            .exclude(gateway_transaction_reference__isnull=True)
            .exclude(gateway_transaction_reference='')
            .select_related('routine_product'))


def _item(payment) -> dict:
    if payment.routine_product_id is not None:
        product = payment.routine_product
        snapshot = (payment.metadata or {}).get('product') or {}
        title = (product.title_in(get_language()) if product is not None
                 else snapshot.get('title_en') or snapshot.get('title_ar') or '')
        return {'type': 'routine_product', 'id': payment.routine_product_id, 'name': title}
    plan = payment.plan_bought()
    return {'type': 'plan', 'id': plan['plan_id'], 'name': plan['plan_name'],
            'duration_days': plan['duration_days']}


def _result(payment):
    """What a confirmed (or refunded) session bought: the subscription, or the purchase
    and the buyer's copy. Null while it is not confirmed."""
    if payment.status not in ('completed', 'refunded'):
        return None
    if payment.routine_product_id is None:
        return {'subscription_id': str(payment.subscription_id)}
    from routine.product_models import RoutinePurchase

    purchase = (RoutinePurchase.objects.select_related('routine')
                .filter(checkout_reference=payment.gateway_transaction_reference).first())
    if purchase is None:
        return None
    return {'purchase_id': purchase.pk, 'purchase_status': purchase.status,
            'routine_id': getattr(getattr(purchase, 'routine', None), 'pk', None)}


def session_payload(payment, request, may_buy=None) -> dict:
    """One session as the hub reads it (see `CheckoutSessionsView`). `may_buy` is
    `_may_buy` for each kind of money, when the caller has asked it once for a list."""
    from .views import review_of

    body = {
        'id': str(payment.id),
        'money_type': payment.money_type,
        'item': _item(payment),
        'kind': (payment.metadata or {}).get('kind') or 'initial',
        'status': SESSION_STATES.get(payment.status, payment.status),
        'payment_status': payment.status,
        'currency': payment.currency,
        'expires_at': checkout_expires_at(payment),
        'review': review_of(payment),
        'result': _result(payment),
        'created_at': payment.created_at,
    }
    shows = (may_buy[payment.money_type] if may_buy is not None
             else _may_buy(request, payment.money_type))
    if shows:
        # How to pay, and how much. Never to a build that may not sell this kind of
        # money (the spec, section 4: no price, payment instruction or hub for it).
        body['amount'] = str(payment.amount)
        body['reference'] = payment.gateway_transaction_reference
        body['payment_target'] = payment_step(payment.payment_method).target(payment)
    return body


def _refusal(message, code, http_status, **extra):
    return Response({'error': message, 'code': code, **extra}, status=http_status)


def _price_changed(amount, currency):
    return _refusal(_('The price has changed. Please refresh and try again.'),
                    'price_changed', status.HTTP_409_CONFLICT,
                    expected_amount=str(amount), expected_currency=currency)


def _open_plan_session(request, plan_id):
    """A plan's session: the caller's subscription's ONE open checkout, through the Pay
    button's own call (`PaymentService.start_payment`), or the Renew button's
    (`start_renewal`) while cover runs. Returns `(payment, None)` or `(None, refusal)`."""
    from .views import (_checkout_open, _checkout_opener, _opened_from, _plan_moved,
                        move_plan_while_idle)

    plan = SubscriptionPlan.objects.filter(pk=plan_id, is_active=True).first()
    if plan is None:
        return None, _refusal(_('Invalid or inactive plan'), 'invalid_plan',
                              status.HTTP_400_BAD_REQUEST)
    if plan.price <= 0:
        return None, _refusal(_('This plan does not require a payment'),
                              'payment_not_required', status.HTTP_400_BAD_REQUEST)
    user = request.user
    subscription = Subscription.objects.select_related('plan').filter(user=user).first()
    if subscription is None:
        # Choosing the plan, as `POST v1/subscriptions/` does: a pending subscription.
        try:
            with transaction.atomic():
                subscription = Subscription.objects.create(user=user, plan=plan)
        except IntegrityError:
            # The other tap created it first.
            subscription = Subscription.objects.select_related('plan').get(user=user)
    if subscription.plan_id != plan.pk:
        # Another plan, as `change-plan/` moves it: only while nothing runs and no
        # checkout is open. A dearer plan while cover runs is `upgrade/`.
        subscription, _changed, refusal = move_plan_while_idle(subscription.pk, plan)
        if refusal is not None:
            return None, refusal

    opener = _checkout_opener(request, subscription, 'shamcash',
                              payment_step('shamcash').service)
    opened = {'by': By.CUSTOMER, 'actor': user, 'evidence': _opened_from(request)}
    try:
        try:
            payment = PaymentService.start_payment(subscription, opener,
                                                   expected_plan_id=plan.pk, **opened)
        except SubscriptionAlreadyActive:
            payment = PaymentService.start_renewal(subscription, initiate=opener, **opened)
    except PlanChanged:
        return None, _plan_moved(subscription.pk, request, _price_changed)
    except CheckoutOpen as exc:
        held = exc.payment
        if (held.status == 'needs_review' and (held.metadata or {}).get('kind') != 'upgrade'
                and held.plan_bought()['plan_id'] == plan.pk):
            # The session for this plan, held while a person checks money that arrived
            # for it: it is still THE session, and a second request returns it.
            held.already_open = True
            return held, None
        return None, _checkout_open(held)
    except ExchangeRateUnavailable as exc:
        return None, Response(refusal_body(exc), status=status.HTTP_409_CONFLICT)
    except CurrencyNotChargeable:
        from .views import _currency_not_chargeable

        return None, _currency_not_chargeable()
    except PaymentStepUnavailable:
        return None, _gateway_unavailable()
    except PaymentGatewayError as exc:
        logger.warning("Plan session for %s not opened: %s", subscription.id, exc)
        return None, _refusal(_('Request could not be completed.'), 'gateway_error',
                              status.HTTP_400_BAD_REQUEST)
    except PaymentError:
        # `start_renewal` on a subscription it cannot renew (a trial).
        return None, _refusal(_('Request could not be completed.'), 'not_renewable',
                              status.HTTP_400_BAD_REQUEST)
    return payment, None


def _gateway_unavailable():
    return _refusal(_('That payment method is not available right now.'),
                    'gateway_unavailable', status.HTTP_503_SERVICE_UNAVAILABLE)


def _open_product_session(request, product_id):
    """A routine product's session (`PaymentService.start_product_checkout`). Refused
    exactly as the wallet's `buy/` refuses the same facts. Returns `(payment, None)` or
    `(None, refusal)`."""
    from routine import products
    from routine.product_views import _not_found, _service_refusal

    # The caller's open session for it is THE session, whatever became of the product
    # since it opened (retired, off sale): asking again answers it.
    existing = products.open_checkout_for(request.user, product_id)
    if existing is not None:
        existing.already_open = True
        return existing, None
    product = products.visible_product(request.user, product_id)
    if product is None:
        return None, _not_found()
    if product.trainer_id == request.user.pk:
        return None, _refusal(_("You cannot buy your own program."), 'own_product',
                              status.HTTP_403_FORBIDDEN)
    if not getattr(request.user, 'is_client', False):
        return None, _refusal(_("Only clients can buy programs."), 'clients_only',
                              status.HTTP_403_FORBIDDEN)
    from .views import _currency_not_chargeable, _opened_from

    step = payment_step('shamcash')
    try:
        payment = PaymentService.start_product_checkout(
            product.pk, request.user,
            step.opener(request, metadata={'product_id': str(product.pk)}),
            by=By.CUSTOMER, actor=request.user, evidence=_opened_from(request))
    except products.ProductRefusal as exc:
        return None, _service_refusal(exc)
    except ExchangeRateUnavailable as exc:
        return None, Response(refusal_body(exc), status=status.HTTP_409_CONFLICT)
    except CurrencyNotChargeable:
        return None, _currency_not_chargeable()
    except PaymentStepUnavailable:
        return None, _gateway_unavailable()
    except PaymentGatewayError as exc:
        logger.warning("Product session for %s not opened: %s", product.pk, exc)
        return None, _refusal(_('Request could not be completed.'), 'gateway_error',
                              status.HTTP_400_BAD_REQUEST)
    except IntegrityError:
        # `uniq_open_product_checkout`: another request opened it between the lock and
        # this insert — which the product lock rules out; answered as the one open.
        existing = products.open_checkout_for(request.user, product)
        if existing is None:
            raise
        existing.already_open = True
        return existing, None
    return payment, None


class CheckoutSessionsView(APIView):
    """`/api/v1/checkout/sessions/` — open a checkout session, or list the open ones.

    **POST** (authenticated). Headers: `X-App-Channel` (required — it opens a checkout),
    `X-App-Version`. Body (JSON): `{"money_type": "PLATFORM_SUB" | "ROUTINE_PRODUCT",
    "item_id": <int>}` — the plan's id, or the routine product's.

    ONE OPEN SESSION PER USER PER ITEM: asking again while it is open answers the SAME
    session with the same reference (200, `already_open: true`) — a double tap costs
    nothing and opens nothing. A new session answers 201 (`already_open: false`).

    * `PLATFORM_SUB` — the caller's subscription's checkout, exactly as the app's Pay
      button (`POST /api/subscription/v1/gateways/`) opens it, and the same checkout:
      one subscription, one open checkout, whichever door opened it. No subscription
      yet: one is created, pending, on this plan. On another plan with nothing running
      and no checkout open: moved to this one first, as `change-plan/` does. Cover
      running on this plan: the session is its RENEWAL (`kind: "renewal"`), as the Renew
      button opens it; on another plan: 409 `plan_change_while_active` (a dearer plan is
      `subscriptions/{id}/upgrade/`).
    * `ROUTINE_PRODUCT` — a published product offered to the caller (a client, not its
      trainer), priced now in the caller's own currency; the commission it will be
      split at is fixed now too. Confirmed, it delivers the caller's own copy of the
      program and pays the trainer, exactly as a wallet purchase does
      (`routine/products.py`); expired or rejected, it delivers nothing.

    201 / 200: the session (below) plus `already_open`.

    **GET** — `{"results": [<session>, ...]}`: the caller's OPEN sessions (`status`
    `open` or `needs_review`), newest first. Answered on every build.

    **A session** (`GET /api/v1/checkout/sessions/{id}/` answers one)::

        {"id": "<uuid>", "money_type": "PLATFORM_SUB" | "ROUTINE_PRODUCT",
         "item": {"type": "plan", "id", "name", "duration_days"}
               | {"type": "routine_product", "id", "name"},
         "kind": "initial" | "renewal" | "upgrade" | "routine_product",
         "status": "open" | "needs_review" | "confirmed" | "rejected" | "expired"
                   | "refunded",
         "payment_status": "<the payment's own status>",
         "amount": "5000.00", "currency": "SYP", "reference": "SHAMCASH_...",
         "expires_at": "<iso>" | null,
         "payment_target": {"adapter": "manual_transfer", "gateway": "shamcash",
                            "account_id": "...", "amount": "5000.00", "currency": "SYP",
                            "note": "<the reference>", "payment_url": null},
         "review": null | {"reason": "...", "held_at": "<iso>"},
         "result": null | {"subscription_id"}
                        | {"purchase_id", "purchase_status", "routine_id"},
         "created_at": "<iso>"}

    `status` is the spec's lifecycle (section 7): `open` until money is matched;
    `needs_review` while a person checks money that is almost right (it never expires);
    `confirmed` once paid (`result` then says what it bought); `rejected` when the
    reviewer refused the transfer (the money goes back by hand); `expired` when nothing
    arrived within the window (money that arrives within 72 hours after still confirms
    it); `refunded` after an operator's refund. `expires_at` is when the reference stops
    being payable (null once the session is not open, and while it is held for review).
    `payment_target` is what to pay and how (`subscription/payment_step.py`): today a
    TRANSFER to `account_id` of exactly `amount` `currency` with `note` in the
    transfer's note; the server finds it (every minute) and confirms it — the payment
    step is an adapter, and a hosted ShamCash page will answer `payment_url` instead.
    Poll this route for the status; "I paid" is `POST
    /api/subscription/v1/payments/{id}/reconcile/`.

    **Differs by build.** Opening: `web` and `apk` only — `play` / `ios` are refused in
    code before anything is read (403 `not_available_on_channel`), and a request that
    declares no channel is 400 `app_channel_required`. Reading (list and detail) works
    on every build, but a build that may not buy the session's kind of money is sent NO
    `amount`, `reference` or `payment_target` (the spec, section 4).

    Codes it can answer (branch on `code`, never on the text):

    | Status | `code` | Meaning |
    |---|---|---|
    | 400 | `app_channel_required` | no known `X-App-Channel` (POST) |
    | 403 | `not_available_on_channel` | this build may not buy this kind of money (`play`, `ios`) (POST) |
    | 400 | `validation_error` | `money_type` missing or not `PLATFORM_SUB` / `ROUTINE_PRODUCT` (`field_errors.money_type[0].code`: `required` / `invalid_choice` — coaching and wallet money are never sold here), `item_id` missing or not a positive integer (`required` / `invalid` / `min_value`), or the body is not a JSON object |
    | 400 | `invalid_plan` | PLATFORM_SUB: no active plan has this id |
    | 400 | `payment_not_required` | PLATFORM_SUB: the plan is free |
    | 409 | `plan_change_while_active` | PLATFORM_SUB: cover is running on another plan (a dearer one is `subscriptions/{id}/upgrade/`) |
    | 409 | `checkout_open` | PLATFORM_SUB: the subscription's open checkout is for another plan, or an upgrade; `payment_id`, `status`, `expires_at` say which |
    | 409 | `price_changed` | PLATFORM_SUB: the subscription moved to another plan while this request ran; `expected_amount` / `expected_currency` |
    | 400 | `not_renewable` | PLATFORM_SUB: cover is running in a state that is not renewed (a trial) |
    | 404 | `not_found` | ROUTINE_PRODUCT: no such product offered to the caller |
    | 403 | `own_product` | ROUTINE_PRODUCT: the caller wrote it |
    | 403 | `clients_only` | ROUTINE_PRODUCT: the caller is not a client |
    | 409 | `already_owned` | ROUTINE_PRODUCT: the caller owns it; `purchase_id`, `routine_id` |
    | 409 | `product_not_for_sale` | ROUTINE_PRODUCT: it stopped being on sale |
    | 409 | `exchange_rate_missing` / `exchange_rate_stale` | the price has to be converted into the caller's currency and the operator's rate is missing / too old |
    | 503 | `gateway_unavailable` | the payment rail is not configured to take payments |
    | 503 | `currency_not_chargeable` | the rail is not configured to charge in the caller's currency |
    | 400 | `gateway_error` | the rail refused or failed to open the checkout |
    | 429 | `throttled` | the rate limiter (POST; `Retry-After`) |
    """
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        # Opening a checkout is a charging action; reading one (the hub polls) is not.
        return [ChargingRateThrottle()] if self.request.method == 'POST' else []

    def get(self, request):
        sessions = (_sessions_of(request.user).filter(status__in=OPEN_PAYMENT_STATUSES)
                    .order_by('-created_at', '-id'))
        may_buy = {kind: _may_buy(request, kind) for kind in MoneyType.values}
        return Response({'results': [session_payload(p, request, may_buy) for p in sessions]})

    def post(self, request):
        body = body_dict(request)
        money_type = body.get('money_type')
        # FIRST: a build that may not buy this kind of money is told so, whatever else
        # it sent. Any other kind is never sold here: after the channel is checked it
        # is a validation error below.
        if money_type in SESSION_MONEY_TYPES:
            require_purchase_allowed(request, money_type)
        elif channel_of(request) is None:
            raise AppChannelRequired()
        serializer = SessionRequestSerializer(data=body)
        serializer.is_valid(raise_exception=True)
        money_type = serializer.validated_data['money_type']
        item_id = serializer.validated_data['item_id']

        if money_type == MoneyType.PLATFORM_SUB:
            payment, refusal = _open_plan_session(request, item_id)
        else:
            payment, refusal = _open_product_session(request, item_id)
        if refusal is not None:
            return refusal
        already_open = bool(getattr(payment, 'already_open', False))
        return Response({**session_payload(payment, request), 'already_open': already_open},
                        status=status.HTTP_200_OK if already_open else status.HTTP_201_CREATED)


class CheckoutSessionDetailView(APIView):
    """`GET /api/v1/checkout/sessions/{id}/` — one of the caller's sessions, in any
    state (the shape is `CheckoutSessionsView`'s). The hub polls it while the customer
    pays. 404 `not_found` when there is no such session or it is not the caller's (one
    answer for both). Answered on every build; a build that may not buy the session's
    kind of money is sent no `amount`, `reference` or `payment_target`.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, session_id):
        payment = _sessions_of(request.user).filter(pk=session_id).first()
        if payment is None:
            return _refusal(_('Payment not found'), 'not_found', status.HTTP_404_NOT_FOUND)
        return Response(session_payload(payment, request))
