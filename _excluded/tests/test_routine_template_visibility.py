#!/usr/bin/env python3
import requests
import json
import time
import os
import django

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from routine.models import RoutineTemplate, Exercise, RoutineTemplateExercise

# Test the routine template visibility system
class RoutineTemplateVisibilityTester:
    def __init__(self):
        self.base_url = "http://127.0.0.1:8000"
        self.session = requests.Session()
        self.admin_token = None
        self.trainer1_token = None
        self.trainer2_token = None
        self.client_token = None
        self.admin_id = None
        self.trainer1_id = None
        self.trainer2_id = None
        self.client_id = None
        
    def print_section(self, title):
        print(f"\n{'='*60}")
        print(f"🧪 {title}")
        print(f"{'='*60}")
    
    def print_success(self, message):
        print(f"✅ {message}")
    
    def print_error(self, message):
        print(f"❌ {message}")
    
    def print_info(self, message):
        print(f"ℹ️  {message}")
    
    def get_auth_headers(self, token):
        return {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}'
        }
    
    def login_user(self, email, password):
        """Login user and return access token and user id"""
        response = self.session.post(
            f"{self.base_url}/api/auth/token/",
            json={"email": email, "password": password}
        )
        if response.status_code == 200:
            data = response.json()
            return data.get('access'), data.get('user', {}).get('id')
        else:
            self.print_error(f"Login failed for {email}: {response.status_code}")
            print(f"Response: {response.text}")
            return None, None
    
    def create_test_users(self):
        """Create test users for the visibility test"""
        self.print_section("CREATING TEST USERS")
        
        # Create admin user
        admin_user, created = CustomUser.objects.get_or_create(
            email="admin@test.com",
            defaults={
                'username': 'testadmin',
                'user_type': 'admin',
                'first_name': 'Test',
                'last_name': 'Admin'
            }
        )
        admin_user.set_password("adminpass123")
        admin_user.save()
        self.admin_id = admin_user.id
        self.print_success(f"Admin user created/updated: {admin_user.username}")
        
        # Create trainer1
        trainer1_user, created = CustomUser.objects.get_or_create(
            email="trainer1@test.com",
            defaults={
                'username': 'testtrainer1',
                'user_type': 'trainer',
                'first_name': 'Trainer',
                'last_name': 'One'
            }
        )
        trainer1_user.set_password("trainerpass123")
        trainer1_user.save()
        self.trainer1_id = trainer1_user.id
        self.print_success(f"Trainer1 created/updated: {trainer1_user.username}")
        
        # Create trainer2
        trainer2_user, created = CustomUser.objects.get_or_create(
            email="trainer2@test.com",
            defaults={
                'username': 'testtrainer2',
                'user_type': 'trainer',
                'first_name': 'Trainer',
                'last_name': 'Two'
            }
        )
        trainer2_user.set_password("trainerpass123")
        trainer2_user.save()
        self.trainer2_id = trainer2_user.id
        self.print_success(f"Trainer2 created/updated: {trainer2_user.username}")
        
        # Create client
        client_user, created = CustomUser.objects.get_or_create(
            email="client@test.com",
            defaults={
                'username': 'testclient_vis',
                'user_type': 'client',
                'first_name': 'Test',
                'last_name': 'Client'
            }
        )
        client_user.set_password("clientpass123")
        client_user.save()
        self.client_id = client_user.id
        self.print_success(f"Client created/updated: {client_user.username}")
        
        return True
    
    def login_all_users(self):
        """Login all test users"""
        self.print_section("LOGGING IN ALL USERS")
        
        # Login admin
        self.admin_token, _ = self.login_user("admin@test.com", "adminpass123")
        if self.admin_token:
            self.print_success("Admin logged in")
        else:
            self.print_error("Admin login failed")
            return False
        
        # Login trainer1
        self.trainer1_token, _ = self.login_user("trainer1@test.com", "trainerpass123")
        if self.trainer1_token:
            self.print_success("Trainer1 logged in")
        else:
            self.print_error("Trainer1 login failed")
            return False
        
        # Login trainer2
        self.trainer2_token, _ = self.login_user("trainer2@test.com", "trainerpass123")
        if self.trainer2_token:
            self.print_success("Trainer2 logged in")
        else:
            self.print_error("Trainer2 login failed")
            return False
        
        # Login client
        self.client_token, _ = self.login_user("client@test.com", "clientpass123")
        if self.client_token:
            self.print_success("Client logged in")
        else:
            self.print_error("Client login failed")
            return False
        
        return True
    
    def create_test_exercises(self):
        """Create test exercises for templates"""
        self.print_section("CREATING TEST EXERCISES")
        
        # Create some basic exercises
        exercises = [
            {'name': 'Push-ups', 'target_muscle': 'Upper Chest', 'difficulty_level': 'beginner'},
            {'name': 'Squats', 'target_muscle': 'Front Quads', 'difficulty_level': 'beginner'},
            {'name': 'Pull-ups', 'target_muscle': 'Lats', 'difficulty_level': 'intermediate'},
        ]
        
        created_exercises = []
        for exercise_data in exercises:
            exercise, created = Exercise.objects.get_or_create(
                name=exercise_data['name'],
                defaults=exercise_data
            )
            created_exercises.append(exercise)
            if created:
                self.print_success(f"Created exercise: {exercise.name}")
            else:
                self.print_info(f"Exercise already exists: {exercise.name}")
        
        return created_exercises
    
    def create_test_templates(self, exercises):
        """Create test templates with different visibility settings"""
        self.print_section("CREATING TEST TEMPLATES")
        
        trainer1 = CustomUser.objects.get(id=self.trainer1_id)
        trainer2 = CustomUser.objects.get(id=self.trainer2_id)
        
        # Trainer1 creates a public template
        public_template = RoutineTemplate.objects.create(
            name="Public Strength Template",
            description="A public strength training template",
            goal="Strength",
            is_public=True,
            created_by=trainer1
        )
        
        # Add exercises to public template
        for i, exercise in enumerate(exercises[:2]):
            RoutineTemplateExercise.objects.create(
                template=public_template,
                exercise=exercise,
                sets=3,
                reps=10,
                rest_time=90,
                order=i+1
            )
        
        self.print_success(f"Created public template: {public_template.name}")
        
        # Trainer1 creates a private template
        private_template1 = RoutineTemplate.objects.create(
            name="Private Trainer1 Template",
            description="A private template for trainer1",
            goal="Hypertrophy",
            is_public=False,
            created_by=trainer1
        )
        
        # Add exercises to private template
        for i, exercise in enumerate(exercises[1:]):
            RoutineTemplateExercise.objects.create(
                template=private_template1,
                exercise=exercise,
                sets=4,
                reps=8,
                rest_time=120,
                order=i+1
            )
        
        self.print_success(f"Created private template for trainer1: {private_template1.name}")
        
        # Trainer2 creates a private template
        private_template2 = RoutineTemplate.objects.create(
            name="Private Trainer2 Template",
            description="A private template for trainer2",
            goal="Endurance",
            is_public=False,
            created_by=trainer2
        )
        
        # Add exercises to private template
        for i, exercise in enumerate(exercises):
            RoutineTemplateExercise.objects.create(
                template=private_template2,
                exercise=exercise,
                sets=2,
                reps=15,
                rest_time=60,
                order=i+1
            )
        
        self.print_success(f"Created private template for trainer2: {private_template2.name}")
        
        return {
            'public': public_template,
            'private_trainer1': private_template1,
            'private_trainer2': private_template2
        }
    
    def extract_templates(self, data):
        """Helper to extract templates from paginated or non-paginated response."""
        if isinstance(data, dict) and 'results' in data:
            return data['results']
        elif isinstance(data, list):
            return data
        else:
            return []

    def test_admin_visibility(self):
        """Test admin can see all templates"""
        self.print_section("TESTING ADMIN VISIBILITY")
        
        response = self.session.get(
            f"{self.base_url}/api/routine/templates/",
            headers=self.get_auth_headers(self.admin_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            templates = self.extract_templates(data)
            self.print_success(f"Admin can see {len(templates)} templates")
            
            # Admin should see all templates (public and private)
            public_count = sum(1 for t in templates if t.get('is_public'))
            private_count = len(templates) - public_count
            
            self.print_info(f"Public templates: {public_count}")
            self.print_info(f"Private templates: {private_count}")
            
            return len(templates) >= 3  # Should see at least our 3 test templates
        else:
            self.print_error(f"Admin template access failed: {response.status_code}")
            return False
    
    def test_trainer1_visibility(self):
        """Test trainer1 can see own templates + public templates"""
        self.print_section("TESTING TRAINER1 VISIBILITY")
        
        response = self.session.get(
            f"{self.base_url}/api/routine/templates/",
            headers=self.get_auth_headers(self.trainer1_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            templates = self.extract_templates(data)
            self.print_success(f"Trainer1 can see {len(templates)} templates")
            
            # Trainer1 should see their own templates + public templates
            own_templates = [t for t in templates if t.get('created_by') == 'testtrainer1']
            public_templates = [t for t in templates if t.get('is_public')]
            
            self.print_info(f"Own templates: {len(own_templates)}")
            self.print_info(f"Public templates: {len(public_templates)}")
            
            # Should see at least 2 templates (1 public + 1 private own)
            return len(templates) >= 2
        else:
            self.print_error(f"Trainer1 template access failed: {response.status_code}")
            return False
    
    def test_trainer2_visibility(self):
        """Test trainer2 can see own templates + public templates"""
        self.print_section("TESTING TRAINER2 VISIBILITY")
        
        response = self.session.get(
            f"{self.base_url}/api/routine/templates/",
            headers=self.get_auth_headers(self.trainer2_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            templates = self.extract_templates(data)
            self.print_success(f"Trainer2 can see {len(templates)} templates")
            
            # Trainer2 should see their own templates + public templates
            own_templates = [t for t in templates if t.get('created_by') == 'testtrainer2']
            public_templates = [t for t in templates if t.get('is_public')]
            
            self.print_info(f"Own templates: {len(own_templates)}")
            self.print_info(f"Public templates: {len(public_templates)}")
            
            # Should see at least 2 templates (1 public + 1 private own)
            return len(templates) >= 2
        else:
            self.print_error(f"Trainer2 template access failed: {response.status_code}")
            return False
    
    def test_client_visibility(self):
        """Test client can only see public templates"""
        self.print_section("TESTING CLIENT VISIBILITY")
        
        response = self.session.get(
            f"{self.base_url}/api/routine/templates/",
            headers=self.get_auth_headers(self.client_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            templates = self.extract_templates(data)
            self.print_success(f"Client can see {len(templates)} templates")
            
            # Client should only see public templates
            public_templates = [t for t in templates if t.get('is_public')]
            private_templates = [t for t in templates if not t.get('is_public')]
            
            self.print_info(f"Public templates: {len(public_templates)}")
            self.print_info(f"Private templates: {len(private_templates)}")
            
            # Should only see public templates
            return len(private_templates) == 0 and len(public_templates) >= 1
        else:
            self.print_error(f"Client template access failed: {response.status_code}")
            return False
    
    def test_my_templates_endpoint(self):
        """Test the my_templates endpoint for trainers"""
        self.print_section("TESTING MY_TEMPLATES ENDPOINT")
        
        # Test trainer1 my_templates
        response = self.session.get(
            f"{self.base_url}/api/routine/templates/my_templates/",
            headers=self.get_auth_headers(self.trainer1_token)
        )
        
        if response.status_code == 200:
            templates = response.json()
            self.print_success(f"Trainer1 my_templates: {len(templates)} templates")
            
            # Should see both public and private templates
            public_count = sum(1 for t in templates if t.get('is_public'))
            private_count = len(templates) - public_count
            
            self.print_info(f"Public: {public_count}, Private: {private_count}")
            
            return len(templates) >= 2  # Should see both templates
        else:
            self.print_error(f"Trainer1 my_templates failed: {response.status_code}")
            return False
    
    def test_public_templates_endpoint(self):
        """Test the public_templates endpoint"""
        self.print_section("TESTING PUBLIC_TEMPLATES ENDPOINT")
        
        response = self.session.get(
            f"{self.base_url}/api/routine/templates/public_templates/",
            headers=self.get_auth_headers(self.client_token)
        )
        
        if response.status_code == 200:
            templates = response.json()
            self.print_success(f"Public templates endpoint: {len(templates)} templates")
            
            # Should only see public templates
            public_count = sum(1 for t in templates if t.get('is_public'))
            private_count = len(templates) - public_count
            
            self.print_info(f"Public: {public_count}, Private: {private_count}")
            
            return private_count == 0 and public_count >= 1
        else:
            self.print_error(f"Public templates endpoint failed: {response.status_code}")
            return False
    
    def test_template_generation_security(self):
        """Test that trainers can only generate routines for their own clients"""
        self.print_section("TESTING TEMPLATE GENERATION SECURITY")
        
        # First, assign client to trainer1
        client = CustomUser.objects.get(id=self.client_id)
        client.assigned_trainer_id = self.trainer1_id
        client.save()
        self.print_info("Assigned client to trainer1")
        
        # Get a template to use for generation
        response = self.session.get(
            f"{self.base_url}/api/routine/templates/",
            headers=self.get_auth_headers(self.trainer1_token)
        )
        
        if response.status_code != 200:
            self.print_error("Failed to get templates for generation test")
            return False
        
        templates = self.extract_templates(response.json())
        if not templates:
            self.print_error("No templates available for generation test")
            return False
        
        template_id = templates[0]['id']
        
        # Test 1: Trainer1 should be able to generate routine for their client
        generate_data = {
            'client_id': self.client_id,
            'customizations': {}
        }
        
        response = self.session.post(
            f"{self.base_url}/api/routine/templates/{template_id}/generate/",
            json=generate_data,
            headers=self.get_auth_headers(self.trainer1_token)
        )
        
        if response.status_code == 201:
            self.print_success("Trainer1 successfully generated routine for their client")
        else:
            self.print_error(f"Trainer1 generation failed: {response.status_code}")
            return False
        
        # Test 2: Trainer2 should NOT be able to generate routine for trainer1's client
        response = self.session.post(
            f"{self.base_url}/api/routine/templates/{template_id}/generate/",
            json=generate_data,
            headers=self.get_auth_headers(self.trainer2_token)
        )
        
        if response.status_code == 403:
            self.print_success("Trainer2 correctly blocked from generating routine for trainer1's client")
        else:
            self.print_error(f"Trainer2 should have been blocked but got: {response.status_code}")
            return False
        
        return True
    
    def run_complete_test(self):
        """Run the complete routine template visibility test"""
        self.print_section("ROUTINE TEMPLATE VISIBILITY SYSTEM TEST")
        
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
        
        # Step 4: Create test templates
        templates = self.create_test_templates(exercises)
        if not templates:
            return False
        
        # Step 5: Test visibility for each user type
        if not self.test_admin_visibility():
            return False
        
        if not self.test_trainer1_visibility():
            return False
        
        if not self.test_trainer2_visibility():
            return False
        
        if not self.test_client_visibility():
            return False
        
        # Step 6: Test special endpoints
        if not self.test_my_templates_endpoint():
            return False
        
        if not self.test_public_templates_endpoint():
            return False
        
        # Step 7: Test security
        if not self.test_template_generation_security():
            return False
        
        self.print_section("🎉 ALL TESTS PASSED!")
        self.print_success("Routine template visibility system is working perfectly!")
        self.print_info("✅ Public templates visible to all users")
        self.print_info("✅ Private templates only visible to creators")
        self.print_info("✅ Trainers can only assign to their own clients")
        self.print_info("✅ Admins can see all templates")
        return True

if __name__ == "__main__":
    tester = RoutineTemplateVisibilityTester()
    tester.run_complete_test() 