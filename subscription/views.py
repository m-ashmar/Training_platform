import logging
from django.shortcuts import render
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db import transaction
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from datetime import timedelta
from decimal import Decimal

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
from .settings.gateway_config import get_available_gateways

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
        except Exception as e:
            logger.error(f"Error fetching available plans: {str(e)}")
            return Response(
                {'error': 'Failed to fetch subscription plans'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class SubscriptionViewSet(viewsets.ModelViewSet):
    """ViewSet for user subscriptions"""
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
                    currency='USD',
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
                    else:
                        subscription.auto_renew = False
                    
                    subscription.save()
                    
                    # Log the cancellation
                    Payment.objects.create(
                        subscription=subscription,
                        amount=0,
                        currency='USD',
                        status='cancelled',
                        description=f"Subscription cancelled: {reason}",
                        metadata={'reason': reason, 'immediate': immediate}
                    )
                
                logger.info(f"Cancelled subscription {subscription.id} for user {request.user.id}")
                
                return Response({
                    'message': 'Subscription cancelled successfully',
                    'status': subscription.status
                })
            
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
        except Exception as e:
            logger.error(f"Error cancelling subscription: {str(e)}")
            return Response(
                {'error': 'Failed to cancel subscription'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def renew(self, request, pk=None):
        """Renew a subscription with proper validation"""
        try:
            subscription = self.get_object()
            
            if subscription.status not in ['active', 'expired']:
                return Response(
                    {'error': 'Subscription cannot be renewed'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            with transaction.atomic():
                # Extend the subscription
                subscription.end_date = subscription.end_date + timedelta(days=subscription.plan.duration_days)
                subscription.status = 'active'
                subscription.auto_renew = True
                subscription.save()
                
                # Create payment record
                Payment.objects.create(
                    subscription=subscription,
                    amount=subscription.plan.price,
                    currency='USD',
                    status='completed',
                    description=f"Renewal for {subscription.plan.name}"
                )
            
            logger.info(f"Renewed subscription {subscription.id} for user {request.user.id}")
            
            return Response({
                'message': 'Subscription renewed successfully',
                'new_end_date': subscription.end_date
            })
            
        except Exception as e:
            logger.error(f"Error renewing subscription: {str(e)}")
            return Response(
                {'error': 'Failed to renew subscription'},
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
                {'message': 'No active subscription'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error fetching current subscription: {str(e)}")
            return Response(
                {'error': 'Failed to fetch subscription'},
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
        except Exception as e:
            logger.error(f"Error fetching usage statistics: {str(e)}")
            return Response(
                {'error': 'Failed to fetch usage statistics'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class PaymentViewSet(viewsets.ModelViewSet):
    """ViewSet for payment management"""
    serializer_class = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        """Optimized queryset with select_related"""
        return Payment.objects.filter(
            subscription__user=self.request.user
        ).select_related('subscription', 'subscription__plan')
    
    def get_serializer_class(self):
        if self.action == 'create':
            return PaymentCreateSerializer
        return PaymentSerializer
    
    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        """Confirm a payment (webhook from payment provider) with proper validation"""
        try:
            payment = self.get_object()
            
            # Validate payment can be confirmed
            if payment.status not in ['pending', 'failed']:
                return Response(
                    {'error': 'Payment cannot be confirmed'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            with transaction.atomic():
                payment.status = 'completed'
                payment.save()
                
                # Update subscription status
                subscription = payment.subscription
                subscription.status = 'active'
                subscription.save()
            
            logger.info(f"Confirmed payment {payment.id} for subscription {subscription.id}")
            
            return Response({'message': 'Payment confirmed'})
            
        except Exception as e:
            logger.error(f"Error confirming payment: {str(e)}")
            return Response(
                {'error': 'Failed to confirm payment'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

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
        except Exception as e:
            logger.error(f"Error fetching subscription statistics: {str(e)}")
            return Response(
                {'error': 'Failed to fetch statistics'},
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
                    {'error': 'user_id and plan_id are required'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                user = CustomUser.objects.get(id=user_id)
                plan = SubscriptionPlan.objects.get(id=plan_id)
            except (CustomUser.DoesNotExist, SubscriptionPlan.DoesNotExist):
                return Response(
                    {'error': 'Invalid user or plan ID'},
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
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Error creating trial subscription: {str(e)}")
            return Response(
                {'error': 'Failed to create trial subscription'},
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
                    {'error': 'features must be a list'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                subscription = user.subscription
                if not subscription.is_active:
                    return Response({
                        'has_access': False,
                        'message': 'No active subscription'
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
                    'message': 'No subscription found'
                })
                
        except Exception as e:
            logger.error(f"Error checking subscription access: {str(e)}")
            return Response(
                {'error': 'Failed to check access'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class PaymentWebhookView(APIView):
    """
    Webhook endpoint for payment gateway notifications.
    
    This view handles webhook notifications from all payment gateways.
    Supports dynamic routing: /api/webhook/<gateway_name>/
    """
    
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
            
            # Get raw payload and headers
            payload = request.body
            headers = dict(request.headers)
            
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
        Process payment status update from webhook.
        
        Args:
            payment_data: Parsed payment data from webhook
            gateway_name: Name of the gateway
        """
        try:
            # Find payment by reference
            payment = Payment.objects.filter(
                gateway_transaction_reference=payment_data['reference']
            ).first()
            
            if not payment:
                logger.warning(f"Payment not found for reference: {payment_data['reference']}")
                return
            
            # Update payment status
            gateway_status = payment_data['status']
            if gateway_status == 'completed':
                payment.status = 'completed'
                payment.gateway_response = payment_data.get('gateway_data', {})
                payment.save()
                
                # Activate subscription
                subscription = payment.subscription
                subscription.status = 'active'
                subscription.save()
                
                logger.info(f"Payment {payment.id} completed, subscription {subscription.id} activated")
                
            elif gateway_status == 'failed':
                payment.status = 'failed'
                payment.gateway_error = payment_data.get('error_message', 'Payment failed')
                payment.gateway_response = payment_data.get('gateway_data', {})
                payment.save()
                
                logger.info(f"Payment {payment.id} failed")
            
        except Exception as e:
            logger.error(f"Error processing payment update: {str(e)}")
            raise


class PaymentGatewayView(APIView):
    """
    API endpoint for payment gateway operations.
    """
    
    def get(self, request):
        """
        Get available payment gateways.
        
        Returns:
            List of available gateways with their capabilities
        """
        try:
            available_gateways = get_available_gateways()
            
            gateway_list = []
            for gateway_name, gateway_info in available_gateways.items():
                gateway_list.append({
                    'name': gateway_name,
                    'display_name': gateway_info['name'],
                    'supported_currencies': gateway_info['supported_currencies'],
                    'min_amount': gateway_info['min_amount'],
                    'max_amount': gateway_info['max_amount'],
                    'enabled': gateway_info['enabled']
                })
            
            return Response({
                'success': True,
                'gateways': gateway_list
            })
            
        except Exception as e:
            logger.error(f"Error getting available gateways: {str(e)}")
            return Response(
                {'error': 'Failed to get available gateways'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def post(self, request):
        """
        Initiate a payment through a specific gateway.
        
        Expected payload:
        {
            "gateway": "syriatel_cash",
            "amount": "1000.00",
            "currency": "SYP",
            "subscription_id": "uuid"
        }
        """
        try:
            gateway_name = request.data.get('gateway')
            amount = Decimal(request.data.get('amount'))
            currency = request.data.get('currency', 'SYP')
            subscription_id = request.data.get('subscription_id')
            
            # Validate inputs
            if not all([gateway_name, amount, subscription_id]):
                return Response(
                    {'error': 'Missing required fields'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Get subscription
            try:
                subscription = Subscription.objects.get(
                    id=subscription_id,
                    user=request.user
                )
            except Subscription.DoesNotExist:
                return Response(
                    {'error': 'Subscription not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Initialize gateway service
            gateway_service = PaymentGatewayService(gateway_name)
            
            # Prepare user data
            user_data = {
                'email': request.user.email,
                'phone': request.user.phone_number,
                'name': f"{request.user.first_name} {request.user.last_name}".strip()
            }
            
            # Initiate payment
            payment_result = gateway_service.initiate_payment(
                amount=amount,
                currency=currency,
                user_data=user_data,
                metadata={'subscription_id': str(subscription_id)}
            )
            
            # Create payment record
            payment = Payment.objects.create(
                subscription=subscription,
                amount=amount,
                currency=currency,
                status='pending',
                payment_method=gateway_name,
                gateway_transaction_reference=payment_result['reference'],
                gateway_response=payment_result['response'],
                description=f"Payment for {subscription.plan.name}"
            )
            
            logger.info(f"Payment initiated: {payment.id} via {gateway_name}")
            
            return Response({
                'success': True,
                'payment_id': str(payment.id),
                'reference': payment_result['reference'],
                'payment_url': payment_result['response'].get('payment_url'),
                'expires_at': payment_result['response'].get('expires_at')
            })
            
        except PaymentGatewayError as e:
            logger.error(f"Payment gateway error: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            logger.error(f"Error initiating payment: {str(e)}")
            return Response(
                {'error': 'Failed to initiate payment'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class PaymentStatusView(APIView):
    """
    API endpoint for checking payment status.
    """
    
    def get(self, request, payment_id):
        """
        Get payment status.
        
        Args:
            payment_id: Payment UUID
        """
        try:
            # Get payment
            try:
                payment = Payment.objects.get(
                    id=payment_id,
                    subscription__user=request.user
                )
            except Payment.DoesNotExist:
                return Response(
                    {'error': 'Payment not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # If payment is still pending, check with gateway
            if payment.status == 'pending' and payment.gateway_transaction_reference:
                try:
                    gateway_service = PaymentGatewayService(payment.payment_method)
                    status_result = gateway_service.get_payment_status(
                        payment.gateway_transaction_reference
                    )
                    
                    # Update payment if status changed
                    if status_result['status'] != payment.status:
                        payment.status = status_result['status']
                        payment.gateway_response = status_result.get('gateway_response', {})
                        payment.save()
                        
                        # Activate subscription if payment completed
                        if status_result['status'] == 'completed':
                            subscription = payment.subscription
                            subscription.status = 'active'
                            subscription.save()
                
                except PaymentGatewayError as e:
                    logger.warning(f"Could not check payment status: {str(e)}")
            
            # Return payment status
            return Response({
                'success': True,
                'payment_id': str(payment.id),
                'status': payment.status,
                'amount': str(payment.amount),
                'currency': payment.currency,
                'payment_method': payment.payment_method,
                'created_at': payment.created_at,
                'updated_at': payment.updated_at
            })
            
        except Exception as e:
            logger.error(f"Error checking payment status: {str(e)}")
            return Response(
                {'error': 'Failed to check payment status'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
