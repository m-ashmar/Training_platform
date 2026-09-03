import logging
from django.shortcuts import render
from rest_framework import viewsets, status, permissions, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db import transaction
from django.utils.translation import gettext as _
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from .models import (
    SubscriptionPlan, Subscription, Payment, 
    SubscriptionFeature, SubscriptionUsage
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

from .services.payment_gateways import PaymentGatewayService, PaymentGatewayError
from .services.payment_service import (
    PaymentService, PaymentError, InvalidPaymentTransition, PaymentVerificationError,
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
        ).select_related('plan', 'user').prefetch_related('payments', 'usage')
    
    def get_serializer_class(self):
        if self.action == 'create':
            return SubscriptionCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return SubscriptionUpdateSerializer
        return SubscriptionSerializer
    
    def perform_create(self, serializer):
        """Create a new subscription for the current user with proper error handling"""
        try:
            with transaction.atomic():
                subscription = serializer.save()
                
                # Create initial payment record
                Payment.objects.create(
                    subscription=subscription,
                    amount=subscription.plan.price,
                    currency=subscription.plan.currency,
                    status='pending',
                    description=f"Subscription to {subscription.plan.name}"
                )
                
                logger.info(f"Created subscription {subscription.id} for user {self.request.user.id}")
                
        except ValidationError as e:
            logger.error(f"Validation error creating subscription: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Error creating subscription: {str(e)}")
            raise
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a subscription with proper validation and logging"""
        try:
            subscription = self.get_object()
            serializer = SubscriptionCancelSerializer(
                data=request.data,
                context={'subscription': subscription}
            )
            
            if serializer.is_valid():
                reason = serializer.validated_data.get('reason', '')
                immediate = serializer.validated_data.get('immediate', False)
                
                with transaction.atomic():
                    if immediate:
                        subscription.status = 'cancelled'
                        subscription.cancelled_at = timezone.now()
                        state_changed = True
                    else:
                        # Turning auto-renew off twice is not a second cancellation.
                        # The serializer only rejects an already-'cancelled' row, so a
                        # non-immediate cancel stayed 'active' and could be replayed
                        # without limit, appending an audit row to the caller's own
                        # payment history on every call.
                        state_changed = subscription.auto_renew
                        subscription.auto_renew = False

                    subscription.save()

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
                            metadata={'reason': reason, 'immediate': immediate}
                        )
                
                logger.info(f"Cancelled subscription {subscription.id} for user {request.user.id}")
                
                return Response({
                    'message': _('Subscription cancelled successfully'),
                    'status': subscription.status
                })
            
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
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
        """
        try:
            subscription = self.get_object()

            try:
                payment = PaymentService.start_renewal(subscription)
            except PaymentError as e:
                return Response({'error': _('Request could not be completed.')}, status=status.HTTP_400_BAD_REQUEST)

            gateway_name = request.data.get('gateway', 'shamcash')
            try:
                gateway_service = PaymentGatewayService(gateway_name)
                reference = gateway_service._generate_reference()
                init = gateway_service.initiate_payment(
                    amount=payment.amount,
                    currency=payment.currency,
                    user_data={
                        'email': request.user.email,
                        'phone': request.user.phone_number,
                        'name': f"{request.user.first_name} {request.user.last_name}".strip(),
                    },
                    reference=reference,
                    metadata={'subscription_id': str(subscription.id), 'kind': 'renewal',
                              'description': f"Renewal for {subscription.plan.name}"},
                    callback_url=request.build_absolute_uri(f"/api/subscription/webhook/{gateway_name}/"),
                )
                payment.payment_method = gateway_name
                payment.gateway_transaction_reference = reference
                payment.gateway_response = init.get('response', {})
                payment.save()
            except PaymentGatewayError as e:
                PaymentService.fail_payment(payment.id, str(e))
                return Response({'error': _('Request could not be completed.')}, status=status.HTTP_400_BAD_REQUEST)

            logger.info(f"Renewal payment {payment.id} initiated for subscription {subscription.id}")
            resp = init.get('response', {})
            return Response({
                'message': _('Renewal payment initiated. Complete payment to extend your subscription.'),
                'payment_id': str(payment.id),
                'reference': reference,
                'status': 'pending',
                'payment_url': resp.get('payment_url'),
                'instructions': resp.get('instructions'),
            })

        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Error renewing subscription: {str(e)}")
            return Response(
                {'error': _('Failed to renew subscription')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['get'])
    def current(self, request):
        """Get current user's subscription with proper error handling"""
        try:
            subscription = request.user.subscription
            serializer = UserSubscriptionSummarySerializer(subscription)
            return Response(serializer.data)
        except Subscription.DoesNotExist:
            return Response(
                {'message': _('No active subscription')},
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
            user_id = request.data.get('user_id')
            plan_id = request.data.get('plan_id')
            trial_days = request.data.get('trial_days', 7)
            
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
            features = request.data.get('features', [])
            user = request.user
            
            if not isinstance(features, list):
                return Response(
                    {'error': _('features must be a list')},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                subscription = user.subscription
                if not subscription.is_active:
                    return Response({
                        'has_access': False,
                        'message': _('No active subscription')
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
#: handler is exhaustive: anything outside these three sets is a delivery failure, not
#: a silent 200.
SUCCESS_STATUSES = frozenset({'completed', 'success', 'succeeded', 'paid'})
FAILURE_STATUSES = frozenset({'failed', 'declined', 'error', 'rejected', 'expired'})
REFUND_STATUSES = frozenset({'refunded', 'refund', 'reversed', 'reversal',
                             'chargeback', 'charged_back', 'disputed', 'cancelled',
                             'canceled', 'voided'})


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
            
            # Initialize gateway service
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
        # Idempotency: a replayed event id that already landed is a no-op.
        event_id = payment_data.get('event_id')
        if event_id and Payment.objects.filter(gateway_event_id=event_id, status='completed').exists():
            logger.info(f"Webhook event {event_id} already processed; ignoring replay")
            return

        payment = Payment.objects.filter(
            gateway_transaction_reference=payment_data['reference']
        ).first()
        if not payment:
            logger.warning(f"Payment not found for reference: {payment_data['reference']}")
            return

        gateway_status = str(payment_data.get('status', '')).lower()
        try:
            if gateway_status in SUCCESS_STATUSES:
                PaymentService.complete_payment(payment.id, payment_data)
                logger.info(f"Webhook completed payment {payment.id} via {gateway_name}")
            elif gateway_status in FAILURE_STATUSES:
                PaymentService.fail_payment(payment.id, payment_data.get('error_message', 'Payment failed'))
                logger.info(f"Webhook marked payment {payment.id} failed")
            elif gateway_status in REFUND_STATUSES:
                PaymentService.refund_payment(
                    payment.id, payment_data.get('error_message', gateway_status)
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
        Initiate a payment for one of the caller's own subscriptions.

        Expected payload:
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
        than the figure they agreed to.
        """
        try:
            gateway_name = request.data.get('gateway')
            subscription_id = request.data.get('subscription_id')

            if not gateway_name or not subscription_id:
                return Response(
                    {'error': _('Missing required fields')},
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
                    {'error': _('Subscription not found')},
                    status=status.HTTP_404_NOT_FOUND
                )

            # --- The price is the plan's price. Full stop. ---
            plan = subscription.plan
            amount = plan.price
            currency = plan.currency

            if amount <= 0:
                # A free plan has nothing to charge; sending it to a gateway would
                # only fail the minimum-amount check with a confusing message.
                return Response(
                    {'error': _('This plan does not require a payment')},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Paying again while cover is still running buys nothing: activation only
            # moves end_date when it is missing or already past, so the money would be
            # taken and no time added. Extending an active subscription is what the
            # renew action is for.
            if subscription.is_active:
                return Response(
                    {'error': _('This subscription is already active. Use renew to extend it.')},
                    status=status.HTTP_409_CONFLICT
                )

            # --- Optional client confirmation of the price it displayed ---
            quoted_amount = request.data.get('amount')
            if quoted_amount is not None:
                try:
                    quoted = Decimal(str(quoted_amount))
                except (InvalidOperation, TypeError, ValueError):
                    return Response(
                        {'error': _('Invalid amount')},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                if quoted.quantize(Decimal('0.01')) != Decimal(amount).quantize(Decimal('0.01')):
                    return Response(
                        {'error': _('The price has changed. Please refresh and try again.'),
                         'expected_amount': str(amount), 'expected_currency': currency},
                        status=status.HTTP_409_CONFLICT
                    )
            quoted_currency = request.data.get('currency')
            if quoted_currency is not None and str(quoted_currency).upper() != currency:
                return Response(
                    {'error': _('The price has changed. Please refresh and try again.'),
                     'expected_amount': str(amount), 'expected_currency': currency},
                    status=status.HTTP_409_CONFLICT
                )

            # Initialize gateway service
            gateway_service = PaymentGatewayService(gateway_name)
            reference = gateway_service._generate_reference()

            # Prepare user data
            user_data = {
                'email': request.user.email,
                'phone': request.user.phone_number,
                'name': f"{request.user.first_name} {request.user.last_name}".strip()
            }

            # Initiate payment (caller owns the reference, tied to the Payment row)
            payment_result = gateway_service.initiate_payment(
                amount=amount,
                currency=currency,
                user_data=user_data,
                reference=reference,
                metadata={'subscription_id': str(subscription_id),
                          'description': f"Payment for {subscription.plan.name}"},
                callback_url=request.build_absolute_uri(f"/api/subscription/webhook/{gateway_name}/"),
            )

            # Create the PENDING payment record. It becomes 'completed' only via
            # PaymentService after gateway confirmation.
            payment = Payment.objects.create(
                subscription=subscription,
                amount=amount,
                currency=currency,
                status='pending',
                payment_method=gateway_name,
                gateway_transaction_reference=reference,
                gateway_response=payment_result['response'],
                description=f"Payment for {subscription.plan.name}"
            )

            logger.info(f"Payment initiated: {payment.id} via {gateway_name}")

            resp = payment_result['response']
            return Response({
                'success': True,
                'payment_id': str(payment.id),
                'reference': reference,
                'payment_url': resp.get('payment_url'),
                'instructions': resp.get('instructions'),
                'expires_at': resp.get('expires_at')
            })
            
        except PaymentGatewayError as e:
            logger.error(f"Payment gateway error: {str(e)}")
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
            logger.error(f"Error initiating payment: {str(e)}")
            return Response(
                {'error': _('Failed to initiate payment')},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PaymentStatusView(APIView):
    """
    Read-only payment status. A GET is a safe method and NEVER changes state —
    it only reports the stored status. To reconcile a pending payment against the
    gateway, use POST .../reconcile/ (PaymentReconcileView).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, payment_id):
        try:
            payment = Payment.objects.get(id=payment_id, subscription__user=request.user)
        except Payment.DoesNotExist:
            return Response({'error': _('Payment not found')}, status=status.HTTP_404_NOT_FOUND)

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
        })


class PaymentReconcileView(APIView):
    """
    Reconcile a PENDING payment against the gateway (fallback for missed webhooks).
    Non-safe method: it authoritatively verifies the transaction at the provider
    and, only on a verified match, completes it via PaymentService. It never
    trusts a bare status flag and never activates on unverified data.
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
            return Response({'error': _('Payment not found')}, status=status.HTTP_404_NOT_FOUND)

        if payment.status == 'completed':
            return Response({'success': True, 'status': 'completed',
                             'invoice_number': payment.invoice_number})

        if not payment.gateway_transaction_reference:
            return Response({'error': _('Payment has no gateway reference to reconcile')},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            gateway_service = PaymentGatewayService(payment.payment_method)
            result = gateway_service.fetch_payment_status(
                payment.gateway_transaction_reference,
                expected={'amount': payment.amount, 'currency': payment.currency},
            )
        except PaymentGatewayError as e:
            logger.warning(f"Reconcile lookup failed for {payment.id}: {str(e)}")
            return Response({'error': _('Could not verify payment with gateway')},
                            status=status.HTTP_502_BAD_GATEWAY)

        if str(result.get('status', '')).lower() in ('completed', 'success', 'succeeded', 'paid'):
            try:
                PaymentService.complete_payment(payment.id, result)
                payment.refresh_from_db()
            except (PaymentVerificationError, InvalidPaymentTransition) as e:
                logger.warning(f"Reconcile rejected for {payment.id}: {str(e)}")
                return Response({'error': _('Request could not be completed.')}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'success': True,
            'payment_id': str(payment.id),
            'status': payment.status,
            'invoice_number': payment.invoice_number,
        })
