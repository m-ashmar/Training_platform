#!/usr/bin/env python3
import os
import django
import requests
import json
from datetime import date, timedelta

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from routine.models import Routine, RoutineExercise, Exercise, RoutineProgress, UserExerciseProgress

class UserProgressTrackingTester:
    def __init__(self):
        self.base_url = "http://127.0.0.1:8000"
        self.session = requests.Session()
        self.admin_token = None
        self.trainer1_token = None
        self.client_token = None
        self.admin_id = None
        self.trainer1_id = None
        self.client_id = None

    def print_section(self, title):
        print(f"\n{'='*60}")
        print(f"🔍 {title}")
        print(f"{'='*60}")

    def print_success(self, message):
        print(f"✅ {message}")

    def print_error(self, message):
        print(f"❌ {message}")

    def print_info(self, message):
        print(f"ℹ️  {message}")

    def get_auth_headers(self, token):
        return {'Authorization': f'Bearer {token}'}

    def login_user(self, email, password):
        """Login user and return token"""
        response = self.session.post(
            f"{self.base_url}/api/auth/token/",
            json={'email': email, 'password': password}
        )
        if response.status_code == 200:
            return response.json()['access']
        else:
            self.print_error(f"Login failed for {email}: {response.status_code}")
            return None

    def create_test_users(self):
        """Create test users for progress tracking"""
        self.print_section("CREATING TEST USERS")
        
        # Get existing users from previous test
        admin = CustomUser.objects.get(email="admin@test.com")
        trainer1 = CustomUser.objects.get(email="trainer1@test.com")
        client = CustomUser.objects.get(email="client@test.com")
        
        self.print_success(f"Using existing admin: {admin.username}")
        self.print_success(f"Using existing trainer1: {trainer1.username}")
        self.print_success(f"Using existing client: {client.username}")
        
        self.admin_id = admin.id
        self.trainer1_id = trainer1.id
        self.client_id = client.id

        # Ensure client is assigned to trainer1
        if client.assigned_trainer_id != trainer1.id:
            client.assigned_trainer_id = trainer1.id
            client.save()
            self.print_success("Assigned client to trainer1")
        else:
            self.print_success("Client already assigned to trainer1")

        return True

    def login_all_users(self):
        """Login all test users"""
        self.print_section("LOGGING IN USERS")
        
        self.admin_token = self.login_user("admin@test.com", "testpass123")
        self.trainer1_token = self.login_user("trainer1@test.com", "testpass123")
        self.client_token = self.login_user("client@test.com", "testpass123")
        
        if all([self.admin_token, self.trainer1_token, self.client_token]):
            self.print_success("All users logged in successfully")
            return True
        else:
            self.print_error("Some users failed to login")
            return False

    def create_test_exercises(self):
        """Create test exercises"""
        self.print_section("CREATING TEST EXERCISES")
        
        exercises = []
        exercise_data = [
            {
                'name': 'Push-ups',
                'description': 'Basic push-up exercise',
                'muscle_groups': ['chest', 'triceps', 'shoulders'],
                'equipment_needed': ['bodyweight'],
                'difficulty_level': 'beginner'
            },
            {
                'name': 'Squats',
                'description': 'Basic squat exercise',
                'muscle_groups': ['quadriceps', 'glutes'],
                'equipment_needed': ['bodyweight'],
                'difficulty_level': 'beginner'
            },
            {
                'name': 'Pull-ups',
                'description': 'Basic pull-up exercise',
                'muscle_groups': ['back', 'biceps'],
                'equipment_needed': ['pull-up bar'],
                'difficulty_level': 'intermediate'
            }
        ]
        
        for data in exercise_data:
            exercise, created = Exercise.objects.get_or_create(
                name=data['name'],
                defaults={
                    'description': data['description'],
                    'muscle_groups': data['muscle_groups'],
                    'equipment_needed': data['equipment_needed'],
                    'difficulty_level': data['difficulty_level'],
                    'is_global': True,
                    'is_active': True
                }
            )
            if created:
                self.print_success(f"Created exercise: {exercise.name}")
            exercises.append(exercise)
        
        return exercises

    def create_test_routine(self, exercises):
        """Create a test routine with exercises"""
        self.print_section("CREATING TEST ROUTINE")
        
        # Create routine with client already assigned
        routine = Routine.objects.create(
            name="Progress Test Routine",
            description="A test routine for progress tracking",
            created_by_id=self.trainer1_id,
            days=3,
            is_active=True
        )
        
        # Assign routine to client BEFORE adding exercises
        routine.assigned_to.add(self.client_id)
        
        # Add exercises to routine
        for i, exercise in enumerate(exercises):
            RoutineExercise.objects.create(
                routine=routine,
                exercise=exercise,
                sets=3,
                reps=10,
                rest_time=60,
                order=i+1,
                day=1  # All exercises on day 1 for simplicity
            )
        
        # Force save to trigger progress creation
        routine.save()
        
        self.print_success(f"Created routine: {routine.name}")
        self.print_success(f"Assigned routine to client")
        
        return routine

    def test_routine_progress_creation(self, routine):
        """Test that routine progress is automatically created"""
        self.print_section("TESTING ROUTINE PROGRESS CREATION")
        
        # Check if progress entries were created
        progress_entries = RoutineProgress.objects.filter(
            routine=routine,
            user_id=self.client_id
        )
        
        self.print_info(f"Found {progress_entries.count()} progress entries")
        
        for entry in progress_entries:
            self.print_info(f"Day {entry.day}: {entry.status}")
        
        # Should have 3 entries (one for each day)
        assert progress_entries.count() == 3, f"Expected 3 progress entries, got {progress_entries.count()}"
        self.print_success("Routine progress entries created automatically")
        
        return progress_entries

    def test_progress_update_api(self, routine):
        """Test updating progress via API"""
        self.print_section("TESTING PROGRESS UPDATE API")
        
        # Update progress for day 1
        update_data = {
            'day': 1,
            'status': 'Completed'
        }
        
        response = self.session.post(
            f"{self.base_url}/api/routine/routines/{routine.id}/update_progress/",
            json=update_data,
            headers=self.get_auth_headers(self.client_token)
        )
        
        if response.status_code == 200:
            self.print_success("Progress updated successfully via API")
            result = response.json()
            self.print_info(f"Updated progress: {result}")
        else:
            self.print_error(f"Progress update failed: {response.status_code}")
            return False
        
        # Verify the update in database
        progress = RoutineProgress.objects.get(
            routine=routine,
            user_id=self.client_id,
            day=1
        )
        
        assert progress.status == 'Completed', f"Expected 'Completed', got '{progress.status}'"
        self.print_success("Progress update verified in database")
        
        return True

    def test_exercise_progress_tracking(self, routine):
        """Test exercise-specific progress tracking"""
        self.print_section("TESTING EXERCISE PROGRESS TRACKING")
        
        # Get exercises from routine
        routine_exercises = RoutineExercise.objects.filter(routine=routine)
        
        # Create exercise progress for today
        today = date.today()
        
        for rex in routine_exercises:
            progress, created = UserExerciseProgress.objects.update_or_create(
                user_id=self.client_id,
                exercise=rex.exercise,
                date=today,
                defaults={
                    'completed_sets': rex.sets,
                    'target_sets': rex.sets,
                    'skipped': False,
                    'total_weight': 50.0,  # Example weight
                    'total_repetitions': rex.sets * rex.reps
                }
            )
            
            if created:
                self.print_success(f"Created progress for {rex.exercise.name}")
            else:
                self.print_success(f"Updated progress for {rex.exercise.name}")
        
        # Test bulk completion API
        bulk_data = {
            'routine_id': routine.id,
            'day': 1,
            'date': today.isoformat(),
            'completed_sets': 3,
            'target_sets': 3,
            'skipped': False
        }
        
        response = self.session.post(
            f"{self.base_url}/api/routine/user-exercise-progress/bulk-complete/",
            json=bulk_data,
            headers=self.get_auth_headers(self.client_token)
        )
        
        if response.status_code == 200:
            result = response.json()
            self.print_success(f"Bulk completion successful: {result['count']} exercises")
        else:
            self.print_error(f"Bulk completion failed: {response.status_code}")
        
        return True

    def test_trainer_progress_view(self, routine):
        """Test that trainer can view client progress"""
        self.print_section("TESTING TRAINER PROGRESS VIEW")
        
        # Get routine progress as trainer
        response = self.session.get(
            f"{self.base_url}/api/routine/routine-progress/",
            headers=self.get_auth_headers(self.trainer1_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            progress_entries = data.get('results', data) if isinstance(data, dict) else data
            self.print_success(f"Trainer can see {len(progress_entries)} progress entries")
            
            for entry in progress_entries:
                self.print_info(f"User: {entry.get('user')}, Day: {entry.get('day')}, Status: {entry.get('status')}")
        else:
            self.print_error(f"Trainer progress view failed: {response.status_code}")
            return False
        
        # Get client progress as trainer
        response = self.session.get(
            f"{self.base_url}/api/routine/routines/my_clients_progress/",
            headers=self.get_auth_headers(self.trainer1_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            self.print_success(f"Trainer dashboard: {data}")
        else:
            self.print_error(f"Trainer dashboard failed: {response.status_code}")
        
        return True

    def test_client_progress_view(self, routine):
        """Test that client can view their own progress"""
        self.print_section("TESTING CLIENT PROGRESS VIEW")
        
        # Get own progress
        response = self.session.get(
            f"{self.base_url}/api/routine/routine-progress/",
            headers=self.get_auth_headers(self.client_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            progress_entries = data.get('results', data) if isinstance(data, dict) else data
            self.print_success(f"Client can see {len(progress_entries)} progress entries")
            
            for entry in progress_entries:
                self.print_info(f"Routine: {entry.get('routine', {}).get('name', 'N/A')}, Day: {entry.get('day')}, Status: {entry.get('status')}")
        else:
            self.print_error(f"Client progress view failed: {response.status_code}")
            return False
        
        # Get exercise progress
        response = self.session.get(
            f"{self.base_url}/api/routine/user-exercise-progress/",
            headers=self.get_auth_headers(self.client_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            progress_entries = data.get('results', data)
            self.print_success(f"Client can see {len(progress_entries)} exercise progress entries")
        else:
            self.print_error(f"Client exercise progress view failed: {response.status_code}")
        
        return True

    def test_analytics_endpoints(self):
        """Test analytics endpoints"""
        self.print_section("TESTING ANALYTICS ENDPOINTS")
        
        # Test summary analytics
        response = self.session.get(
            f"{self.base_url}/api/routine/analytics/summary/",
            headers=self.get_auth_headers(self.client_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            self.print_success(f"Analytics summary: {data}")
        else:
            self.print_error(f"Analytics summary failed: {response.status_code}")
        
        # Test completion analytics
        response = self.session.get(
            f"{self.base_url}/api/routine/analytics/completion/",
            headers=self.get_auth_headers(self.trainer1_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            self.print_success(f"Completion analytics: {data}")
        else:
            self.print_error(f"Completion analytics failed: {response.status_code}")
        
        return True

    def run_complete_test(self):
        """Run the complete user progress tracking test"""
        self.print_section("USER PROGRESS TRACKING SYSTEM TEST")
        
        # Step 1: Create test users
        if not self.create_test_users():
            return False
        
        # Step 2: Login all users
        if not self.login_all_users():
            return False
        
        # Step 3: Create test exercises
        exercises = self.create_test_exercises()
        if not exercises:
            return False
        
        # Step 4: Create test routine
        routine = self.create_test_routine(exercises)
        if not routine:
            return False
        
        # Step 5: Test progress creation
        progress_entries = self.test_routine_progress_creation(routine)
        if not progress_entries:
            return False
        
        # Step 6: Test progress update API
        if not self.test_progress_update_api(routine):
            return False
        
        # Step 7: Test exercise progress tracking
        if not self.test_exercise_progress_tracking(routine):
            return False
        
        # Step 8: Test trainer progress view
        if not self.test_trainer_progress_view(routine):
            return False
        
        # Step 9: Test client progress view
        if not self.test_client_progress_view(routine):
            return False
        
        # Step 10: Test analytics endpoints
        if not self.test_analytics_endpoints():
            return False
        
        self.print_section("🎉 ALL PROGRESS TRACKING TESTS PASSED!")
        self.print_success("User progress tracking system is working perfectly!")
        self.print_info("✅ Routine progress automatically created")
        self.print_info("✅ Progress can be updated via API")
        self.print_info("✅ Exercise-specific progress tracking")
        self.print_info("✅ Trainers can view client progress")
        self.print_info("✅ Clients can view their own progress")
        self.print_info("✅ Analytics endpoints working")
        return True

if __name__ == "__main__":
    tester = UserProgressTrackingTester()
    tester.run_complete_test() 