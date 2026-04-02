from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from users.models import OTPVerification
from django.utils import timezone
import json

User = get_user_model()

class AuthResponseTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = 'password123'
        
        # Create a client user with incomplete profile
        self.client_user = User.objects.create_user(
            email='client@test.com',
            username='clientuser',
            password=self.password,
            user_type='client',
            phone_number='+1111111111',
            is_active=True # Active for token test
        )
        
    def test_onboarding_logic(self):
        """Test the is_onboarding_completed property"""
        user = self.client_user
        
        # Initially incomplete
        self.assertFalse(user.is_onboarding_completed)
        
        # Add basic info
        user.first_name = "John"
        user.last_name = "Doe"
        user.save()
        self.assertFalse(user.is_onboarding_completed) # Still false because it's a client missing details
        
        # Add client info
        user.height = 180
        user.weight = 80
        user.age = 30
        user.gender = "Male"
        user.save()
        
        self.assertTrue(user.is_onboarding_completed)
        
        # Test trainer logic
        trainer = User.objects.create_user(
            email='trainer@test.com',
            username='traineruser',
            password=self.password,
            user_type='trainer',
            first_name="Jane",
            last_name="Doe",
            phone_number='+2222222222',
            is_active=True
        )
        self.assertTrue(trainer.is_onboarding_completed) # Trainers only need name
        
    def test_token_endpoint_response(self):
        """Test api/auth/token/ response"""
        url = reverse('users:token_obtain_pair')
        data = {
            'email': self.client_user.email,
            'password': self.password
        }
        
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        
        json_data = response.json()
        self.assertIn('user', json_data)
        user_info = json_data['user']
        
        self.assertIn('is_active', user_info)
        self.assertIn('onboarding_completed', user_info)
        
        self.assertTrue(user_info['is_active'])
        self.assertFalse(user_info['onboarding_completed'])
        
    def test_verify_otp_response(self):
        """Test api/auth/verify-otp/ response"""
        # Create inactive user for OTP
        user = User.objects.create_user(
            email='otp@test.com',
            username='otpuser',
            password=self.password,
            user_type='client',
            first_name="OTP",
            last_name="User",
            phone_number='+3333333333'
        )
        user.is_active = False
        user.save()
        
        # Create OTP
        otp_code = '123456'
        OTPVerification.objects.create(
            user=user,
            email=user.email,
            otp_code=otp_code,
            expires_at=timezone.now() + timezone.timedelta(minutes=10)
        )
        
        url = reverse('users:verify_otp')
        data = {
            'email': user.email,
            'otp_code': otp_code
        }
        
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        
        json_data = response.json()
        self.assertIn('user', json_data)
        user_info = json_data['user']
        
        self.assertIn('is_active', user_info)
        self.assertIn('onboarding_completed', user_info)
        
        self.assertTrue(user_info['is_active'])
        # User has First/Last name but missing height/weight etc
        self.assertFalse(user_info['onboarding_completed'])

    def test_user_update_response(self):
        """Test api/auth/user/update/ response"""
        from rest_framework_simplejwt.tokens import RefreshToken
        
        # Generate token
        refresh = RefreshToken.for_user(self.client_user)
        access_token = str(refresh.access_token)
        
        url = reverse('users:update_user_details')
        
        # Use token in header
        response = self.client.get(
            url, 
            HTTP_AUTHORIZATION=f'Bearer {access_token}'
        )
        self.assertEqual(response.status_code, 200)
        
        user_info = response.json()
        self.assertIn('is_active', user_info)
        self.assertIn('onboarding_completed', user_info)
        
        self.assertTrue(user_info['is_active'])
        self.assertFalse(user_info['onboarding_completed'])
        
    def test_trainer_profile_response(self):
        """Test api/auth/trainer/profile/ response"""
        from rest_framework_simplejwt.tokens import RefreshToken
        
        # Create and login trainer
        trainer = User.objects.create_user(
            email='trainer_prof@test.com',
            username='trainer_prof',
            password=self.password,
            user_type='trainer',
            first_name="Jane",
            last_name="Doe",
            phone_number='+4444444444',
            is_active=True
        )
        
        # Generate token
        refresh = RefreshToken.for_user(trainer)
        access_token = str(refresh.access_token)
        
        url = reverse('users:trainer_profile')
        
        response = self.client.get(
            url, 
            HTTP_AUTHORIZATION=f'Bearer {access_token}'
        )
        self.assertEqual(response.status_code, 200)
        
        user_info = response.json()
        self.assertIn('is_active', user_info)
        self.assertIn('onboarding_completed', user_info)
        
        self.assertTrue(user_info['is_active'])
        self.assertTrue(user_info['onboarding_completed'])
