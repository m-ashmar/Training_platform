import logging
from django.shortcuts import render
from rest_framework import viewsets, status, permissions, mixins, serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.renderers import BaseRenderer, BrowsableAPIRenderer, JSONRenderer

from training_platform.query_params import body_dict
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db import transaction
from django.db.models import Prefetch
from django.utils.translation import gettext as _
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from .models import (
    OPEN_PAYMENT_STATUSES, SubscriptionPlan, Subscription, Payment,
    SubscriptionFeature, SubscriptionUsage, plan_snapshot
)
from .serializers import (
    SubscriptionPlanSerializer, SubscriptionSerializer,
    SubscriptionCreateSerializer, SubscriptionUpdateSerializer,
    SubscriptionCancelSerializer, PaymentSerializer,
    PaymentCreateSerializer, SubscriptionFeatureSerializer,
    SubscriptionUsageSerializer, SubscriptionPlanListSerializer,
    UserSubscriptionSummarySerializer
)

# Import CustomUser at the top
from users.models import CustomUser

# Configure logging
logger = logging.getLogger(__name__)

from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from .services.payment_gateways import (
    CurrencyNotChargeable, PaymentGatewayError, PaymentGatewayService,
)
from .services.payment_service import (
    PaymentService, PaymentError, InvalidPaymentTransition, PaymentVerificationError,
    PlanChanged, SubscriptionAlreadyActive,
)
# NOTE: use PaymentGatewayManager.get_available_gateways() (returns a dict of
# gateway_name -> info). The same-named helper in settings.gateway_config returns
# a plain LIST of names; calling .items() on it raised
# "'list' object has no attribute 'items'" and 500'd this endpoint.
from .services.payment_gateways import PaymentGatewayManager
from rest_framework.permissions import AllowAny
import uuid
from rest_framework.exceptions import NotFound, PermissionDenied, NotAuthenticated
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS
from wallet.throttles import ChargingRateThrottle
from policy.money import MoneyType
from policy.services import flags_for_request, require_purchase_allowed
from wallet import currency_for as wallet_currency_for
from wallet.exchange import ExchangeRateUnavailable, refusal_body

from .pricing import charge_for


def _checkout_opener(request, subscription, gateway_name, gateway_service):
    """The gateway call that opens a checkout, for `PaymentService` to run under its lock.

    `gateway_service` is a callable returning the service, so a caller that may be handed
    back an already-open checkout resolves the gateway only when a new one is opened.
    Returns `(payment_method, reference, gateway_response)`.
    """
    def initiate(payment):
        service = gateway_service()
        reference = service._generate_reference()
        init = service.initiate_payment(
            amount=payment.amount,
            currency=payment.currency,
            user_data={
                'email': request.user.email,
                'phone': request.user.phone_number,
                'name': f"{request.user.first_name} {request.user.last_name}".strip(),
            },
            reference=reference,
            metadata={'subscription_id': str(subscription.id),
                      **({'kind': payment.metadata['kind']}
                         if (payment.metadata or {}).get('kind') else {}),
                      'description': payment.description},
            callback_url=request.build_absolute_uri(f"/api/subscription/webhook/{gateway_name}/"),
        )
        return gateway_name, reference, init.get('response', {})
    return initiate


def _currency_not_chargeable():
    """503 `currency_not_chargeable`: the gateway takes no payments in this buyer's
    currency until the operator configures its amount bounds (SIDE_BUGS S-052). The
    text is the one the exchange-rate refusal already uses, because to the customer it
    is the same fact: paying in their currency is not available right now."""
    from wallet.exchange import REFUSAL_MESSAGES, NoExchangeRate

    return Response({'error': REFUSAL_MESSAGES[NoExchangeRate.code],
                     'code': CurrencyNotChargeable.code},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _plan_moved(subscription_pk, request, price_changed):
    """409 `price_changed` for a Pay tap whose plan moved under it (`PlanChanged`):
    `expected_amount`/`expected_currency` are what the plan the row is on NOW would
    charge this buyer, so the app can show it and ask again. If that figure cannot be
    had (the rate is missing or stale), the rate's own 409 instead."""
    plan = Subscription.objects.select_related('plan').get(pk=subscription_pk).plan
    try:
        priced = charge_for(plan.price, plan.currency, wallet_currency_for(request.user))
    except ExchangeRateUnavailable as exc:
        return Response(refusal_body(exc), status=status.HTTP_409_CONFLICT)
    return price_changed(priced.amount, priced.currency)


def _locked_subscription(pk):
    """The subscription row under `SELECT ... FOR UPDATE`, for a write that must not race.

    `of=('self',)`: without it the lock reaches through `select_related` to the plan row,
    and every subscriber on a plan would queue behind every other one.
    """
    return (Subscription.objects.select_for_update(of=('self',))
            .select_related('plan').get(pk=pk))


class SubscriptionPlanViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for subscription plans (read-only for users)"""
    queryset = SubscriptionPlan.objects.filter(is_active=True)
    serializer_class = SubscriptionPlanSerializer
    permission_classes = [permissions.AllowAny]
    
    def get_queryset(self):
        """Optimized queryset with select_related for better performance"""
        return SubscriptionPlan.objects.filter(is_active=True).select_related()
    
    def get_serializer_class(self):
        if self.action == 'list':
            return SubscriptionPlanListSerializer
        return SubscriptionPlanSerializer
    
    @action(detail=False, methods=['get'])
    def available(self, request):
        """Get all available subscription plans"""
        try:
            plans = self.get_queryset().order_by('price')
            serializer = self.get_serializer(plans, many=True)
            return Response(serializer.data)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error fetching available plans: {str(e)}")
            return Response(
                {'error': _('Failed to fetch subscription plans')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class SubscriptionViewSet(mixins.CreateModelMixin,
                          mixins.RetrieveModelMixin,
                          mixins.UpdateModelMixin,
                          mixins.ListModelMixin,
                          viewsets.GenericViewSet):
    """ViewSet for user subscriptions.

    Deliberately NOT a ModelViewSet: there is no destroy. `Payment.subscription` is
    on_delete=PROTECT so the ledger survives, which means a DELETE could never
    succeed for any subscription that has payments — and perform_create gives every
    subscription one immediately. The route therefore existed only to raise an
    unhandled ProtectedError and answer 500. Ending a subscription is `cancel`.
    """
    serializer_class = SubscriptionSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Optimized queryset with select_related and prefetch_related"""
        return Subscription.objects.filter(
            user=self.request.user
        ).select_related('plan', 'user').prefetch_related(
            # ...__plan too: `PaymentSerializer.item` reads `payment.subscription.plan`
            # for the "what was bought" line, which is two hops past a bare
            # `prefetch_related('payments')` — two extra queries per payment row.
            Prefetch('payments',
                     queryset=Payment.objects.select_related('subscription__plan')),
            'usage')
    
    def get_serializer_class(self):
        if self.action == 'create':
            return SubscriptionCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return SubscriptionUpdateSerializer
        return SubscriptionSerializer
    
    def create(self, request, *args, **kwargs):
        # Choosing a paid plan is the first step of buying one, so a build that may not
        # sell plans is refused here, before a pending subscription and its payment
        # exist. A free plan is not a purchase and stays open to every build. An id that
        # names no plan is treated as paid: the refusal must not depend on a lookup.
        try:
            plan = SubscriptionPlan.objects.filter(
                pk=request.data.get('plan_id'), is_active=True).first()
        except (TypeError, ValueError):
            plan = None
        if plan is None or plan.price > 0:
            require_purchase_allowed(request, MoneyType.PLATFORM_SUB)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        """Create the subscription — and NO payment.

        This also wrote a pending Payment at the plan's price in the PLAN'S currency,
        unconverted and with no price of record, although a payment is charged in the
        BUYER'S currency (the owner, 2026-09-21): a dollar client's history showed a
        pound amount, and an operator completing that row paid out nothing because the
        currencies disagreed (SIDE_BUGS S-134). Nothing could complete it anyway — it
        had no reference. The checkout is opened, priced and snapshotted by
        `PaymentService` when the customer taps Pay (`POST v1/gateways/`).
        """
        try:
            with transaction.atomic():
                subscription = serializer.save()
                logger.info(f"Created subscription {subscription.id} for user {self.request.user.id}")
                
        except ValidationError as e:
            logger.error(f"Validation error creating subscription: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Error creating subscription: {str(e)}")
            raise
    
    def update(self, request, *args, **kwargs):
        """PATCH/PUT: the customer's own settings (`auto_renew`), under the row lock.

        `UpdateModelMixin` validated the row `get_object()` had read with no lock and then
        saved EVERY column of it back. A renewal that cleared between that read and that
        write had its new `end_date` overwritten with the old one: turning auto-renew off
        while a renewal cleared lost the 30 days just paid for, 20 of 20 times on a
        barrier, both calls answering 200 and the payment staying 'completed'. The row is
        now read under `select_for_update` — the lock the payment service takes before it
        extends cover — and only the fields the request changes are written.

        Nothing is writable today (`SubscriptionUpdateSerializer`). Codes it can answer:

        | Status | `code` | Meaning |
        |---|---|---|
        | 400 | `auto_renew_read_only` | the body carries `auto_renew`; renewal is never automatic. `field_errors.auto_renew` repeats it |
        | 400 | `validation_error` | the subscription is not active or on trial |
        | 404 | `not_found` | not the caller's subscription |
        """
        partial = kwargs.pop('partial', False)
        subscription = self.get_object()
        if 'auto_renew' in (request.data if hasattr(request.data, 'keys') else {}):
            # Refused HERE, with the code at the top level of the body. The serializer's
            # own refusal is a field error, and the exception handler turns every field
            # error into `code: "validation_error"` — so the app, which branches on the
            # top-level `code` as the contract tells it to, never saw
            # `auto_renew_read_only` (SIDE_BUGS S-131). The field error is kept beside it.
            from rest_framework.exceptions import ErrorDetail
            from rest_framework.exceptions import ValidationError as DRFValidationError

            refusal = DRFValidationError({'auto_renew': [ErrorDetail(
                SubscriptionUpdateSerializer.AUTO_RENEW_READ_ONLY,
                code='auto_renew_read_only')]})
            refusal.service_code = 'auto_renew_read_only'
            raise refusal
        with transaction.atomic():
            locked = _locked_subscription(subscription.pk)
            serializer = self.get_serializer(locked, data=request.data, partial=partial)
            serializer.is_valid(raise_exception=True)
            for field, value in serializer.validated_data.items():
                setattr(locked, field, value)
            locked.save(update_fields=[*serializer.validated_data, 'updated_at'])
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a subscription with proper validation and logging"""
        try:
            subscription = self.get_object()
            with transaction.atomic():
                # Decided and written on the row as it is under the lock, and only the
                # columns a cancellation owns are written. This read the row with no lock
                # and saved every column back from that copy, so a renewal that cleared in
                # between had its extension overwritten: 18 of 20 paid renewals lost their
                # 30 days, and with `immediate` the stale write also put status back to
                # 'cancelled' over the reactivation.
                subscription = _locked_subscription(subscription.pk)
                serializer = SubscriptionCancelSerializer(
                    data=request.data,
                    context={'subscription': subscription}
                )
                if not serializer.is_valid():
                    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

                reason = serializer.validated_data.get('reason', '')
                immediate = serializer.validated_data.get('immediate', False)

                if immediate:
                    subscription.status = 'cancelled'
                    subscription.cancelled_at = timezone.now()
                    # A cancelled subscription does not renew, as a refund's cancellation
                    # already says. Activation no longer switches this back on, so a row
                    # left at True here would read "renews" for good.
                    subscription.auto_renew = False
                    state_changed = True
                else:
                    # Turning auto-renew off twice is not a second cancellation.
                    # The serializer only rejects an already-'cancelled' row, so a
                    # non-immediate cancel stayed 'active' and could be replayed
                    # without limit, appending an audit row to the caller's own
                    # payment history on every call.
                    state_changed = subscription.auto_renew
                    subscription.auto_renew = False

                subscription.save(update_fields=[
                    'status', 'cancelled_at', 'auto_renew', 'updated_at'])

                if state_changed:
                    # Audit row. Currency follows the plan like every other Payment;
                    # 'USD' here was a third independent guess at a currency the
                    # platform does not charge in.
                    Payment.objects.create(
                        subscription=subscription,
                        amount=0,
                        currency=subscription.plan.currency,
                        status='cancelled',
                        description=f"Subscription cancelled: {reason}",
                        # The plan that was cancelled, so the history row keeps naming
                        # it after the row moves to another plan (`Payment.plan_bought`).
                        metadata={'reason': reason, 'immediate': immediate,
                                  'plan': plan_snapshot(subscription.plan)}
                    )

            logger.info(f"Cancelled subscription {subscription.id} for user {request.user.id}")

            return Response({
                'message': _('Subscription cancelled successfully'),
                'status': subscription.status
            })

        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Error cancelling subscription: {str(e)}")
            return Response(
                {'error': _('Failed to cancel subscription')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def renew(self, request, pk=None):
        """
        Start a renewal. This creates a PENDING payment and initiates the gateway;
        it does NOT extend or activate the subscription. Activation happens only
        after the gateway confirms funds (webhook or verified reconcile), via
        PaymentService.complete_payment.

        `POST`, authenticated. Headers: `X-App-Channel` (required — it opens a
        checkout), `X-App-Version`. Body: `{"gateway": "shamcash"}` (optional; that is
        the default). If the subscription already has an open checkout — whichever
        button opened it — THAT checkout comes back, not a second one.

        200::

            {"message": "<translated>", "payment_id": "<uuid>", "reference": "...",
             "status": "pending", "amount": "5000.00", "currency": "SYP",
             "payment_url": null | "...", "instructions": {...} | null,
             "expires_at": "<iso>" | null}

        `amount`/`currency` are what this checkout charges, in the buyer's currency —
        for a checkout that was already open, what it was opened at. `expires_at` is
        when the server closes the checkout, as `POST v1/gateways/` answers it.

        Codes it can answer (branch on `code`, never on the text):

        | Status | `code` | Meaning |
        |---|---|---|
        | 400 | `app_channel_required` | no known `X-App-Channel` |
        | 403 | `not_available_on_channel` | this build may not sell plans (`play`, `ios`) |
        | 404 | `not_found` | not the caller's subscription |
        | 400 | `not_renewable` | the subscription is not `active` or `expired` |
        | 409 | `exchange_rate_stale` / `exchange_rate_missing` | the plan is priced in the other currency and the operator's rate is too old / was never set |
        | 503 | `currency_not_chargeable` | the gateway is not configured to charge in the buyer's currency |
        | 400 | `gateway_error` | the gateway could not open the checkout (not configured, or refused it) |
        | 500 | `server_error` | unexpected fault |
        """
        from .utils import checkout_expires_at

        try:
            require_purchase_allowed(request, MoneyType.PLATFORM_SUB)
            subscription = self.get_object()
            gateway_name = request.data.get('gateway', 'shamcash')

            # Reserving the checkout, opening the gateway session and storing its reference
            # are ONE step under the subscription lock, inside `start_renewal`. They were
            # three: the service committed a reference-less payment and released the lock,
            # and this view then generated a reference and saved it. Every tap that had
            # been waiting on the lock was released into that gap, saw no reference, made
            # its own and saved over the others — 30 of 30 double taps handed the customer
            # a reference that matched no payment, and a transfer quoting it was answered
            # 400 by the webhook on every redelivery. The same call also hands back a
            # checkout the Pay button already opened, rather than a second payable one.
            try:
                payment = PaymentService.start_renewal(
                    subscription,
                    initiate=_checkout_opener(
                        request, subscription, gateway_name,
                        # Through the manager, so an unconfigured gateway refuses instead
                        # of handing out transfer instructions to an empty account.
                        lambda: PaymentGatewayManager.get_service_for_initiation(gateway_name)),
                )
            except ExchangeRateUnavailable as exc:
                # The plan is priced in the other currency and the operator's rate is
                # missing or stale, so there is no honest number to renew at. Refused
                # with its own `code` rather than charged at yesterday's pound.
                logger.warning("Renewal for subscription %s refused: %s", subscription.id, exc)
                return Response(refusal_body(exc), status=status.HTTP_409_CONFLICT)
            except CurrencyNotChargeable as e:
                logger.warning("Renewal for subscription %s refused: %s", subscription.id, e)
                return _currency_not_chargeable()
            except PaymentGatewayError as e:
                logger.warning("Renewal for subscription %s not initiated: %s", subscription.id, e)
                return Response({'error': _('Request could not be completed.'),
                                 'code': 'gateway_error'},
                                status=status.HTTP_400_BAD_REQUEST)
            except PaymentError:
                return Response({'error': _('Request could not be completed.'),
                                 'code': 'not_renewable'},
                                status=status.HTTP_400_BAD_REQUEST)

            resp = payment.gateway_response or {}
            if payment.already_open:
                logger.info("Checkout %s already open; returning its reference", payment.id)
                message = _('A renewal payment is already awaiting completion.')
            else:
                logger.info(f"Renewal payment {payment.id} initiated for subscription {subscription.id}")
                message = _('Renewal payment initiated. Complete payment to extend your subscription.')
            return Response({
                'message': message,
                'payment_id': str(payment.id),
                'reference': payment.gateway_transaction_reference,
                'status': payment.status,
                # What THIS checkout charges. For one that was already open, the figure
                # it was opened at — which may be an older rate than today's.
                'amount': str(payment.amount),
                'currency': payment.currency,
                'payment_url': resp.get('payment_url'),
                'instructions': resp.get('instructions'),
                # When this checkout closes, as `POST v1/gateways/` already answers — the
                # Pay screen's countdown needed a second call on the renewal path
                # (BACKEND_BUGS B-052).
                'expires_at': checkout_expires_at(payment),
            })

        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Error renewing subscription: {str(e)}")
            return Response(
                {'error': _('Failed to renew subscription'), 'code': 'server_error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'], url_path='change-plan')
    def change_plan(self, request, pk=None):
        """Move a subscription that holds no running cover onto another plan.

        One subscription per user, and `POST v1/subscriptions/` refuses a second, so a
        customer whose row is pending on the wrong plan, or lapsed on Basic, had no way
        to buy Premium (BACKEND_BUGS B-047). This is that way. It moves the row and
        nothing else: no payment is opened, nothing is charged. The next step is the
        Pay button, `POST v1/gateways/`, which prices the NEW plan in the buyer's
        currency.

        `POST`, authenticated. Headers: `X-App-Channel` (required when the new plan is
        paid — choosing one is the first step of buying it), `X-App-Version`. Body:
        `{"plan_id": <int>}`.

        Allowed while nothing is running: `pending`, `expired`, `cancelled`, or
        `active`/`trial` past its end. Refused while cover runs — how an upgrade or a
        downgrade mid-period is priced is the owner's to decide — and while a checkout
        is open: the customer may be transferring against its reference right now, and
        a transfer that cleared after the change would buy the new plan at the old
        price. Each payment records the plan it was priced for, so the receipts and
        history of earlier payments keep naming the plan they bought.

        200::

            {"message": "<translated>", "changed": true | false,
             "subscription": {<the subscription, as GET v1/subscriptions/{id}/>}}

        `changed` is false when `plan_id` is the plan the row is already on.

        Codes it can answer (branch on `code`, never on the text):

        | Status | `code` | Meaning |
        |---|---|---|
        | 400 | `app_channel_required` | no known `X-App-Channel` (the new plan is paid) |
        | 403 | `not_available_on_channel` | this build may not sell plans (`play`, `ios`) |
        | 404 | `not_found` | not the caller's subscription |
        | 400 | `invalid_plan` | `plan_id` missing, or names no active plan |
        | 409 | `plan_change_while_active` | cover is running; it can change once the period ends |
        | 409 | `checkout_open` | a checkout is open for this subscription; `payment_id` and `expires_at` say which, and when it closes |
        | 400 | `validation_error` | the body is not a JSON object |
        | 500 | `server_error` | unexpected fault |

        `plan_id` is an integer as `POST v1/subscriptions/` takes it: `true`, `2.9` or a
        string that is not a number is `invalid_plan`, never a plan it happens to round to.
        """
        from .utils import checkout_expires_at

        subscription = self.get_object()
        body = body_dict(request)
        # Parsed as `POST v1/subscriptions/` parses it (a DRF IntegerField). A bare
        # `int()` took `true` as plan 1 and `2.9` as plan 2 — moving the row to a plan
        # the client never named — and answered 500 for `1e999` (OverflowError).
        try:
            plan_id = serializers.IntegerField(min_value=1).run_validation(
                body.get('plan_id'))
        except serializers.ValidationError:
            plan_id = None
        plan = (SubscriptionPlan.objects.filter(pk=plan_id, is_active=True).first()
                if plan_id is not None else None)
        # The same rule as `create`: a paid plan is a purchase, and an id that names no
        # plan is treated as paid so the refusal never depends on a lookup.
        if plan is None or plan.price > 0:
            require_purchase_allowed(request, MoneyType.PLATFORM_SUB)
        if plan is None:
            return Response({'error': _('Invalid or inactive plan'), 'code': 'invalid_plan'},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            # The lock the Pay button and every completion take first, so a checkout
            # opened or cleared concurrently is either seen here or prices the new plan.
            locked = _locked_subscription(subscription.pk)
            if locked.plan_id == plan.pk:
                return Response({'message': _('The subscription is already on this plan.'),
                                 'changed': False,
                                 'subscription': SubscriptionSerializer(
                                     locked, context=self.get_serializer_context()).data})
            if locked.is_active:
                return Response(
                    {'error': _('The plan can change once the current period ends.'),
                     'code': 'plan_change_while_active'},
                    status=status.HTTP_409_CONFLICT)
            # Open with a reference = offered to the customer. One with no reference
            # was never offered, and is repriced from the plan when it opens.
            open_checkout = (Payment.objects
                             .filter(subscription=locked, status__in=OPEN_PAYMENT_STATUSES)
                             .exclude(gateway_transaction_reference__isnull=True)
                             .exclude(gateway_transaction_reference='')
                             .order_by('created_at', 'id').first())
            if open_checkout is not None:
                return Response(
                    {'error': _('A payment is already open for this subscription. '
                                'Complete it, or change the plan once it closes.'),
                     'code': 'checkout_open',
                     'payment_id': str(open_checkout.id),
                     'expires_at': checkout_expires_at(open_checkout)},
                    status=status.HTTP_409_CONFLICT)
            previous = locked.plan_id
            locked.plan = plan
            # `save()` re-inherits the plan's five access flags on a plan change.
            locked.save(update_fields=['plan', *Subscription.PLAN_FEATURE_FLAGS,
                                       'updated_at'])
        logger.info("Subscription %s moved from plan %s to plan %s by user %s",
                    locked.id, previous, plan.pk, request.user.id)
        locked = self.get_queryset().get(pk=locked.pk)
        # With the request as context, as `GET v1/subscriptions/{id}/` answers it: the
        # plan's price in the READER's currency, and none at all on a store build.
        return Response({'message': _('Plan changed. Complete the payment to start it.'),
                         'changed': True,
                         'subscription': SubscriptionSerializer(
                             locked, context=self.get_serializer_context()).data})

    @action(detail=False, methods=['get'])
    def current(self, request):
        """The caller's own subscription, or a 404 the app can branch on.

        `GET`, authenticated, no body. 200 is `UserSubscriptionSummarySerializer` —
        `access` (`active | frozen | inactive`), `renewal_due`, `plan_duration_days`,
        `plan_currency`, `plan_features`, and the renewal's price in the READER'S
        currency: `plan_price` (and `plan_currency`, which is the reader's currency, not
        necessarily the plan's), `price_converted`, `exchange_rate`, `price_of_record`,
        `price_of_record_currency` — see the serializer for each. All five price keys
        are ABSENT (not null) on a build whose `can_show_prices` is off (`play`, `ios`);
        `plan_currency` stays, since a currency code is not a price.

        Codes it can answer:

        | Status | `code` | Meaning |
        |---|---|---|
        | 404 | `no_subscription` | this account has never had one — show the plans |
        | 500 | `server_error` | unexpected fault |

        The 404 used to carry `message` alone, so the app could not tell "never
        subscribed" from any other 404 (SIDE_BUGS S-024). `message` is kept beside the
        new `code` because it shipped; branch on `code`.
        """
        try:
            subscription = request.user.subscription
            serializer = UserSubscriptionSummarySerializer(subscription, context={'request': request})
            return Response(serializer.data)
        except Subscription.DoesNotExist:
            return Response(
                {'message': _('No active subscription'), 'code': 'no_subscription'},
                status=status.HTTP_404_NOT_FOUND
            )
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error fetching current subscription: {str(e)}")
            return Response(
                {'error': _('Failed to fetch subscription')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['get'])
    def usage(self, request, pk=None):
        """Get subscription usage statistics"""
        try:
            subscription = self.get_object()
            usage = subscription.usage.all().select_related('feature')
            serializer = SubscriptionUsageSerializer(usage, many=True)
            return Response(serializer.data)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Error fetching usage statistics: {str(e)}")
            return Response(
                {'error': _('Failed to fetch usage statistics')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only view of the caller's payments. Payments are created only through the
    gateway-initiate flow, and completed only by PaymentService (webhook/reconcile).
    There is deliberately no create/update/confirm surface here — clients can never
    mutate payment state.
    """
    serializer_class = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Payment.objects.filter(
            subscription__user=self.request.user
        ).select_related('subscription', 'subscription__plan')

class SubscriptionFeatureViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for subscription features"""
    queryset = SubscriptionFeature.objects.filter(is_active=True)
    serializer_class = SubscriptionFeatureSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Optimized queryset"""
        return SubscriptionFeature.objects.filter(is_active=True)

class SubscriptionUsageViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for subscription usage tracking"""
    serializer_class = SubscriptionUsageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """Optimized queryset with select_related"""
        return SubscriptionUsage.objects.filter(
            subscription__user=self.request.user
        ).select_related('subscription', 'feature')

    @action(detail=False, methods=['get'], url_path='today')
    def today(self, request):
        """`GET /api/subscription/v1/usage/today/` — "استهلاكك اليوم": what the
        caller has used of each daily allowance, and out of how much.

        The Subscription screen draws one bar per row. The list endpoint above cannot
        serve it: it returns only allowances that have already been spent at least
        once (no row exists until the first use), and it carries no allowance that is
        not stored as a `SubscriptionUsage` row at all — the AI coach's daily messages
        are counted in the rate-limit cache, not in that table.

        Nothing here is a price or an offer, so it is answered identically on every
        `X-App-Channel`. Purely a read: no usage row is created by opening the screen.

        200::

            {"quotas": [
               {"feature": "daily_meals",  "used": 2,  "limit": 5,  "resets_at": ...},
               {"feature": "ai_messages",  "used": 12, "limit": 50, "resets_at": ...}]}

        `limit: 0` means unlimited. `daily_meals` is the diet plan builds the plan
        allows in the client's own calendar day; `ai_messages` appears only for a plan
        that includes the AI coach and is the SAME counter the WebSocket enforces
        (`ai_assistant.consumers`), so the number on the screen is the number the user
        will hit. A caller with no live subscription gets `{"quotas": []}` — an empty
        screen, not an error: there is nothing to have used.
        """
        from datetime import datetime, time, timedelta

        from django.conf import settings as dj_settings

        from .quota import QuotaUnavailable, snapshot

        quotas = []
        try:
            row = snapshot(request.user, 'daily_meals')
        except QuotaUnavailable:
            return Response({'quotas': []})
        quotas.append({'feature': 'daily_meals', 'used': row['used'],
                       'limit': row['limit'], 'resets_at': row['period_end']})

        subscription = request.user.subscription
        if subscription.has_ai_advice:
            from training_platform.cache import ratelimit_cache

            limit = getattr(dj_settings, 'AI_ASSISTANT_CONFIG', {}).get(
                'MAX_MESSAGES_PER_DAY', 50)
            # The consumer's own key, to the character: a screen that counted
            # differently from the thing that refuses would be worse than no screen.
            today = timezone.localdate()
            used = 0
            try:
                used = int(ratelimit_cache().get(
                    f"ai_chat_limit:{request.user.id}:{today.isoformat()}") or 0)
            except Exception:
                logger.warning("could not read the AI message counter for user %s",
                               request.user.id, exc_info=True)
            resets_at = timezone.make_aware(
                datetime.combine(today + timedelta(days=1), time.min),
                timezone.get_current_timezone())
            quotas.append({'feature': 'ai_messages', 'used': used, 'limit': limit,
                           'resets_at': resets_at})
        return Response({'quotas': quotas})

class SubscriptionManagementView(APIView):
    """Admin view for subscription management with comprehensive error handling"""
    permission_classes = [permissions.IsAdminUser]
    
    def get(self, request):
        """Get subscription statistics with proper error handling"""
        try:
            total_subscriptions = Subscription.objects.count()
            active_subscriptions = Subscription.objects.filter(status='active').count()
            trial_subscriptions = Subscription.objects.filter(status='trial').count()
            expired_subscriptions = Subscription.objects.filter(status='expired').count()
            
            return Response({
                'total_subscriptions': total_subscriptions,
                'active_subscriptions': active_subscriptions,
                'trial_subscriptions': trial_subscriptions,
                'expired_subscriptions': expired_subscriptions
            })
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error fetching subscription statistics: {str(e)}")
            return Response(
                {'error': _('Failed to fetch statistics')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def post(self, request):
        """Create a trial subscription for a user with proper validation"""
        try:
            body = body_dict(request)
            user_id = body.get('user_id')
            plan_id = body.get('plan_id')
            trial_days = body.get('trial_days', 7)
            
            if not user_id or not plan_id:
                return Response(
                    {'error': _('user_id and plan_id are required')},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                user = CustomUser.objects.get(id=user_id)
                plan = SubscriptionPlan.objects.get(id=plan_id)
            except (CustomUser.DoesNotExist, SubscriptionPlan.DoesNotExist):
                return Response(
                    {'error': _('Invalid user or plan ID')},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Check if user already has subscription
            if hasattr(user, 'subscription'):
                return Response(
                    {'error': 'User already has a subscription'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Create trial subscription
            trial_end = timezone.now() + timedelta(days=trial_days)
            subscription = Subscription.objects.create(
                user=user,
                plan=plan,
                status='trial',
                source='trial',
                trial_end_date=trial_end
            )
            
            logger.info(f"Created trial subscription {subscription.id} for user {user_id}")
            
            serializer = SubscriptionSerializer(subscription)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        except ValidationError as e:
            logger.error(f"Validation error creating trial subscription: {str(e)}")
            return Response(
                {'error': _('Request could not be completed.')},
                status=status.HTTP_400_BAD_REQUEST
            )
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error creating trial subscription: {str(e)}")
            return Response(
                {'error': _('Failed to create trial subscription')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class SubscriptionAccessView(APIView):
    """View to check subscription access for specific features with comprehensive validation"""
    permission_classes = [permissions.IsAuthenticated]
    
    def post(self, request):
        """Check if user has access to specific features with proper error handling"""
        try:
            features = body_dict(request).get('features', [])
            user = request.user
            
            if not isinstance(features, list):
                return Response(
                    {'error': _('features must be a list')},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                subscription = user.subscription
                if not subscription.is_active:
                    # `subscription_status` and `frozen` added: a lapsed subscriber
                    # (frozen: plans readable, nothing usable) and one who never paid
                    # got the same answer.
                    return Response({
                        'has_access': False,
                        'message': _('No active subscription'),
                        'subscription_status': subscription.status,
                        'frozen': subscription.is_frozen,
                    })
                
                access_results = {}
                for feature in features:
                    if feature == 'diet':
                        access_results[feature] = subscription.has_diet_access
                    elif feature == 'routine':
                        access_results[feature] = subscription.has_routine_access
                    elif feature == 'challenges':
                        access_results[feature] = subscription.has_challenges_access
                    elif feature == 'ai_advice':
                        access_results[feature] = subscription.has_ai_advice
                    elif feature == 'priority_support':
                        access_results[feature] = subscription.has_priority_support
                    else:
                        access_results[feature] = False
                
                return Response({
                    'has_access': any(access_results.values()),
                    'access_details': access_results,
                    'subscription_status': subscription.status,
                    'days_remaining': subscription.days_remaining
                })
                
            except Subscription.DoesNotExist:
                return Response({
                    'has_access': False,
                    'message': _('No subscription found')
                })
                
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error checking subscription access: {str(e)}")
            return Response(
                {'error': _('Failed to check access')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

#: What a gateway can tell us about a payment, and which of them we act on. The
#: handler is exhaustive: anything outside these four sets is a delivery failure, not
#: a silent 200.
SUCCESS_STATUSES = frozenset({'completed', 'success', 'succeeded', 'paid'})
FAILURE_STATUSES = frozenset({'failed', 'declined', 'error', 'rejected', 'expired'})
#: Money that was taken and given back. Only legal from `completed`.
REFUND_STATUSES = frozenset({'refunded', 'refund', 'reversed', 'reversal',
                             'chargeback', 'charged_back', 'disputed'})
#: Money that was never taken. `cancelled`/`canceled`/`voided` were in REFUND_STATUSES,
#: and a refund is only legal from `completed` — so the most common event of all, a
#: customer opening the payment page and walking away, was answered 400 and redelivered
#: for ever against a row that could never leave `pending`.
CANCELLATION_STATUSES = frozenset({'cancelled', 'canceled', 'voided', 'abandoned'})


class PaymentWebhookView(APIView):
    """
    Webhook endpoint for payment gateway notifications.

    Unauthenticated by design (called by the external gateway) but protected by
    signature + timestamp verification inside the gateway's verify_webhook, and
    idempotent completion via PaymentService. Route: /webhook/<gateway_name>/
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)
    
    def post(self, request, gateway_name):
        """
        Handle webhook notification from payment gateway.
        
        Args:
            request: HTTP request
            gateway_name: Name of the gateway (e.g., 'syriatel_cash')
        
        Returns:
            HTTP response
        """
        try:
            # Log webhook receipt
            logger.info(f"Webhook received from {gateway_name}")
            
            # Get raw payload and headers. `request.headers` is Django's HttpHeaders,
            # which is case-insensitive; `dict(...)` of it is not, and that alone was
            # enough to reject every webhook the gateway ever sent. Gateways now fold
            # case themselves (PaymentGateway.header), so this is belt and braces.
            payload = request.body
            headers = request.headers
            
            # NOT through the enabled check. A webhook is about money that has ALREADY
            # moved, so a gateway being switched off for NEW payments must not make us
            # drop a delivery about an old one — that is the silent-loss failure the
            # unmatched-reference branch below exists to prevent.
            gateway_service = PaymentGatewayService(gateway_name)
            
            # Verify webhook and extract payment data
            is_valid, payment_data = gateway_service.verify_webhook(payload, headers)
            
            if not is_valid:
                logger.warning(f"Invalid webhook from {gateway_name}")
                return HttpResponse(status=400)
            
            # Process payment status update
            with transaction.atomic():
                self._process_payment_update(payment_data, gateway_name)
            
            logger.info(f"Webhook processed successfully for {gateway_name}")
            return HttpResponse(status=200)
            
        except PaymentGatewayError as e:
            logger.error(f"Payment gateway error in webhook: {str(e)}")
            return HttpResponse(status=400)
        except Exception as e:
            logger.error(f"Unexpected error in webhook: {str(e)}")
            return HttpResponse(status=500)
    
    def _process_payment_update(self, payment_data: dict, gateway_name: str):
        """
        Route a verified webhook to the single authority (PaymentService). This
        method never sets payment/subscription state directly.
        """
        # Idempotency: a replayed event id that already landed grants nothing again —
        # but it is how the gateway retries a delivery we answered 500, and the usual
        # reason for that 500 is a payout that failed AFTER the payment committed. This
        # returned early on the payment's status alone, answered 200, and the gateway
        # stopped retrying with the trainer unpaid. A payout still owed is sent again.
        event_id = payment_data.get('event_id')
        replayed = (Payment.objects.filter(gateway_event_id=event_id, status='completed').first()
                    if event_id else None)
        if replayed:
            logger.info(f"Webhook event {event_id} already processed; ignoring replay")
            PaymentService.redeliver_payout(replayed)
            return

        payment = Payment.objects.filter(
            gateway_transaction_reference=payment_data['reference']
        ).first()
        if not payment:
            # A FAILED delivery, not a silent 200. This logged and returned, and the
            # caller answered 200 — which tells the gateway "accepted, stop retrying" —
            # so every reference mismatch was an unrecoverable, unalerted loss of a
            # customer's money. The identical reasoning is already applied to an unknown
            # STATUS below; it was never applied to an unknown reference.
            logger.error("Payment not found for reference: %s (%s)",
                         payment_data['reference'], gateway_name)
            raise PaymentGatewayError(
                f"No payment matches reference {payment_data['reference']!r}")

        gateway_status = str(payment_data.get('status', '')).lower()
        try:
            if gateway_status in SUCCESS_STATUSES:
                PaymentService.complete_payment(payment.id, payment_data)
                logger.info(f"Webhook completed payment {payment.id} via {gateway_name}")
            elif gateway_status in FAILURE_STATUSES:
                PaymentService.fail_payment(payment.id, payment_data.get('error_message', 'Payment failed'))
                logger.info(f"Webhook marked payment {payment.id} failed")
            elif gateway_status in CANCELLATION_STATUSES:
                # An abandoned checkout is not a refund. These words were in
                # REFUND_STATUSES, and `refund_payment` is only legal from `completed`,
                # so the single most common gateway event — the customer opens the
                # payment page and walks away — produced a 400 and an endless
                # redelivery loop against a row that stayed `pending` for ever.
                PaymentService.cancel_payment(
                    payment.id, payment_data.get('error_message', gateway_status))
                logger.info(f"Webhook cancelled payment {payment.id} via {gateway_name}")
            elif gateway_status in REFUND_STATUSES:
                PaymentService.refund_payment(
                    payment.id, payment_data.get('error_message', gateway_status),
                    # The refund path did NO amount or currency verification at all,
                    # while completion verifies both — so a signed webhook claiming
                    # 1.00 USD against a 5,000.00 SYP payment revoked the whole
                    # subscription and unwound the whole ledger.
                    gateway_data=payment_data,
                )
                logger.info(f"Webhook refunded payment {payment.id} via {gateway_name}")
            else:
                # There used to be no else. Every status that was neither a success nor
                # a failure word fell through, the view answered 200, and the gateway
                # took that for acceptance and never retried — so a refund, a reversal
                # or a chargeback left the subscription active with the money returned.
                # An unknown status is now a failed delivery, which is what makes the
                # gateway send it again and puts it in front of a person.
                raise PaymentGatewayError(
                    f"Unhandled gateway status {gateway_status!r} for payment {payment.id}"
                )
        except (PaymentVerificationError, InvalidPaymentTransition) as e:
            # Do not activate on mismatched/illegal data; surface as a bad webhook.
            logger.warning(f"Webhook rejected for payment {payment.id}: {str(e)}")
            raise PaymentGatewayError(str(e))


class PaymentGatewayView(APIView):
    """
    API endpoint for payment gateway operations.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        """
        Get available payment gateways.
        
        Returns:
            List of available gateways with their capabilities
        """
        try:
            # How to pay is a payment instruction. A build that may not sell plans gets
            # an empty list — the same for every user of that build, reviewer or not.
            if not flags_for_request(request)['can_purchase_platform_sub']:
                return Response({'success': True, 'gateways': []})

            available_gateways = PaymentGatewayManager.get_available_gateways()
            
            gateway_list = []
            for gateway_name, gateway_info in available_gateways.items():
                gateway_list.append({
                    'name': gateway_name,
                    'display_name': gateway_info['name'],
                    'supported_currencies': gateway_info['supported_currencies'],
                    'settlement_currency': gateway_info['settlement_currency'],
                    # Per-currency bounds. The flat pair below is kept for existing
                    # clients and describes the settlement currency only.
                    'amount_limits': gateway_info['amount_limits'],
                    'min_amount': gateway_info.get('min_amount'),
                    'max_amount': gateway_info.get('max_amount'),
                    'enabled': gateway_info['enabled']
                })
            
            return Response({
                'success': True,
                'gateways': gateway_list
            })
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error getting available gateways: {str(e)}")
            return Response(
                {'error': _('Failed to get available gateways')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def post(self, request):
        """
        Initiate a payment for one of the caller's own subscriptions — the Pay button.

        `POST`, authenticated. Headers: `X-App-Channel` (required — it opens a
        checkout), `X-App-Version`. Expected payload:
        {
            "gateway": "shamcash",
            "subscription_id": "uuid",
            "amount": "5000.00",   # OPTIONAL, confirmation only (see below)
            "currency": "SYP"      # OPTIONAL, confirmation only
        }

        THE CLIENT DOES NOT SET THE PRICE. Amount and currency are read from
        `subscription.plan` and nowhere else. They used to be taken straight from
        the request body, and because every later check compares the gateway's
        report against `payment.amount`, the whole verification chain then agreed
        with a number the payer had chosen: a 5000.00 plan activated in full for
        the 100 SYP gateway floor.

        `amount`/`currency` are still accepted, but only as an assertion of the
        price the client displayed. A mismatch is a conflict, not an instruction —
        silently charging the server price would bill the user something other
        than the figure they agreed to. The assertion is checked against WHAT THIS
        CALL WILL CHARGE: the plan's price in the buyer's currency at today's rate
        for a new checkout, or — when the subscription already has an open checkout —
        the amount that checkout was opened at, which after a rate change is not
        today's figure (SIDE_BUGS S-136). The 409 says which, and the response always
        carries the checkout's own `amount`/`currency`.

        200::

            {"success": true, "payment_id": "<uuid>", "reference": "...",
             "amount": "5000.00", "currency": "SYP",
             "payment_url": null | "...", "instructions": {...} | null,
             "expires_at": "<ISO 8601>"}

        `amount`/`currency` are what the returned reference charges, in the buyer's
        currency — draw THESE on the Pay screen, not a figure from the plan list.

        Codes it can answer (branch on `code`, never on the text):

        | Status | `code` | Meaning |
        |---|---|---|
        | 400 | `app_channel_required` | no known `X-App-Channel` |
        | 403 | `not_available_on_channel` | this build may not sell plans (`play`, `ios`) |
        | 400 | `missing_fields` | `gateway` or `subscription_id` missing |
        | 404 | `not_found` | no such subscription, or not the caller's |
        | 400 | `payment_not_required` | the plan is free |
        | 409 | `exchange_rate_stale` / `exchange_rate_missing` | the plan is priced in the other currency and the operator's rate is too old / was never set |
        | 400 | `invalid_amount` | the quoted `amount` is not a number |
        | 409 | `price_changed` | the quoted `amount`/`currency` is not what this call would charge, or the subscription moved to another plan (`subscriptions/{id}/change-plan/`) while this call ran; `expected_amount`/`expected_currency` say what it would charge now |
        | 409 | `subscription_active` | cover is running; use `subscriptions/{id}/renew/` |
        | 503 | `gateway_unavailable` | that gateway is not configured to take payments |
        | 503 | `currency_not_chargeable` | the gateway is not configured to charge in the buyer's currency |
        | 400 | `gateway_error` | the gateway refused or failed to open the checkout |
        | 500 | `server_error` | unexpected fault |
        """
        try:
            require_purchase_allowed(request, MoneyType.PLATFORM_SUB)
            body = body_dict(request)
            gateway_name = body.get('gateway')
            subscription_id = body.get('subscription_id')

            if not gateway_name or not subscription_id:
                return Response(
                    {'error': _('Missing required fields'), 'code': 'missing_fields'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Get subscription
            try:
                subscription = Subscription.objects.select_related('plan').get(
                    id=subscription_id,
                    user=request.user
                )
            except (Subscription.DoesNotExist, ValidationError, ValueError):
                return Response(
                    {'error': _('Subscription not found'), 'code': 'not_found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # --- The price is the plan's price. Full stop. ---
            plan = subscription.plan

            if plan.price <= 0:
                # A free plan has nothing to charge; sending it to a gateway would
                # only fail the minimum-amount check with a confusing message. Decided
                # on the price of record, BEFORE any conversion: nothing times a rate
                # is still nothing, and a free plan must not be refused because the
                # operator's rate is stale.
                return Response(
                    {'error': _('This plan does not require a payment'),
                     'code': 'payment_not_required'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # WHAT THIS BUYER PAYS, in the one currency this buyer trades in (the
            # owner, 2026-09-21). The plan keeps the price of record the operator set;
            # a buyer who holds the other currency is charged it converted at the
            # operator's live rate, and `PaymentService` snapshots that rate onto the
            # payment. A buyer already in the plan's currency converts nothing and
            # consults no rate, so a stale rate cannot stop a same-currency sale.
            #
            # Computed HERE as well as inside the service so the quoted-price check
            # below compares against the number the app actually displayed, and so a
            # missing or stale rate is a clean 409 with a `code` rather than a 500 out
            # of the service.
            try:
                priced = charge_for(plan.price, plan.currency,
                                    wallet_currency_for(request.user))
            except ExchangeRateUnavailable as exc:
                # `exchange_rate_missing` / `exchange_rate_stale`. The app branches on
                # the code; the text never changes.
                logger.warning("Checkout refused for subscription %s: %s",
                               subscription.id, exc)
                return Response(refusal_body(exc), status=status.HTTP_409_CONFLICT)

            # What this call will actually charge. An open checkout that already has a
            # reference is handed back as it is (`PaymentService.start_payment`), at the
            # amount it was opened at — so after a rate change its figure is not
            # `priced`, and checking the quote against `priced` let the app's number
            # pass while the reference it got back charged another (SIDE_BUGS S-136).
            # Read without the lock: the lock is taken below, and the answer is checked
            # again against the payment that comes back.
            already_open = (Payment.objects
                            .filter(subscription=subscription,
                                    status__in=OPEN_PAYMENT_STATUSES)
                            .exclude(gateway_transaction_reference__isnull=True)
                            .exclude(gateway_transaction_reference='')
                            .order_by('created_at', 'id').first())
            if already_open is not None:
                amount, currency = already_open.amount, already_open.currency
            else:
                amount, currency = priced.amount, priced.currency

            # --- Optional client confirmation of the price it displayed ---
            quoted_amount = request.data.get('amount')
            quoted = None
            if quoted_amount is not None:
                try:
                    quoted = Decimal(str(quoted_amount)).quantize(Decimal('0.01'))
                except (InvalidOperation, TypeError, ValueError):
                    return Response(
                        {'error': _('Invalid amount'), 'code': 'invalid_amount'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            quoted_currency = request.data.get('currency')
            quoted_currency = (str(quoted_currency).upper()
                               if quoted_currency is not None else None)

            def _mismatch(charged_amount, charged_currency):
                return ((quoted is not None
                         and quoted != Decimal(charged_amount).quantize(Decimal('0.01')))
                        or (quoted_currency is not None
                            and quoted_currency != charged_currency))

            def _price_changed(charged_amount, charged_currency):
                return Response(
                    {'error': _('The price has changed. Please refresh and try again.'),
                     'code': 'price_changed',
                     'expected_amount': str(charged_amount),
                     'expected_currency': charged_currency},
                    status=status.HTTP_409_CONFLICT
                )

            if _mismatch(amount, currency):
                return _price_changed(amount, currency)

            # Through the MANAGER, which is the only place the enabled check lives.
            # Constructing the service directly skipped it, so a deploy missing
            # SHAMCASH_ACCOUNT_ID answered 200 and told every customer to transfer
            # 5,000 SYP to account "" — while GET /v1/gateways/ correctly hid the
            # gateway, so the failure was invisible until customers reported the loss.
            try:
                gateway_service = PaymentGatewayManager.get_service_for_initiation(gateway_name)
            except PaymentGatewayError as e:
                logger.error("Payment initiation refused: %s", e)
                return Response(
                    {'error': _('That payment method is not available right now.'),
                     'code': 'gateway_unavailable'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE
                )

            # The subscription's ONE open checkout: returned if it has one — whichever
            # button opened it — and otherwise opened, with the gateway session and its
            # reference stored under the subscription lock. This created a new Payment on
            # every call, so two taps on a subscription that was pending or had lapsed
            # each handed out their own payable reference, and Renew and Pay after a lapse
            # did the same between them. Paying both charged 10,000 SYP for one 5,000 SYP
            # period.
            try:
                payment = PaymentService.start_payment(
                    subscription,
                    _checkout_opener(request, subscription, gateway_name,
                                     lambda: gateway_service),
                    # The plan priced and quote-checked above, read without the lock. A
                    # `change-plan` from the customer's other device that commits in
                    # between would otherwise open a checkout at a price nobody quoted.
                    expected_plan_id=plan.id,
                )
            except PlanChanged:
                return _plan_moved(subscription.pk, request, _price_changed)
            except SubscriptionAlreadyActive:
                # Extending an active subscription is what the renew action is for. Decided
                # under the lock, so a payment clearing at this moment is seen.
                return Response(
                    {'error': _('This subscription is already active. Use renew to extend it.'),
                     'code': 'subscription_active'},
                    status=status.HTTP_409_CONFLICT
                )
            except CurrencyNotChargeable as e:
                logger.warning("Checkout refused for subscription %s: %s",
                               subscription.id, e)
                return _currency_not_chargeable()

            if payment.already_open:
                logger.info("Checkout %s already open; returning its reference", payment.id)
                # The checkout under the lock is not the one read above (it closed, or
                # another opened, in between): the quote is held to it all the same.
                if _mismatch(payment.amount, payment.currency):
                    return _price_changed(payment.amount, payment.currency)
            else:
                logger.info(f"Payment initiated: {payment.id} via {gateway_name}")

            resp = payment.gateway_response or {}
            from .utils import checkout_expires_at

            return Response({
                'success': True,
                'payment_id': str(payment.id),
                'reference': payment.gateway_transaction_reference,
                # What the returned reference charges, in the buyer's currency. It was
                # not answered, so after a rate change the app drew one figure and the
                # already-open reference it was handed charged another (S-136).
                'amount': str(payment.amount),
                'currency': payment.currency,
                'payment_url': resp.get('payment_url'),
                'instructions': resp.get('instructions'),
                # When the reference stops being payable — what the Pay screen counts
                # down to. The gateway's own key first, for a hosted checkout that
                # sets one; otherwise OUR window, which is the one the poller
                # enforces. It used to be the gateway's key alone, and the ShamCash
                # adapter has never set it, so this was null on every checkout.
                'expires_at': resp.get('expires_at') or checkout_expires_at(payment),
            })

        except PaymentGatewayError as e:
            logger.error(f"Payment gateway error: {str(e)}")
            return Response(
                {'error': _('Request could not be completed.'), 'code': 'gateway_error'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Error initiating payment: {str(e)}")
            return Response(
                {'error': _('Failed to initiate payment'), 'code': 'server_error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PaymentStatusView(APIView):
    """`GET /api/subscription/v1/payments/{payment_id}/status/` — the stored status.

    A GET is a safe method and NEVER changes state — it only reports the stored status.
    To reconcile a pending payment against the gateway, use `POST .../reconcile/`.

    200 `{success, payment_id, status, amount, currency, payment_method, invoice_number,
    created_at, updated_at, expires_at, reference}` (`expires_at` null once the checkout
    is no longer open). 404 `not_found` — no such payment, or not the caller's
    (one answer for both).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, payment_id):
        try:
            payment = Payment.objects.get(id=payment_id, subscription__user=request.user)
        except Payment.DoesNotExist:
            return Response({'error': _('Payment not found'), 'code': 'not_found'},
                            status=status.HTTP_404_NOT_FOUND)

        from .utils import checkout_expires_at

        return Response({
            'success': True,
            'payment_id': str(payment.id),
            'status': payment.status,
            'amount': str(payment.amount),
            'currency': payment.currency,
            'payment_method': payment.payment_method,
            'invoice_number': payment.invoice_number,
            'created_at': payment.created_at,
            'updated_at': payment.updated_at,
            # The Confirming screen polls here while it is open, and the Pay screen
            # counts down to this. Null once the checkout is no longer open.
            'expires_at': checkout_expires_at(payment),
            'reference': payment.gateway_transaction_reference,
        })


class PaymentReconcileView(APIView):
    """`POST /api/subscription/v1/payments/{payment_id}/reconcile/` — ask the gateway.

    Reconciles a PENDING payment against the gateway (fallback for missed webhooks).
    Non-safe method: it authoritatively verifies the transaction at the provider and,
    only on a verified match, completes it via PaymentService. It never trusts a bare
    status flag and never activates on unverified data.

    200 `{success, payment_id, status, invoice_number}` — `status` is whatever the
    payment is after asking (still `pending` when the gateway has seen nothing yet).
    Refusals, each with its `code` (BACKEND_BUGS B-048):
      404 `not_found`               no such payment, or not the caller's
      400 `no_gateway_reference`    the checkout never reached the gateway
      502 `gateway_unreachable`     the provider could not be asked; nothing changed,
                                    the money is safe, ask again later
      400 `reconcile_rejected`      the provider answered but the match failed
                                    verification (amount, currency, or an illegal
                                    transition); nothing changed
    """
    permission_classes = [permissions.IsAuthenticated]
    # Every call makes an outbound gateway lookup, so an authenticated caller could
    # amplify one request of theirs into unbounded traffic at the provider. The
    # `charging` scope is already declared in DEFAULT_THROTTLE_RATES and was used only
    # by the wallet.
    throttle_classes = [ChargingRateThrottle]

    def post(self, request, payment_id):
        try:
            payment = Payment.objects.get(id=payment_id, subscription__user=request.user)
        except Payment.DoesNotExist:
            return Response({'error': _('Payment not found'), 'code': 'not_found'},
                            status=status.HTTP_404_NOT_FOUND)

        if payment.status == 'completed':
            # Already completed is not "nothing left to do" while its payout is owed.
            PaymentService.redeliver_payout(payment)
            return Response({'success': True, 'payment_id': str(payment.id),
                             'status': 'completed',
                             'invoice_number': payment.invoice_number})

        if not payment.gateway_transaction_reference:
            return Response({'error': _('Payment has no gateway reference to reconcile'),
                             'code': 'no_gateway_reference'},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            gateway_service = PaymentGatewayService(payment.payment_method)
            result = gateway_service.fetch_payment_status(
                payment.gateway_transaction_reference,
                expected={'amount': payment.amount, 'currency': payment.currency,
                          'since': payment.created_at},
            )
        except PaymentGatewayError as e:
            logger.warning(f"Reconcile lookup failed for {payment.id}: {str(e)}")
            return Response({'error': _('Could not verify payment with gateway'),
                             'code': 'gateway_unreachable'},
                            status=status.HTTP_502_BAD_GATEWAY)

        if str(result.get('status', '')).lower() in ('completed', 'success', 'succeeded', 'paid'):
            try:
                PaymentService.complete_payment(payment.id, result)
                payment.refresh_from_db()
            except (PaymentVerificationError, InvalidPaymentTransition) as e:
                logger.warning(f"Reconcile rejected for {payment.id}: {str(e)}")
                return Response({'error': _('Request could not be completed.'),
                                 'code': 'reconcile_rejected'},
                                status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'success': True,
            'payment_id': str(payment.id),
            'status': payment.status,
            'invoice_number': payment.invoice_number,
        })


class ReceiptPDFRenderer(BaseRenderer):
    """Lets `?format=pdf` mean what it says.

    DRF negotiates the response format BEFORE the handler runs, and a `format` it has
    no renderer for is answered `404` from inside `initial()` — so a receipt view that
    simply returned a PDF never got the chance to: every `?format=pdf` was a 404 that
    looked like a missing payment. The bytes are already a PDF by the time they reach
    here, so this renderer only declares that the format exists and hands them through.
    """
    media_type = 'application/pdf'
    format = 'pdf'
    charset = None
    render_style = 'binary'

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if isinstance(data, (bytes, bytearray)):
            return data
        # Not a PDF: a refusal (404 `not_found` / `receipt_not_available`) that DRF
        # routed here because the format was negotiated before the handler ran. Handed
        # back as a dict, HttpResponse joined its KEYS into the body — the app received
        # `errorcodestatus` labelled application/pdf, and lost the very `code` the
        # docstring tells it to branch on (BACKEND_BUGS B-049). Render it as the JSON
        # every other refusal is, and say so in the content type.
        from rest_framework.renderers import JSONRenderer

        response = (renderer_context or {}).get('response')
        if response is not None:
            response['Content-Type'] = 'application/json'
        return JSONRenderer().render(data, 'application/json', renderer_context)


class PaymentReceiptView(APIView):
    """`GET /api/subscription/v1/payments/{payment_id}/receipt/` — the receipt for one
    completed payment.

    Headers: `Authorization`, `Accept-Language` (the document is written in the active
    language; Arabic is the primary one). **No `X-App-Channel` is required and none is
    read.**

    Which side of the store line this is on: a receipt is a RECORD OF A PAYMENT ALREADY
    MADE, not a sale. It carries no price for anything not yet bought, no purchase
    button, no payment instruction and no link to the web hub — only what the customer
    already paid and when. `require_purchase_allowed` is therefore deliberately NOT
    called here, and the answer is byte-identical on `apk`, `play`, `ios` and `web`:
    spec §3 says of a store build "the user's plan status, expiry date and history may
    be shown", and history without its receipts is not history. Behaviour depends on the
    build in no way at all, which is the strongest form of rule zero.

    `?format=pdf` returns the same receipt as an A5 PDF
    (`Content-Type: application/pdf`, `Content-Disposition: attachment`), in Arabic when
    Arabic is active. Anything else, including no query at all, returns JSON.

    200 (JSON)::

        {"receipt": {
            "payment_id": "<uuid>",
            "invoice_number": "INV-2026-0A1B2C3D4E5F",  stable; never reused
            "issued_at": "2026-09-19T14:12:00Z",        when the money was confirmed
            "status": "paid",
            "item": {"plan_id", "plan_name", "plan_type", "duration_days",
                     "money_type"},                     draw as "Premium · 30 يوماً"
            "amount": "2500.00", "currency": "SYP",     what was CHARGED, in the payer's currency
            "exchange": null | {                        null unless the plan was priced in
                "price_of_record": "2.00",              the other currency; then what it
                "price_of_record_currency": "USD",      was priced at, and the rate the
                "rate": "13000.000000",                 charge was struck at — read off the
                "rate_text": "1 USD = 13000.000000 SYP"},  payment, never today's rate
            "method": {"code": "shamcash", "name": "<translated>"},
            "reference": "<the gateway's reference, or "">",
            "covers": {"from": ..., "until": ...},      the access it bought
            "payer": {"id", "name", "email"},
            "platform": {"name", "legal_name", "contact_email", "website"},
            "plan_duration_days": 30,
            "pdf_url": "<this path>?format=pdf"}}

    A receipt for a converted charge must say so: when `exchange` is not null, draw
    the price of record and `rate_text` beside the amount (SIDE_BUGS S-137).

    Errors (branch on `code`):

    | HTTP | `code` | when |
    |---|---|---|
    | 404 | `not_found` | no such payment, or it belongs to someone else — the same answer either way, so the endpoint cannot be used to discover another customer's payment ids |
    | 404 | `receipt_not_available` | the payment exists and is the caller's, but is not `completed`: a checkout nobody paid has no receipt |

    Who may read it: the payer, and a superuser. A trainer is not a party to their
    client's plan payment and gets `not_found`.
    """
    permission_classes = [permissions.IsAuthenticated]
    #: `pdf` has to be a format DRF knows, or content negotiation refuses the request
    #: before this view runs — see `ReceiptPDFRenderer`.
    renderer_classes = [JSONRenderer, BrowsableAPIRenderer, ReceiptPDFRenderer]

    def get(self, request, payment_id):
        from .receipts import receipt_data, render_pdf

        qs = Payment.objects.select_related('subscription', 'subscription__plan',
                                            'subscription__user')
        if not getattr(request.user, 'is_superuser', False):
            qs = qs.filter(subscription__user=request.user)
        try:
            payment = qs.get(id=payment_id)
        except (Payment.DoesNotExist, ValidationError, ValueError):
            # Identical to "not yours": an id that exists must not answer differently
            # from one that does not.
            return Response({'error': _('Payment not found'), 'code': 'not_found'},
                            status=status.HTTP_404_NOT_FOUND)

        if payment.status != 'completed':
            return Response(
                {'error': _('This payment is not complete, so it has no receipt yet.'),
                 'code': 'receipt_not_available', 'status': payment.status},
                status=status.HTTP_404_NOT_FOUND)

        # One generator for invoice numbers, and it lives in PaymentService.
        invoice_number = PaymentService.ensure_invoice_number(payment)
        data = receipt_data(payment, invoice_number=invoice_number)

        if request.query_params.get('format') == 'pdf':
            pdf = render_pdf(data)
            response = HttpResponse(pdf, content_type='application/pdf')
            response['Content-Disposition'] = (
                f'attachment; filename="{invoice_number or payment.id}.pdf"')
            return response

        data['pdf_url'] = request.build_absolute_uri(f'{request.path}?format=pdf')
        return Response({'receipt': data})
