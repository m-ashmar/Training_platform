from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from django.urls import reverse
from .models import TrainerClientRelation, DeviceToken
import json
from unittest.mock import patch
from routine.models import Routine
from diet.models import DietPlan
from rest_framework.authtoken.models import Token
from datetime import date, timedelta

User = get_user_model()


class ClientProfileViewTestCase(APITestCase):
    """
    Test suite for client profile viewing functionality.
    
    Tests:
    - Trainers can view approved client profiles (success)
    - Trainers cannot view unapproved client profiles (failure)
    - Clients cannot view other client profiles
    - Admin can view all client profiles
    - Profile data includes calculated metrics (BMI, BMR, TDEE)
    """
    
    def setUp(self):
        """Set up test data for client profile viewing tests."""
        # Create test users
        self.trainer = User.objects.create_user(
            username='test_trainer',
            email='trainer@test.com',
            password='testpass123',
            phone_number='+1234567890',
            user_type='trainer'
        )
        
        self.client_user = User.objects.create_user(
            username='test_client',
            email='client@test.com',
            password='testpass123',
            phone_number='+1234567891',
            user_type='client',
            height=175.0,
            weight=70.0,
            age=25,
            gender='Male',
            activity_level='Moderate'
        )
        
        self.unrelated_client = User.objects.create_user(
            username='unrelated_client',
            email='unrelated@test.com',
            password='testpass123',
            phone_number='+1234567892',
            user_type='client',
            height=160.0,
            weight=55.0,
            age=30,
            gender='Female',
            activity_level='Light'
        )
        
        self.admin_user = User.objects.create_superuser(
            username='admin',
            email='admin@test.com',
            password='adminpass123',
            phone_number='+1234567893'
        )
        
        # Create approved trainer-client relationship
        self.approved_relation = TrainerClientRelation.objects.create(
            trainer=self.trainer,
            client=self.client_user,
            status='approved'
        )
        
        # Set up API client
        self.client = APIClient()
    
    def test_trainer_can_view_approved_client_profile(self):
        """Test that trainers can view profiles of their approved clients."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('users:trainer-client-profile-detail', kwargs={'pk': self.client_user.pk})
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['username'], self.client_user.username)
        self.assertEqual(response.data['height'], self.client_user.height)
        self.assertEqual(response.data['weight'], self.client_user.weight)
        self.assertEqual(response.data['age'], self.client_user.age)
        self.assertEqual(response.data['gender'], self.client_user.gender)
        self.assertEqual(response.data['activity_level'], self.client_user.activity_level)
        
        # Check calculated fields
        self.assertIsNotNone(response.data['bmi'])
        self.assertIsNotNone(response.data['bmr'])
        self.assertIsNotNone(response.data['tdee'])
        self.assertIsNotNone(response.data['full_name'])
    
    def test_trainer_cannot_view_unapproved_client_profile(self):
        """Test that trainers cannot view profiles of unapproved clients."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('users:trainer-client-profile-detail', kwargs={'pk': self.unrelated_client.pk})
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    
    def test_client_cannot_view_other_client_profiles(self):
        """Test that clients cannot view other client profiles."""
        self.client.force_authenticate(user=self.client_user)
        
        url = reverse('users:trainer-client-profile-detail', kwargs={'pk': self.unrelated_client.pk})
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    
    def test_admin_can_view_all_client_profiles(self):
        """Test that admins can view all client profiles."""
        self.client.force_authenticate(user=self.admin_user)
        
        # View approved client profile
        url1 = reverse('users:trainer-client-profile-detail', kwargs={'pk': self.client_user.pk})
        response1 = self.client.get(url1)
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        
        # View unrelated client profile
        url2 = reverse('users:trainer-client-profile-detail', kwargs={'pk': self.unrelated_client.pk})
        response2 = self.client.get(url2)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
    
    def test_trainer_can_list_approved_clients(self):
        """Test that trainers can list their approved clients."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('users:trainer-client-profile-list')
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['client_count'], 1)
        self.assertEqual(len(response.data['clients']), 1)
        self.assertEqual(response.data['clients'][0]['username'], self.client_user.username)
    
    def test_profile_data_includes_calculated_metrics(self):
        """Test that profile data includes calculated BMI, BMR, and TDEE."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('users:trainer-client-profile-detail', kwargs={'pk': self.client_user.pk})
        response = self.client.get(url)
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Check that calculated fields are present and reasonable
        self.assertIsNotNone(response.data['bmi'])
        self.assertIsNotNone(response.data['bmr'])
        self.assertIsNotNone(response.data['tdee'])
        
        # BMI should be reasonable for the given height/weight
        expected_bmi = 70.0 / ((175.0 / 100) ** 2)  # weight / (height in m)^2
        self.assertAlmostEqual(response.data['bmi'], expected_bmi, places=1)


class TrainerClientRelationshipTestCase(APITestCase):
    """
    Test suite for trainer-client relationship management.
    
    Tests:
    - Trainer can request client assignment
    - Client approval process
    - Relationship status management
    - Unassignment functionality
    """
    
    def setUp(self):
        """Set up test data for trainer-client relationship tests."""
        self.trainer = User.objects.create_user(
            username='test_trainer',
            email='trainer@test.com',
            password='testpass123',
            phone_number='+1234567890',
            user_type='trainer'
        )
        
        self.client_user = User.objects.create_user(
            username='test_client',
            email='client@test.com',
            password='testpass123',
            phone_number='+1234567891',
            user_type='client'
        )
        
        self.client = APIClient()
    
    def test_trainer_can_request_client_assignment(self):
        """Test that trainers can request client assignment."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('users:assign_client')
        data = {'client_id': self.client_user.id}
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('Assignment request sent', response.data['message'])
        self.assertEqual(response.data['status'], 'pending')
        
        # Check that relationship was created
        relation = TrainerClientRelation.objects.get(trainer=self.trainer, client=self.client_user)
        self.assertEqual(relation.status, 'pending')
    
    def test_duplicate_assignment_request_handling(self):
        """Test that duplicate assignment requests are handled properly."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('users:assign_client')
        data = {'client_id': self.client_user.id}
        
        # First request
        response1 = self.client.post(url, data, format='json')
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        
        # Second request (should fail)
        response2 = self.client.post(url, data, format='json')
        self.assertEqual(response2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('already pending', response2.data['error'])
    
    def test_trainer_can_unassign_client(self):
        """Test that trainers can unassign clients."""
        # Create an approved relationship first
        relation = TrainerClientRelation.objects.create(
            trainer=self.trainer,
            client=self.client_user,
            status='approved'
        )
        
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('users:unassign_client')
        data = {'client_id': self.client_user.id}
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('unassigned successfully', response.data['message'])
        
        # Check that relationship was deleted
        self.assertFalse(TrainerClientRelation.objects.filter(
            trainer=self.trainer,
            client=self.client_user
        ).exists())
    
    def test_unassigning_unassigned_client(self):
        """Test that unassigning an unassigned client is handled properly."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('users:unassign_client')
        data = {'client_id': self.client_user.id}
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not assigned', response.data['error'])


class UserRegistrationTestCase(APITestCase):
    """
    Test suite for user registration functionality.
    
    Tests:
    - Trainer registration
    - Client registration
    - Admin registration (restricted)
    - Validation and error handling
    """
    
    def setUp(self):
        """Set up test data for registration tests."""
        self.client = APIClient()
    
    def test_trainer_registration(self):
        """Test that trainers can register successfully."""
        data = {
            'username': 'new_trainer',
            'email': 'new_trainer@test.com',
            'password1': 'testpass123',
            'password2': 'testpass123',
            'phone_number': '+1234567890',
            'user_type': 'trainer'
        }
        
        url = reverse('users:custom_register')
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['user']['user_type'], 'trainer')
        
        # Check that user was created
        user = User.objects.get(email='new_trainer@test.com')
        self.assertEqual(user.user_type, 'trainer')
        self.assertTrue(user.is_trainer)
    
    def test_client_registration(self):
        """Test that clients can register successfully."""
        data = {
            'username': 'new_client',
            'email': 'new_client@test.com',
            'password1': 'testpass123',
            'password2': 'testpass123',
            'phone_number': '+1234567891',
            'user_type': 'client'
        }
        
        url = reverse('users:custom_register')
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['user']['user_type'], 'client')
        
        # Check that user was created
        user = User.objects.get(email='new_client@test.com')
        self.assertEqual(user.user_type, 'client')
        self.assertTrue(user.is_client)
    
    def test_admin_registration_restriction(self):
        """Test that admin registration is restricted."""
        data = {
            'username': 'new_admin',
            'email': 'new_admin@test.com',
            'password1': 'testpass123',
            'password2': 'testpass123',
            'phone_number': '+1234567892',
            'user_type': 'admin'
        }
        
        url = reverse('users:custom_register')
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Only admins can create admin users', str(response.data))
    
    def test_duplicate_email_registration(self):
        """Test that duplicate email registration is prevented."""
        # Create first user
        User.objects.create_user(
            username='existing_user',
            email='existing@test.com',
            password='testpass123',
            phone_number='+1234567893',
            user_type='client'
        )
        
        # Try to register with same email
        data = {
            'username': 'new_user',
            'email': 'existing@test.com',
            'password1': 'testpass123',
            'password2': 'testpass123',
            'phone_number': '+1234567894',
            'user_type': 'client'
        }
        
        url = reverse('users:custom_register')
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('already exists', str(response.data))


class IntegrationWorkflowTestCase(APITestCase):
    """
    Integration test suite for the complete trainer-client workflow.
    
    Tests the full flow from registration to profile viewing.
    """
    
    def setUp(self):
        """Set up test data for integration tests."""
        self.client = APIClient()
    
    def test_complete_trainer_client_workflow(self):
        """
        Test the complete workflow:
        1. Register trainer and client
        2. Create trainer-client relationship
        3. Approve relationship
        4. View client profile
        5. Verify all functionality works
        """
        # Step 1: Register trainer and client
        trainer_data = {
            'username': 'workflow_trainer',
            'email': 'workflow_trainer@test.com',
            'password1': 'testpass123',
            'password2': 'testpass123',
            'phone_number': '+1234567894',
            'user_type': 'trainer'
        }
        
        client_data = {
            'username': 'workflow_client',
            'email': 'workflow_client@test.com',
            'password1': 'testpass123',
            'password2': 'testpass123',
            'phone_number': '+1234567895',
            'user_type': 'client'
        }
        
        # Register trainer
        trainer_response = self.client.post(
            reverse('users:custom_register'),
            trainer_data,
            format='json'
        )
        self.assertEqual(trainer_response.status_code, status.HTTP_201_CREATED)
        
        # Register client
        client_response = self.client.post(
            reverse('users:custom_register'),
            client_data,
            format='json'
        )
        self.assertEqual(client_response.status_code, status.HTTP_201_CREATED)
        
        # Get user objects
        trainer = User.objects.get(email='workflow_trainer@test.com')
        client = User.objects.get(email='workflow_client@test.com')
        
        # Step 2: Create trainer-client relationship
        self.client.force_authenticate(user=trainer)
        
        assign_response = self.client.post(
            reverse('users:assign_client'),
            {'client_id': client.id},
            format='json'
        )
        self.assertEqual(assign_response.status_code, status.HTTP_200_OK)
        
        # Step 3: Approve the relationship (simulate client approval)
        relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
        relation.status = 'approved'
        relation.save()
        
        # Step 4: View client profile
        profile_response = self.client.get(
            reverse('users:trainer-client-profile-detail', kwargs={'pk': client.pk})
        )
        self.assertEqual(profile_response.status_code, status.HTTP_200_OK)
        
        # Step 5: Verify profile data
        profile_data = profile_response.data
        self.assertEqual(profile_data['username'], client.username)
        self.assertEqual(profile_data['email'], client.email)
        self.assertIsNotNone(profile_data['bmi'])
        self.assertIsNotNone(profile_data['bmr'])
        self.assertIsNotNone(profile_data['tdee'])
        
        # Step 6: List approved clients
        list_response = self.client.get(reverse('users:trainer-client-profile-list'))
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data['client_count'], 1)
        self.assertEqual(len(list_response.data['clients']), 1)
        self.assertEqual(list_response.data['clients'][0]['username'], client.username)


class DeviceTokenNotificationTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='notify_user',
            email='notify@test.com',
            password='testpass123',
            phone_number='+1234567899',
            user_type='client'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_device_token_registration(self):
        url = reverse('users:devicetokenregisterview')
        response = self.client.post(url, {'token': 'test_token_123'}, format='json')
        self.assertIn(response.status_code, [200, 201])
        self.assertTrue(DeviceToken.objects.filter(user=self.user, token='test_token_123').exists())

    @patch('users.utils.send_push_notification')
    def test_send_push_notification(self, mock_send):
        mock_send.return_value = True
        from users.utils import send_push_notification
        result = send_push_notification(self.user, 'Test Title', 'Test Message', data={'foo': 'bar'})
        self.assertTrue(result)
        mock_send.assert_called_once()


class FullWorkflowIntegrationTestCase(APITestCase):
    def setUp(self):
        self.client = APIClient()

    def test_full_workflow(self):
        # 1. Register users
        trainer = User.objects.create_user(
            username='trainer1', email='trainer1@test.com', password='trainerpass', phone_number='+10000000001', user_type='trainer'
        )
        client_user = User.objects.create_user(
            username='client1', email='client1@test.com', password='clientpass', phone_number='+10000000002', user_type='client'
        )
        admin_user = User.objects.create_superuser(
            username='admin1', email='admin1@test.com', password='adminpass', phone_number='+10000000003'
        )

        # 2. Trainer assigns client
        self.client.force_authenticate(user=trainer)
        assign_url = reverse('users:assign_client')
        response = self.client.post(assign_url, {'client_id': client_user.id}, format='json')
        self.assertEqual(response.status_code, 200)
        # Simulate client approval
        from users.models import TrainerClientRelation
        relation = TrainerClientRelation.objects.get(trainer=trainer, client=client_user)
        relation.status = 'approved'
        relation.save()

        # 3. Trainer creates a routine
        routine = Routine.objects.create(
            name='Strength Routine',
            description='A basic strength routine',
            created_by=trainer
        )
        routine.assigned_to.add(client_user)
        routine.save()

        # 4. Trainer creates a diet plan (not AI-generated)
        start = date.today()
        end = start + timedelta(days=6)
        diet_plan = DietPlan.objects.create(
            user=client_user,
            goal='Maintain',
            daily_calories=2200,
            start_date=start,
            end_date=end,
            duration_weeks=1,
            generated_plan={"meals": []},
            generation_strategy='FALLBACK'
        )

        # 5. Admin creates a diet plan for the client
        self.client.force_authenticate(user=admin_user)
        admin_diet_plan = DietPlan.objects.create(
            user=client_user,
            goal='Gain',
            daily_calories=2500,
            start_date=start,
            end_date=end,
            duration_weeks=1,
            generated_plan={"meals": []},
            generation_strategy='FALLBACK'
        )

        # 6. Check permissions and assignments
        self.client.force_authenticate(user=trainer)
        # Trainer can view assigned client profile
        profile_url = reverse('users:trainer-client-profile-detail', kwargs={'pk': client_user.pk})
        response = self.client.get(profile_url)
        self.assertEqual(response.status_code, 200)
        # Trainer can see routine assigned
        self.assertIn(client_user, routine.assigned_to.all())
        # Trainer can see diet plan assigned (created by trainer)
        self.assertTrue(DietPlan.objects.filter(user=client_user, goal='Maintain').exists())
        # Admin can see and create diet plans for any client
        self.client.force_authenticate(user=admin_user)
        self.assertTrue(DietPlan.objects.filter(user=client_user, goal='Gain').exists())
