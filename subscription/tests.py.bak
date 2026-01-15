import uuid
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from datetime import timedelta

from .models import (
    SubscriptionPlan, Subscription, Payment, 
    SubscriptionFeature, SubscriptionUsage
)
from .serializers import (
    SubscriptionPlanSerializer, SubscriptionSerializer,
    SubscriptionCreateSerializer, PaymentSerializer
)

User = get_user_model()

class SubscriptionPlanModelTest(TestCase):
    """Test cases for SubscriptionPlan model"""
    
    def setUp(self):
        self.plan_data = {
            'name': 'Test Plan',
            'plan_type': 'basic',
            'description': 'Test description',
            'price': Decimal('9.99'),
            'duration_days': 30,
            'has_diet_access': True,
            'has_routine_access': False,
        }
    
    def test_create_subscription_plan(self):
        """Test creating a subscription plan"""
        plan = SubscriptionPlan.objects.create(**self.plan_data)
        self.assertEqual(plan.name, 'Test Plan')
        self.assertEqual(plan.price, Decimal('9.99'))
        self.assertTrue(plan.has_diet_access)
        self.assertFalse(plan.has_routine_access)
    
    def test_plan_validation(self):
        """Test plan validation"""
        # Test negative price
        invalid_data = self.plan_data.copy()
        invalid_data['price'] = Decimal('-1.00')
        with self.assertRaises(ValidationError):
            plan = SubscriptionPlan(**invalid_data)
            plan.full_clean()
        
        # Test zero duration
        invalid_data = self.plan_data.copy()
        invalid_data['duration_days'] = 0
        with self.assertRaises(ValidationError):
            plan = SubscriptionPlan(**invalid_data)
            plan.full_clean()
    
    def test_plan_str_representation(self):
        """Test string representation"""
        plan = SubscriptionPlan.objects.create(**self.plan_data)
        self.assertEqual(str(plan), 'Test Plan - $9.99')

class SubscriptionModelTest(TestCase):
    """Test cases for Subscription model"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
            has_diet_access=True,
        )
    
    def test_create_subscription(self):
        """Test creating a subscription"""
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        self.assertEqual(subscription.user, self.user)
        self.assertEqual(subscription.plan, self.plan)
        self.assertEqual(subscription.status, 'active')
        self.assertTrue(subscription.has_diet_access)
    
    def test_subscription_is_active_property(self):
        """Test is_active property"""
        # Active subscription
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active',
            end_date=timezone.now() + timedelta(days=30)
        )
        self.assertTrue(subscription.is_active)
        
        # Expired subscription
        subscription.end_date = timezone.now() - timedelta(days=1)
        subscription.save()
        self.assertFalse(subscription.is_active)
    
    def test_subscription_is_trial_property(self):
        """Test is_trial property"""
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='trial',
            trial_end_date=timezone.now() + timedelta(days=7)
        )
        self.assertTrue(subscription.is_trial)
        
        # Expired trial
        subscription.trial_end_date = timezone.now() - timedelta(days=1)
        subscription.save()
        self.assertFalse(subscription.is_trial)
    
    def test_days_remaining_property(self):
        """Test days_remaining property"""
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active',
            end_date=timezone.now() + timedelta(days=10)
        )
        self.assertGreaterEqual(subscription.days_remaining, 9)
        self.assertLessEqual(subscription.days_remaining, 10)
    
    def test_subscription_validation(self):
        """Test subscription validation"""
        # Test end_date before start_date
        subscription = Subscription(
            user=self.user,
            plan=self.plan,
            end_date=timezone.now() - timedelta(days=1)
        )
        with self.assertRaises(ValidationError):
            subscription.full_clean()

class PaymentModelTest(TestCase):
    """Test cases for Payment model"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
        )
        self.subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
    
    def test_create_payment(self):
        """Test creating a payment"""
        payment = Payment.objects.create(
            subscription=self.subscription,
            amount=Decimal('9.99'),
            currency='USD',
            status='completed',
            payment_method='stripe'
        )
        self.assertEqual(payment.amount, Decimal('9.99'))
        self.assertEqual(payment.status, 'completed')
        self.assertEqual(payment.payment_method, 'stripe')
    
    def test_payment_validation(self):
        """Test payment validation"""
        # Test negative amount
        payment = Payment(
            subscription=self.subscription,
            amount=Decimal('-1.00'),
            currency='USD'
        )
        with self.assertRaises(ValidationError):
            payment.full_clean()

class SubscriptionUsageModelTest(TestCase):
    """Test cases for SubscriptionUsage model"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
        )
        self.subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        self.feature = SubscriptionFeature.objects.create(
            name='test_feature',
            description='Test feature'
        )
    
    def test_create_usage(self):
        """Test creating usage record"""
        usage = SubscriptionUsage.objects.create(
            subscription=self.subscription,
            feature=self.feature,
            usage_count=5,
            limit=10,
            period_end=timezone.now() + timedelta(days=30)
        )
        self.assertEqual(usage.usage_count, 5)
        self.assertEqual(usage.limit, 10)
        self.assertEqual(usage.usage_percentage, 50.0)
    
    def test_usage_percentage_unlimited(self):
        """Test usage percentage for unlimited usage"""
        usage = SubscriptionUsage.objects.create(
            subscription=self.subscription,
            feature=self.feature,
            usage_count=100,
            limit=0,  # Unlimited
            period_end=timezone.now() + timedelta(days=30)
        )
        self.assertEqual(usage.usage_percentage, 0)

class SubscriptionAPITest(APITestCase):
    """Test cases for Subscription API endpoints"""
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
            has_diet_access=True,
        )
        self.client.force_authenticate(user=self.user)
    
    def test_list_subscription_plans(self):
        """Test listing subscription plans"""
        url = '/api/v1/plans/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['name'], 'Test Plan')
    
    def test_get_subscription_plan_detail(self):
        """Test getting subscription plan detail"""
        url = f'/api/v1/plans/{self.plan.id}/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], 'Test Plan')
    
    def test_create_subscription(self):
        """Test creating a subscription"""
        url = '/api/v1/subscriptions/'
        data = {
            'plan_id': str(self.plan.id),
            'auto_renew': True
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['plan']['name'], 'Test Plan')
    
    def test_get_current_subscription(self):
        """Test getting current subscription"""
        # Create a subscription first
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        
        url = '/api/v1/subscriptions/current/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['plan_name'], 'Test Plan')
    
    def test_get_current_subscription_none(self):
        """Test getting current subscription when none exists"""
        url = '/api/v1/subscriptions/current/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
    
    def test_cancel_subscription(self):
        """Test cancelling a subscription"""
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        
        url = f'/api/v1/subscriptions/{subscription.id}/cancel/'
        data = {
            'reason': 'Testing cancellation',
            'immediate': False
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['message'], 'Subscription cancelled successfully')
    
    def test_renew_subscription(self):
        """Test renewing a subscription"""
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        
        url = f'/api/v1/subscriptions/{subscription.id}/renew/'
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['message'], 'Subscription renewed successfully')
    
    def test_check_subscription_access(self):
        """Test checking subscription access"""
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        
        url = '/api/v1/access/check/'
        data = {
            'features': ['diet', 'routine']
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['has_access'])

class PaymentAPITest(APITestCase):
    """Test cases for Payment API endpoints"""
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
        )
        self.subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        self.client.force_authenticate(user=self.user)
    
    def test_list_payments(self):
        """Test listing payments"""
        payment = Payment.objects.create(
            subscription=self.subscription,
            amount=Decimal('9.99'),
            currency='USD',
            status='completed'
        )
        
        url = '/api/v1/payments/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
    
    def test_confirm_payment(self):
        """Test confirming a payment"""
        payment = Payment.objects.create(
            subscription=self.subscription,
            amount=Decimal('9.99'),
            currency='USD',
            status='pending'
        )
        
        url = f'/api/v1/payments/{payment.id}/confirm/'
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Check payment was confirmed
        payment.refresh_from_db()
        self.assertEqual(payment.status, 'completed')

class AdminAPITest(APITestCase):
    """Test cases for Admin API endpoints"""
    
    def setUp(self):
        self.client = APIClient()
        self.admin_user = User.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='adminpass123',
            is_staff=True,
            is_superuser=True
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
        )
        self.client.force_authenticate(user=self.admin_user)
    
    def test_get_subscription_statistics(self):
        """Test getting subscription statistics"""
        url = '/api/v1/admin/management/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('total_subscriptions', response.data)
        self.assertIn('active_subscriptions', response.data)
    
    def test_create_trial_subscription(self):
        """Test creating trial subscription"""
        user = User.objects.create_user(
            username='trialuser',
            email='trial@example.com',
            password='trialpass123'
        )
        
        url = '/api/v1/admin/management/'
        data = {
            'user_id': user.id,
            'plan_id': str(self.plan.id),
            'trial_days': 7
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'trial')

class SerializerTest(TestCase):
    """Test cases for serializers"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
            has_diet_access=True,
        )
    
    def test_subscription_plan_serializer(self):
        """Test SubscriptionPlanSerializer"""
        serializer = SubscriptionPlanSerializer(self.plan)
        data = serializer.data
        self.assertEqual(data['name'], 'Test Plan')
        self.assertEqual(data['price'], '9.99')
        self.assertTrue(data['has_diet_access'])
    
    def test_subscription_serializer(self):
        """Test SubscriptionSerializer"""
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        serializer = SubscriptionSerializer(subscription)
        data = serializer.data
        self.assertEqual(data['status'], 'active')
        self.assertTrue(data['has_diet_access'])
    
    def test_subscription_create_serializer(self):
        """Test SubscriptionCreateSerializer"""
        data = {
            'plan_id': str(self.plan.id),
            'auto_renew': True
        }
        serializer = SubscriptionCreateSerializer(
            data=data,
            context={'request': type('Request', (), {'user': self.user})()}
        )
        self.assertTrue(serializer.is_valid())
        subscription = serializer.save()
        self.assertEqual(subscription.plan, self.plan)
        self.assertEqual(subscription.user, self.user)

class PermissionTest(APITestCase):
    """Test cases for custom permissions"""
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
            has_diet_access=True,
        )
    
    def test_has_diet_access_permission(self):
        """Test HasDietAccess permission"""
        from .permissions import HasDietAccess
        
        # User without subscription
        permission = HasDietAccess()
        request = type('Request', (), {'user': self.user})()
        self.assertFalse(permission.has_permission(request, None))
        
        # User with subscription but no diet access
        plan_no_diet = SubscriptionPlan.objects.create(
            name='No Diet Plan',
            plan_type='basic',
            description='No diet access',
            price=Decimal('5.99'),
            duration_days=30,
            has_diet_access=False,
        )
        subscription = Subscription.objects.create(
            user=self.user,
            plan=plan_no_diet,
            status='active'
        )
        self.assertFalse(permission.has_permission(request, None))
        
        # User with subscription and diet access
        subscription.plan = self.plan
        subscription.save()
        self.assertTrue(permission.has_permission(request, None))
