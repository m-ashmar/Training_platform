"""
Django management command to test payment gateways.

This command simulates payment initiation and webhook processing for all available gateways.
Useful for testing the payment infrastructure before going live.
"""

import json
import time
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta

from subscription.models import SubscriptionPlan, Subscription, Payment
from subscription.services.payment_gateways import PaymentGatewayManager
from subscription.settings.gateway_config import GATEWAY_REGISTRY, GATEWAY_MODE

User = get_user_model()

class Command(BaseCommand):
    help = 'Test payment gateways and simulate payment flows'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--gateway',
            type=str,
            help='Specific gateway to test (e.g., syriatel_cash)'
        )
        parser.add_argument(
            '--user-id',
            type=int,
            help='User ID to use for testing'
        )
        parser.add_argument(
            '--amount',
            type=float,
            default=1000.0,
            help='Payment amount to test (default: 1000.0)'
        )
        parser.add_argument(
            '--currency',
            type=str,
            default='SYP',
            help='Payment currency (default: SYP)'
        )
        parser.add_argument(
            '--test-webhook',
            action='store_true',
            help='Test webhook processing'
        )
        parser.add_argument(
            '--list-gateways',
            action='store_true',
            help='List all available gateways'
        )
    
    def handle(self, *args, **options):
        """Handle the command execution."""
        self.stdout.write(
            self.style.SUCCESS('🚀 Payment Gateway Testing Tool')
        )
        self.stdout.write(f"Environment: {GATEWAY_MODE}")
        self.stdout.write("=" * 50)
        
        # List available gateways
        if options['list_gateways']:
            self._list_gateways()
            return
        
        # Get available gateways
        available_gateways = self._get_available_gateways()
        if not available_gateways:
            self.stdout.write(
                self.style.WARNING('⚠️  No payment gateways are configured')
            )
            return
        
        # Test specific gateway or all gateways
        if options['gateway']:
            if options['gateway'] not in available_gateways:
                raise CommandError(f"Gateway '{options['gateway']}' not found or not configured")
            gateways_to_test = [options['gateway']]
        else:
            gateways_to_test = list(available_gateways.keys())
        
        # Test each gateway
        for gateway_name in gateways_to_test:
            self.stdout.write(f"\n🔧 Testing Gateway: {gateway_name}")
            self.stdout.write("-" * 30)
            
            try:
                # Test connection
                self._test_connection(gateway_name)
                
                # Test payment initiation
                payment_data = self._test_payment_initiation(
                    gateway_name, 
                    options['amount'], 
                    options['currency'],
                    options['user_id']
                )
                
                # Test webhook processing
                if options['test_webhook'] and payment_data:
                    self._test_webhook_processing(gateway_name, payment_data)
                
                # Test status check
                if payment_data:
                    self._test_status_check(gateway_name, payment_data['reference'])
                
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'❌ Error testing {gateway_name}: {str(e)}')
                )
        
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(
            self.style.SUCCESS('✅ Payment gateway testing completed')
        )
    
    def _get_available_gateways(self):
        """Get available gateways from registry."""
        available = {}
        for gateway_name, gateway_info in GATEWAY_REGISTRY.items():
            # Check if gateway is enabled (has API keys)
            try:
                from subscription.settings.gateway_config import get_gateway_config
                config = get_gateway_config(gateway_name)
                if config.get('api_key') and config.get('api_secret'):
                    available[gateway_name] = gateway_info
            except:
                continue
        return available
    
    def _list_gateways(self):
        """List all available gateways."""
        available_gateways = self._get_available_gateways()
        
        if not available_gateways:
            self.stdout.write(
                self.style.WARNING('No gateways configured')
            )
            return
        
        self.stdout.write("Available Payment Gateways:")
        self.stdout.write("-" * 40)
        
        for gateway_name, gateway_info in available_gateways.items():
            self.stdout.write(f"📱 {gateway_name}")
            self.stdout.write(f"   Name: {gateway_info['name']}")
            self.stdout.write(f"   Currencies: {', '.join(gateway_info['supported_currencies'])}")
            self.stdout.write(f"   Amount Range: {gateway_info['min_amount']} - {gateway_info['max_amount']} SYP")
            self.stdout.write(f"   Status: ✅ Enabled")
            self.stdout.write("")
    
    def _test_connection(self, gateway_name):
        """Test connection to a payment gateway."""
        self.stdout.write("🔌 Testing connection...")
        
        result = PaymentGatewayManager.test_gateway_connection(gateway_name)
        
        if result['success']:
            self.stdout.write(
                self.style.SUCCESS(f"✅ Connection successful: {result['message']}")
            )
        else:
            self.stdout.write(
                self.style.ERROR(f"❌ Connection failed: {result['message']}")
            )
    
    def _test_payment_initiation(self, gateway_name, amount, currency, user_id):
        """Test payment initiation."""
        self.stdout.write("💳 Testing payment initiation...")
        
        # Get or create test user
        user = self._get_test_user(user_id)
        
        # Get or create test subscription
        subscription = self._get_test_subscription(user)
        
        # Prepare payment data
        user_data = {
            'email': user.email,
            'phone': user.phone_number,
            'name': f"{user.first_name} {user.last_name}".strip()
        }
        
        try:
            # Initialize gateway service
            gateway_service = PaymentGatewayManager.get_gateway_service(gateway_name)
            
            # Initiate payment
            result = gateway_service.initiate_payment(
                amount=Decimal(str(amount)),
                currency=currency,
                user_data=user_data,
                metadata={'test': True, 'timestamp': time.time()}
            )
            
            # Create payment record
            payment = Payment.objects.create(
                subscription=subscription,
                amount=Decimal(str(amount)),
                currency=currency,
                status='pending',
                payment_method=gateway_name,
                gateway_transaction_reference=result['reference'],
                gateway_response=result['response'],
                description=f"Test payment via {gateway_name}"
            )
            
            self.stdout.write(
                self.style.SUCCESS(f"✅ Payment initiated successfully")
            )
            self.stdout.write(f"   Payment ID: {payment.id}")
            self.stdout.write(f"   Reference: {result['reference']}")
            self.stdout.write(f"   Amount: {amount} {currency}")
            
            return {
                'payment_id': str(payment.id),
                'reference': result['reference'],
                'response': result['response']
            }
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"❌ Payment initiation failed: {str(e)}")
            )
            return None
    
    def _test_webhook_processing(self, gateway_name, payment_data):
        """Test webhook processing."""
        self.stdout.write("📡 Testing webhook processing...")
        
        try:
            # Simulate webhook payload
            webhook_payload = {
                'transaction_id': f"{gateway_name.upper()}_{int(time.time())}",
                'reference': payment_data['reference'],
                'status': 'completed',
                'amount': 1000.00,
                'currency': 'SYP',
                'timestamp': int(time.time()),
                'signature': 'test_signature_123'
            }
            
            # Convert to bytes
            payload_bytes = json.dumps(webhook_payload).encode('utf-8')
            
            # Simulate headers
            headers = {
                'X-Gateway-Signature': 'test_signature_123',
                'X-Gateway-Timestamp': str(int(time.time())),
                'Content-Type': 'application/json'
            }
            
            # Initialize gateway service
            gateway_service = PaymentGatewayManager.get_gateway_service(gateway_name)
            
            # Verify webhook
            is_valid, payment_info = gateway_service.verify_webhook(payload_bytes, headers)
            
            if is_valid:
                self.stdout.write(
                    self.style.SUCCESS("✅ Webhook verification successful")
                )
                self.stdout.write(f"   Status: {payment_info.get('status')}")
                self.stdout.write(f"   Amount: {payment_info.get('amount')}")
            else:
                self.stdout.write(
                    self.style.WARNING("⚠️  Webhook verification failed")
                )
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"❌ Webhook processing failed: {str(e)}")
            )
    
    def _test_status_check(self, gateway_name, reference):
        """Test payment status check."""
        self.stdout.write("📊 Testing status check...")
        
        try:
            # Initialize gateway service
            gateway_service = PaymentGatewayManager.get_gateway_service(gateway_name)
            
            # Check status
            status_result = gateway_service.get_payment_status(reference)
            
            self.stdout.write(
                self.style.SUCCESS("✅ Status check successful")
            )
            self.stdout.write(f"   Status: {status_result.get('status')}")
            self.stdout.write(f"   Transaction ID: {status_result.get('transaction_id')}")
            
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"❌ Status check failed: {str(e)}")
            )
    
    def _get_test_user(self, user_id):
        """Get or create a test user."""
        if user_id:
            try:
                return User.objects.get(id=user_id)
            except User.DoesNotExist:
                raise CommandError(f"User with ID {user_id} not found")
        
        # Create test user if not specified
        test_user, created = User.objects.get_or_create(
            email='test@paymentgateway.com',
            defaults={
                'username': 'test_payment_user',
                'phone_number': '+963123456789',
                'first_name': 'Test',
                'last_name': 'User'
            }
        )
        
        if created:
            self.stdout.write(f"👤 Created test user: {test_user.email}")
        
        return test_user
    
    def _get_test_subscription(self, user):
        """Get or create a test subscription."""
        # Check if user already has a subscription
        if hasattr(user, 'subscription'):
            return user.subscription
        
        # Get or create test plan
        test_plan, created = SubscriptionPlan.objects.get_or_create(
            name='Test Plan',
            defaults={
                'plan_type': 'basic',
                'description': 'Test plan for payment gateway testing',
                'price': Decimal('1000.00'),
                'duration_days': 30,
                'has_diet_access': True,
                'has_routine_access': True
            }
        )
        
        if created:
            self.stdout.write(f"📋 Created test plan: {test_plan.name}")
        
        # Create test subscription with end_date
        now = timezone.now()
        end_date = now + timedelta(days=test_plan.duration_days)
        subscription = Subscription.objects.create(
            user=user,
            plan=test_plan,
            status='pending',
            end_date=end_date
        )
        
        self.stdout.write(f"📦 Created test subscription: {subscription.id}")
        return subscription 