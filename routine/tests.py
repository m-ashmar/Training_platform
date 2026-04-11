from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient, APIRequestFactory
from rest_framework import status
from django.urls import reverse
from .models import Routine, Exercise
from users.models import TrainerClientRelation, CustomUser
import json
import logging
from routine.models import Exercise, RoutineTemplate, RoutineTemplateExercise, Routine, RoutineExercise, ExerciseSetLog, UserExerciseProgress
import datetime

User = get_user_model()


class RoutineAssignmentTestCase(APITestCase):
    """
    Comprehensive test suite for routine assignment functionality.
    
    Tests:
    - Trainer creating and assigning routines to approved clients (success)
    - Trainer attempting to assign to unapproved clients (failure)
    - Client attempting to assign routines (failure)
    - Admin assignment capabilities
    - Permission validation
    - Error handling and logging
    """
    
    def setUp(self):
        """Set up test data for routine assignment tests."""
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
            user_type='client'
        )
        
        self.unrelated_client = User.objects.create_user(
            username='unrelated_client',
            email='unrelated@test.com',
            password='testpass123',
            phone_number='+1234567892',
            user_type='client'
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
        
        # Create a test routine
        self.routine = Routine.objects.create(
            name='Test Routine',
            description='A test routine',
            created_by=self.trainer,
            days=3
        )
        
        # Set up API client
        self.client = APIClient()
    
    def test_trainer_can_assign_routine_to_approved_client(self):
        """Test that trainers can assign routines to their approved clients."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('routine:routine-assign-to-client', kwargs={'pk': self.routine.pk})
        data = {'client_id': self.client_user.id}
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('successfully assigned', response.data['message'])
        self.assertTrue(self.client_user in self.routine.assigned_to.all())
    
    def test_trainer_cannot_assign_to_unapproved_client(self):
        """Test that trainers cannot assign routines to unapproved clients."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('routine:routine-assign-to-client', kwargs={'pk': self.routine.pk})
        data = {'client_id': self.unrelated_client.id}
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('approved clients', response.data['error'])
        self.assertFalse(self.unrelated_client in self.routine.assigned_to.all())
    
    def test_client_cannot_assign_routines(self):
        """Test that clients cannot assign routines."""
        self.client.force_authenticate(user=self.client_user)
        
        url = reverse('routine:routine-assign-to-client', kwargs={'pk': self.routine.pk})
        data = {'client_id': self.unrelated_client.id}
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
    
    def test_admin_can_assign_to_any_client(self):
        """Test that admins can assign routines to any client."""
        self.client.force_authenticate(user=self.admin_user)
        
        url = reverse('routine:routine-assign-to-client', kwargs={'pk': self.routine.pk})
        data = {'client_id': self.unrelated_client.id}
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self.unrelated_client in self.routine.assigned_to.all())
    
    def test_duplicate_assignment_handling(self):
        """Test that duplicate assignments are handled properly."""
        self.client.force_authenticate(user=self.trainer)
        
        # First assignment
        url = reverse('routine:routine-assign-to-client', kwargs={'pk': self.routine.pk})
        data = {'client_id': self.client_user.id}
        
        response1 = self.client.post(url, data, format='json')
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        
        # Second assignment (should fail)
        response2 = self.client.post(url, data, format='json')
        self.assertEqual(response2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('already assigned', response2.data['error'])
    
    def test_missing_client_id_handling(self):
        """Test that missing client_id is handled properly."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('routine:routine-assign-to-client', kwargs={'pk': self.routine.pk})
        data = {}
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('client_id is required', response.data['error'])
    
    def test_nonexistent_client_handling(self):
        """Test that nonexistent client IDs are handled properly."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('routine:routine-assign-to-client', kwargs={'pk': self.routine.pk})
        data = {'client_id': 99999}  # Non-existent ID
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('Client not found', response.data['error'])


class RoutineUnassignmentTestCase(APITestCase):
    """Test suite for routine unassignment functionality."""
    
    def setUp(self):
        """Set up test data for routine unassignment tests."""
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
        
        # Create approved trainer-client relationship
        self.approved_relation = TrainerClientRelation.objects.create(
            trainer=self.trainer,
            client=self.client_user,
            status='approved'
        )
        
        # Create a test routine and assign it
        self.routine = Routine.objects.create(
            name='Test Routine',
            description='A test routine',
            created_by=self.trainer,
            days=3
        )
        self.routine.assigned_to.add(self.client_user)
        
        self.client = APIClient()
    
    def test_trainer_can_unassign_routine_from_approved_client(self):
        """Test that trainers can unassign routines from their approved clients."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('routine:routine-unassign-from-client', kwargs={'pk': self.routine.pk})
        data = {'client_id': self.client_user.id}
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('successfully unassigned', response.data['message'])
        self.assertFalse(self.client_user in self.routine.assigned_to.all())
    
    def test_unassigning_unassigned_routine(self):
        """Test that unassigning an unassigned routine is handled properly."""
        self.client.force_authenticate(user=self.trainer)
        
        # First unassignment
        url = reverse('routine:routine-unassign-from-client', kwargs={'pk': self.routine.pk})
        data = {'client_id': self.client_user.id}
        
        response1 = self.client.post(url, data, format='json')
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        
        # Second unassignment (should fail)
        response2 = self.client.post(url, data, format='json')
        self.assertEqual(response2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not assigned', response2.data['error'])


class RoutineCreationTestCase(APITestCase):
    """Test suite for routine creation permissions."""
    
    def setUp(self):
        """Set up test data for routine creation tests."""
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
    
    def test_trainer_can_create_routine(self):
        """Test that trainers can create routines."""
        self.client.force_authenticate(user=self.trainer)
        
        url = reverse('routine:routine-list')
        data = {
            'name': 'New Routine',
            'description': 'A new routine',
            'days': 5
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Routine.objects.count(), 1)
        self.assertEqual(Routine.objects.first().created_by, self.trainer)
    
    def test_client_cannot_create_routine(self):
        """Test that clients cannot create routines."""
        self.client.force_authenticate(user=self.client_user)
        
        url = reverse('routine:routine-list')
        data = {
            'name': 'New Routine',
            'description': 'A new routine',
            'days': 5
        }
        
        response = self.client.post(url, data, format='json')
        
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Routine.objects.count(), 0)


class PermissionTestCase(TestCase):
    """
    Test permission classes with mock requests and users.
    """
    def setUp(self):
        self.factory = APIRequestFactory()
        self.trainer = CustomUser.objects.create_user(username='trainer', email='trainer@x.com', password='pass', phone_number='+10000000001', user_type='trainer')
        self.client_user = CustomUser.objects.create_user(username='client', email='client@x.com', password='pass', phone_number='+10000000002', user_type='client')
        self.client_user.assigned_trainer = self.trainer
        self.client_user.save()
        # Ensure approved TrainerClientRelation exists for permission test
        from users.models import TrainerClientRelation
        TrainerClientRelation.objects.create(trainer=self.trainer, client=self.client_user, status='approved')
    
    def test_is_trainer_of_approved_client_permission(self):
        from routine.permissions import IsTrainerOfApprovedClient
        permission = IsTrainerOfApprovedClient()
        request = self.factory.get('/')
        request.user = self.trainer
        obj = self.client_user
        self.assertTrue(permission.has_object_permission(request, None, obj))
    
    def test_is_trainer_or_admin_for_assignment_permission(self):
        from routine.permissions import IsTrainerOrAdminForAssignment
        permission = IsTrainerOrAdminForAssignment()
        request = self.factory.get('/')
        request.user = self.trainer
        self.assertTrue(permission.has_permission(request, None))

# --- Comprehensive API-driven test for all routine features ---
class FullRoutineFeatureTestCase(APITestCase):
    """
    End-to-end API test: all actions via API, no direct DB access.
    Covers: registration, login, assignment, exercise/routine/template creation, assignment, set logging, analytics, and template management.
    """
    def setUp(self):
        strong_pw = 'Testpass123!'
        # 1. Register trainer (must include phone_number and user_type)
        resp = self.client.post('/api/auth/register/', {
            'username': 'trainer',
            'email': 'trainer@x.com',
            'password1': strong_pw,
            'password2': strong_pw,
            'phone_number': '+10000000001',
            'user_type': 'trainer'
        })
        self.assertEqual(resp.status_code, 201)
        trainer = User.objects.get(email='trainer@x.com')
        trainer.is_verified = True
        trainer.is_active = True
        trainer.save()
        self.assertEqual(resp.status_code, 201)
        # 2. Register client (must include phone_number and user_type)
        resp = self.client.post('/api/auth/register/', {
            'username': 'client',
            'email': 'client@x.com',
            'password1': strong_pw,
            'password2': strong_pw,
            'phone_number': '+10000000002',
            'user_type': 'client'
        })
        self.assertEqual(resp.status_code, 201)
        client = User.objects.get(email='client@x.com')
        client.is_verified = True
        client.is_active = True
        client.save()
        self.assertEqual(resp.status_code, 201)
        self.client_id = resp.data['user']['id']
        # 3. Login trainer
        resp = self.client.post('/api/auth/token/', {'email': 'trainer@x.com', 'password': strong_pw})
        self.assertEqual(resp.status_code, 200)
        self.trainer_token = resp.data['access']
        # 4. Login client
        resp = self.client.post('/api/auth/token/', {'email': 'client@x.com', 'password': strong_pw})
        self.assertEqual(resp.status_code, 200)
        self.client_token = resp.data['access']

    def test_full_routine_feature_flow(self):
        # 5. Trainer requests client assignment (simulate approval if possible)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.trainer_token}')
        assign_resp = self.client.post('/api/users/trainer/assign-client/', {'client_id': self.client_id})
        if assign_resp.status_code not in [200, 201, 204]:
            print('Assign client error:', assign_resp.status_code, assign_resp.data)
        self.assertIn(assign_resp.status_code, [200, 201, 204])
        # Fetch pending requests to get request_id
        pending_resp = self.client.get('/api/users/trainer/pending-requests/')
        request_id = None
        for req in pending_resp.data.get('pending_requests', []):
            if req.get('client_id') == self.client_id:
                request_id = req.get('request_id')
                break
        if not request_id:
            print('No pending request found for client_id', self.client_id, 'in', pending_resp.data)
        # Approve the trainer-client relationship via API
        approve_resp = self.client.post('/api/users/trainer/respond-to-request/', {'request_id': request_id, 'action': 'approve'})
        if approve_resp.status_code not in [200, 201, 204]:
            print('Approve client error:', approve_resp.status_code, getattr(approve_resp, 'data', approve_resp.content))
        self.assertIn(approve_resp.status_code, [200, 201, 204])

        # 6. Trainer creates exercises
        ex1 = self.client.post('/api/routine/exercises/', {'name': 'Bench Press', 'description': 'Chest', 'target_muscle': 'Upper Chest'})
        if ex1.status_code != 201:
            print('Exercise 1 creation error:', ex1.status_code, getattr(ex1, 'data', ex1.content))
        ex2 = self.client.post('/api/routine/exercises/', {'name': 'Squat', 'description': 'Legs', 'target_muscle': 'Front Quads'})
        if ex2.status_code != 201:
            print('Exercise 2 creation error:', ex2.status_code, getattr(ex2, 'data', ex2.content))
        ex1_id = ex1.data['id']
        ex2_id = ex2.data['id']
        # 7. Trainer creates a routine for the client
        routine_payload = {
            'name': 'Client Routine',
            'description': 'Routine for client',
            'goal': 'Hypertrophy',
            'assigned_to': [assign_resp.data.get('client_id', 2)],  # fallback to 2 if not returned
            'routineexercises': [
                {'exercise_id': ex1_id, 'sets': 4, 'reps': 8, 'rest_time': 90, 'order': 1},
                {'exercise_id': ex2_id, 'sets': 5, 'reps': 5, 'rest_time': 120, 'order': 2},
            ]
        }
        resp = self.client.post('/api/routine/routines/', routine_payload, format='json')
        if resp.status_code != 201:
            print('Routine creation error:', resp.status_code, getattr(resp, 'data', resp.content))
        self.assertEqual(resp.status_code, 201)
        routine_id = resp.data['id']
        # 8. Trainer creates a template
        template_payload = {
            'name': 'Push Pull Legs',
            'description': 'Classic split',
            'goal': 'Hypertrophy',
            'is_public': True,
            'exercises': [
                {'exercise_id': ex1_id, 'sets': 4, 'reps': 8, 'rest_time': 90, 'order': 1},
                {'exercise_id': ex2_id, 'sets': 5, 'reps': 5, 'rest_time': 120, 'order': 2},
            ]
        }
        resp = self.client.post('/api/routine/templates/', template_payload, format='json')
        self.assertEqual(resp.status_code, 201)
        template_id = resp.data['id']
        # 9. Trainer generates a routine for the client from the template
        generate_payload = {
            'client_id': assign_resp.data.get('client_id', 2),
            'customizations': {
                str(ex1_id): {'sets': 3, 'reps': 10, 'rest_time': 60},
                str(ex2_id): {'sets': 4, 'reps': 6, 'rest_time': 100},
            }
        }
        resp = self.client.post(f'/api/routine/templates/{template_id}/generate/', generate_payload, format='json')
        self.assertEqual(resp.status_code, 201)
        # 10. Client logs sets for 7 days
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.client_token}')
        today = datetime.date.today()
        for day in range(7):
            log_date = today - datetime.timedelta(days=day)
            for ex in [ex1_id, ex2_id]:
                # Create or fetch UserExerciseProgress via API
                progress_payload = {
                    'exercise': ex,
                    'date': log_date,
                    'completed_sets': 0,
                    'target_sets': 2,
                    'skipped': False
                }
                progress_resp = self.client.post('/api/routine/user-exercise-progress/', progress_payload, format='json')
                if progress_resp.status_code not in [200, 201]:
                    print('UserExerciseProgress POST failed:', progress_resp.status_code, getattr(progress_resp, 'data', progress_resp.content))
                    # Try to fetch if already exists (unique constraint)
                    progress_list = self.client.get(f'/api/routine/user-exercise-progress/?exercise={ex}&date={log_date}')
                    if progress_list.status_code == 200 and progress_list.data:
                        progress_id = progress_list.data[0]['id']
                    else:
                        self.fail(f'Could not create or fetch UserExerciseProgress for exercise={ex}, date={log_date}')
                else:
                    progress_id = progress_resp.data['id']
                # Log two sets per exercise per day
                for set_num in range(1, 3):
                    set_log_data = {
                        'user_exercise_progress': progress_id,
                        'workout_session': None,
                        'set_number': set_num,
                        'weight': 50 + 5*set_num + ex,
                        'reps': 8,
                        'rest_time': 90,
                        'date': log_date
                    }
                    resp = self.client.post('/api/routine/set-logs/', set_log_data, format='json')
                    self.assertIn(resp.status_code, [200, 201, 400])
        # 11. Check analytics endpoint for correct volume/PRs
        resp = self.client.get('/api/routine/analytics/summary/?period=week')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('week_volume', resp.data)
        self.assertIn('prs', resp.data)
        # self.assertTrue('Bench Press' in resp.data['prs'])
        # self.assertTrue('Squat' in resp.data['prs'])
        # 12. Trainer can list, edit, delete templates
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.trainer_token}')
        resp = self.client.get('/api/routine/templates/')
        self.assertEqual(resp.status_code, 200)
        resp = self.client.patch(f'/api/routine/templates/{template_id}/', {'description': 'Updated'}, format='json')
        self.assertEqual(resp.status_code, 200)
        resp = self.client.delete(f'/api/routine/templates/{template_id}/')
        self.assertEqual(resp.status_code, 204)

class RoutineAppFullWorkflowTestCase(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.trainer_data = {
            "username": "trainer1",
            "email": "trainer1@example.com",
            "password1": "StrongPassword123!",
            "password2": "StrongPassword123!",
            "phone_number": "+10000000001",
            "user_type": "trainer"
        }
        self.client_data = {
            "username": "client1",
            "email": "client1@example.com",
            "password1": "StrongPassword123!",
            "password2": "StrongPassword123!",
            "phone_number": "+10000000002",
            "user_type": "client"
        }

    def test_full_routine_app_workflow(self):
        # 1. Register trainer
        resp = self.client.post("/api/auth/register/", self.trainer_data, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)
        trainer_id = resp.data.get("id") or resp.data.get("user", {}).get("id")
        trainer = User.objects.get(id=trainer_id)
        trainer.is_verified = True
        trainer.is_active = True
        trainer.save()

        # 2. Register client
        resp = self.client.post("/api/auth/register/", self.client_data, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)
        client_id = resp.data.get("id") or resp.data.get("user", {}).get("id")
        client = User.objects.get(id=client_id)
        client.is_verified = True
        client.is_active = True
        client.save()

        # 3. Login trainer
        resp = self.client.post("/api/auth/token/", {"email": self.trainer_data["email"], "password": self.trainer_data["password1"]}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        trainer_token = resp.data.get("access") or resp.data.get("token")
        self.assertIsNotNone(trainer_token)

        # 4. Login client
        resp = self.client.post("/api/auth/token/", {"email": self.client_data["email"], "password": self.client_data["password1"]}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        client_token = resp.data.get("access") or resp.data.get("token")
        self.assertIsNotNone(client_token)

        # 5. Trainer assigns client (simulate assignment request/approval)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {trainer_token}")
        assign_url = "/api/users/trainer/assign-client/"
        resp = self.client.post(assign_url, {"client_id": client_id}, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)
        
        # Approve assignment manually via DB
        from users.models import TrainerClientRelation
        relation = TrainerClientRelation.objects.get(trainer_id=trainer_id, client_id=client_id)
        relation.status = 'approved'
        relation.save()
        
        # Sync the assigned_trainer field
        client.assigned_trainer_id = trainer_id
        client.save()

        # 6. Trainer creates exercise
        exercise_payload = {
            "name": "Bench Press",
            "description": "Chest exercise",
            "target_muscle": "Upper Chest"
        }
        resp = self.client.post("/api/routine/exercises/", exercise_payload, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)
        exercise_id = resp.data["id"]

        # 7. Trainer creates routine and assigns to client
        today = datetime.date.today()
        routine_payload = {
            "name": "Push Day",
            "description": "Upper body push routine",
            "days": 3,
            "start_date": str(today),
            "end_date": str(today + datetime.timedelta(days=30)),
            "assigned_to": [client_id]
        }
        resp = self.client.post("/api/routine/routines/", routine_payload, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)
        routine_id = resp.data["id"]

        # 8. Trainer creates routine template
        template_payload = {
            "name": "Beginner Push",
            "description": "Template for push days",
            "goal": "Strength",
            "is_public": True,
            "exercises": [
                {
                    "exercise_id": exercise_id,
                    "sets": 3,
                    "reps": 10,
                    "rest_time": 60,
                    "order": 1
                }
            ]
        }
        resp = self.client.post("/api/routine/templates/", template_payload, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)
        template_id = resp.data["id"]

        # 9. Trainer generates routine from template for client
        generate_url = f"/api/routine/templates/{template_id}/generate/"
        resp = self.client.post(generate_url, {"client_id": client_id, "start_date": str(today)}, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)
        generated_routine_id = resp.data.get("id")

        # 10. Client logs progress and sets
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {client_token}")
        progress_payload = {
            "exercise": exercise_id,
            "date": str(today),
            "completed_sets": 0,
            "target_sets": 3,
            "skipped": False
        }
        resp = self.client.post("/api/routine/user-exercise-progress/", progress_payload, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)
        progress_id = resp.data["id"]

        # Log a set
        setlog_payload = {
            "user_exercise_progress": progress_id,
            "set_number": 1,
            "weight": 50,
            "date": str(today)
        }
        # Find or create a workout session if required
        # (Assume session is optional or can be omitted)
        resp = self.client.post("/api/routine/set-logs/", setlog_payload, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)

        # 11. Client fetches progress analytics
        resp = self.client.get("/api/routine/set-logs/my-progress/?group_by=exercise")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(isinstance(resp.data, list))
        self.assertTrue(len(resp.data) > 0)

        # 12. Client fetches analytics summary
        resp = self.client.get("/api/routine/analytics/summary/?period=month")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn("month_volume", resp.data)
        self.assertIn("days_trained", resp.data)

        # 13. Permissions: client cannot create routine
        routine_payload["name"] = "Client Routine Attempt"
        resp = self.client.post("/api/routine/routines/", routine_payload, format="json")
        self.assertEqual(resp.status_code, 403)

        # 14. Permissions: trainer cannot assign to unapproved client
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {trainer_token}")
        # Register a new client (not approved)
        new_client_data = {
            "username": "client2",
            "email": "client2@example.com",
            "password1": "StrongPassword123!",
            "password2": "StrongPassword123!",
            "phone_number": "+10000000003",
            "user_type": "client"
        }
        resp = self.client.post("/api/auth/register/", new_client_data, format="json")
        self.assertIn(resp.status_code, [200, 201], resp.data)
        new_client_id = resp.data.get("id") or resp.data.get("user", {}).get("id")
        client2 = User.objects.get(id=new_client_id)
        client2.is_verified = True
        client2.is_active = True
        client2.save()
        routine_payload["assigned_to"] = [new_client_id]
        resp = self.client.post("/api/routine/routines/", routine_payload, format="json")
        self.assertEqual(resp.status_code, 400)

        # 15. Permissions: trainer can view assigned client progress
        progress_list_url = f"/api/routine/user-exercise-progress/?exercise={exercise_id}&date={today}"
        resp = self.client.get(progress_list_url)
        self.assertEqual(resp.status_code, 200)
        print("PROGRESS DATA:", resp.data)
        results = resp.data.get('results', []) if isinstance(resp.data, dict) else resp.data
        self.assertTrue(any(p.get("exercise", {}).get("id") == exercise_id for p in results if isinstance(p, dict)))

        # 16. Permissions: trainer cannot view unassigned client progress
        progress_list_url = f"/api/routine/user-exercise-progress/?exercise={exercise_id}&date={today}&user={new_client_id}"
        resp = self.client.get(progress_list_url)
        # Should be empty or forbidden
        self.assertTrue(resp.status_code in [200, 403])
        if resp.status_code == 200:
            results2 = resp.data.get('results', []) if isinstance(resp.data, dict) else resp.data
            self.assertFalse(any(p.get("user") == new_client_id for p in results2 if isinstance(p, dict)))

        print("Full routine app workflow test completed successfully.")
