from django.test import TestCase
from rest_framework.test import APIClient, APITestCase
from rest_framework import status
from django.contrib.auth import get_user_model
from decimal import Decimal
from django.utils import timezone
from .models import SubscriptionPlan, Subscription, Payment
from .serializers import (
    SubscriptionPlanSerializer, SubscriptionSerializer,
    SubscriptionCreateSerializer
)

User = get_user_model()

class SubscriptionModelTest(TestCase):
    """Test cases for Subscription models"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123',
            phone_number='1234567890'
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
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
        self.assertTrue(subscription.is_active)
    
    def test_subscription_expiration(self):
        """Test subscription expiration logic"""
        now = timezone.now()
        start_date = now - timezone.timedelta(days=32)
        end_date = now - timezone.timedelta(days=2)
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active',
            start_date=start_date,
            end_date=end_date
        )
        self.assertFalse(subscription.is_active)

    def test_plan_str_representation(self):
        """Test string representation"""
        plan = SubscriptionPlan.objects.create(
            name='Test Plan 2',
            description='Test Descr 2',
            price=Decimal('9.99'),
            duration_days=30
        )
        self.assertEqual(str(plan), 'Test Plan 2 - 9.99 SYP')

class SubscriptionAPITest(APITestCase):
    """Test cases for Subscription API endpoints"""
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123',
            phone_number='1234567890'
        )
        self.plan = SubscriptionPlan.objects.create(
            name='Test Plan',
            plan_type='basic',
            description='Test description',
            price=Decimal('9.99'),
            duration_days=30,
            has_diet_access=True,
            has_routine_access=True,
        )
        self.client.force_authenticate(user=self.user)

    def test_list_plans(self):
        """Test listing subscription plans"""
        url = '/api/subscription/v1/plans/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
    
    def test_create_subscription(self):
        """Test creating a subscription via API"""
        url = '/api/subscription/v1/subscriptions/'
        data = {
            'plan_id': str(self.plan.id),
            'auto_renew': True
        }
        response = self.client.post(url, data, format='json')
        if response.status_code != 201:
             print(f"DEBUG Create Sub Error: {response.data}")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Subscription.objects.count(), 1)
    
    def test_cancel_subscription(self):
        """Test cancelling a subscription"""
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        
        url = f'/api/subscription/v1/subscriptions/{subscription.id}/cancel/'
        data = {'reason': 'Too expensive'}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['message'], 'Subscription cancelled successfully')
    
    def test_renew_subscription(self):
        """Test renewing a subscription"""
        subscription = Subscription.objects.create(
            user=self.user,
            plan=self.plan,
            status='active'
        )
        
        url = f'/api/subscription/v1/subscriptions/{subscription.id}/renew/'
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
        
        url = '/api/subscription/v1/access/check/'
        data = {
            'features': ['diet', 'routine']
        }
        response = self.client.post(url, data, format='json')
        if response.status_code != 200:
             print(f"DEBUG Check Access Error: {response.data}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['has_access'])

class PaymentAPITest(APITestCase):
    """Test cases for Payment API endpoints"""
    
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123',
            phone_number='1112223333'
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
        
        url = '/api/subscription/v1/payments/'
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
        
        url = f'/api/subscription/v1/payments/{payment.id}/confirm/'
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
            is_superuser=True,
            phone_number='9998887777'
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
        url = '/api/subscription/v1/admin/management/'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('total_subscriptions', response.data)
        self.assertIn('active_subscriptions', response.data)
    
    def test_create_trial_subscription(self):
        """Test creating trial subscription"""
        user = User.objects.create_user(
            username='trialuser',
            email='trial@example.com',
            password='trialpass123',
            phone_number='5556667777'
        )
        
        url = '/api/subscription/v1/admin/management/'
        data = {
            'user_id': user.id,
            'plan_id': str(self.plan.id),
            'trial_days': 7
        }
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'trial')

class SerializerTest(TestCase):
    """Test cases for serializers"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123',
            phone_number='4443332222'
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
            password='testpass123',
            phone_number='0001112222'
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
