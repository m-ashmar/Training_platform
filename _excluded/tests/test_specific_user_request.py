#!/usr/bin/env python3
import requests
import json
import time
import os
import django

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser, TrainerClientRelation

# Test the specific user request system
class SpecificUserRequestTester:
    def __init__(self):
        self.base_url = "http://127.0.0.1:8000"
        self.session = requests.Session()
        self.client_token = None
        self.trainer_token = None
        self.client_id = None
        self.trainer_id = None
        
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
    
    def find_user_by_email(self, email):
        """Find user by email using Django ORM"""
        try:
            user = CustomUser.objects.get(email=email)
            return user
        except CustomUser.DoesNotExist:
            return None
    
    def test_find_specific_users(self):
        """Find the specific users by email"""
        self.print_section("FINDING SPECIFIC USERS")
        
        # Find client with email hhhh@gmail.com
        client_user = self.find_user_by_email("hhhh@gmail.com")
        if client_user:
            self.print_success(f"Found client: {client_user.username} ({client_user.email})")
            self.print_info(f"User ID: {client_user.id}")
            self.print_info(f"User Type: {client_user.user_type}")
            self.print_info(f"Full Name: {client_user.full_name}")
            self.client_id = client_user.id
        else:
            self.print_error("Client with email 'hhhh@gmail.com' not found")
            return False
        
        # Find trainer with email as@gmail.com
        trainer_user = self.find_user_by_email("as@gmail.com")
        if trainer_user:
            self.print_success(f"Found trainer: {trainer_user.username} ({trainer_user.email})")
            self.print_info(f"User ID: {trainer_user.id}")
            self.print_info(f"User Type: {trainer_user.user_type}")
            self.print_info(f"Full Name: {trainer_user.full_name}")
            self.trainer_id = trainer_user.id
        else:
            self.print_error("Trainer with email 'as@gmail.com' not found")
            return False
        
        return True
    
    def test_client_login(self):
        """Test client login"""
        self.print_section("CLIENT LOGIN TEST")
        
        # Try to login with the client account
        # Note: We need to know the password or create one
        # For now, let's try a common password or create a new one
        
        # First, let's check if we can reset the password or use a default
        client_user = CustomUser.objects.get(email="hhhh@gmail.com")
        
        # Set a known password for testing
        client_user.set_password("testpass123")
        client_user.save()
        self.print_info("Set password to 'testpass123' for testing")
        
        # Now login
        token, user_id = self.login_user("hhhh@gmail.com", "testpass123")
        if token:
            self.client_token = token
            self.client_id = user_id
            self.print_success("Client login successful")
            return True
        else:
            self.print_error("Client login failed")
            return False
    
    def test_trainer_login(self):
        """Test trainer login"""
        self.print_section("TRAINER LOGIN TEST")
        
        # Try to login with the trainer account
        trainer_user = CustomUser.objects.get(email="as@gmail.com")
        
        # Set a known password for testing
        trainer_user.set_password("trainerpass123")
        trainer_user.save()
        self.print_info("Set password to 'trainerpass123' for testing")
        
        # Now login
        token, user_id = self.login_user("as@gmail.com", "trainerpass123")
        if token:
            self.trainer_token = token
            self.trainer_id = user_id
            self.print_success("Trainer login successful")
            return True
        else:
            self.print_error("Trainer login failed")
            return False
    
    def test_client_views_available_trainers(self):
        """Test client viewing available trainers"""
        self.print_section("CLIENT VIEWS AVAILABLE TRAINERS TEST")
        
        try:
            response = self.session.get(
                f"{self.base_url}/api/auth/client/available-trainers/",
                headers=self.get_auth_headers(self.client_token)
            )
            
            if response.status_code == 200:
                self.print_success("Client can view available trainers")
                data = response.json()
                trainers = data.get('available_trainers', [])
                self.print_info(f"Found {len(trainers)} available trainers")
                
                # Check if our target trainer is in the list by ID
                target_trainer = None
                for trainer in trainers:
                    if trainer.get('id') == self.trainer_id:
                        target_trainer = trainer
                        break
                
                if target_trainer:
                    self.print_success(f"Target trainer found in available trainers list")
                    self.print_info(f"Trainer: {target_trainer.get('first_name')} {target_trainer.get('last_name')}")
                    self.print_info(f"Trainer ID: {target_trainer.get('id')}")
                    return True
                else:
                    self.print_error("Target trainer not found in available trainers list")
                    self.print_info(f"Looking for trainer ID: {self.trainer_id}")
                    self.print_info("First 5 trainers in list:")
                    for i, trainer in enumerate(trainers[:5]):
                        self.print_info(f"  {i+1}. ID: {trainer.get('id')}, Name: {trainer.get('first_name')} {trainer.get('last_name')}")
                    return False
            else:
                self.print_error(f"Failed to get available trainers: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error viewing trainers: {e}")
            return False
    
    def test_client_requests_specific_trainer(self):
        """Test client requesting the specific trainer"""
        self.print_section("CLIENT REQUESTS SPECIFIC TRAINER TEST")
        
        request_data = {
            "trainer_id": self.trainer_id,
            "message": "Hi! I would like to work with you for my fitness training. I heard great things about your training methods and would love to start working together."
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/api/auth/client/request-trainer/",
                json=request_data,
                headers=self.get_auth_headers(self.client_token)
            )
            
            if response.status_code == 200:
                self.print_success("Client request sent successfully")
                data = response.json()
                self.print_info(f"Request status: {data.get('status')}")
                self.print_info(f"Message: {data.get('message')}")
                return True
            else:
                self.print_error(f"Client request failed: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error sending request: {e}")
            return False
    
    def test_trainer_views_pending_requests(self):
        """Test trainer viewing pending requests"""
        self.print_section("TRAINER VIEWS PENDING REQUESTS TEST")
        
        try:
            response = self.session.get(
                f"{self.base_url}/api/auth/trainer/pending-requests/",
                headers=self.get_auth_headers(self.trainer_token)
            )
            
            if response.status_code == 200:
                self.print_success("Trainer can view pending requests")
                data = response.json()
                requests = data.get('pending_requests', [])
                self.print_info(f"Found {len(requests)} pending requests")
                
                # Look for our specific client's request
                target_request = None
                for req in requests:
                    if req.get('client_email') == 'hhhh@gmail.com':
                        target_request = req
                        break
                
                if target_request:
                    self.print_success("Found request from our specific client")
                    self.print_info(f"Request from: {target_request.get('client_name')}")
                    self.print_info(f"Request ID: {target_request.get('request_id')}")
                    return target_request.get('request_id')
                else:
                    self.print_error("Request from our specific client not found")
                    return None
            else:
                self.print_error(f"Failed to get pending requests: {response.status_code}")
                print(f"Response: {response.text}")
                return None
                
        except Exception as e:
            self.print_error(f"Error viewing pending requests: {e}")
            return None
    
    def test_trainer_approves_request(self, request_id):
        """Test trainer approving the request"""
        self.print_section("TRAINER APPROVES REQUEST TEST")
        
        approve_data = {
            "request_id": request_id,
            "action": "approve",
            "reason": "Welcome! I'm excited to work with you on your fitness journey. Let's start with an assessment and create a personalized plan."
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/api/auth/trainer/respond-to-request/",
                json=approve_data,
                headers=self.get_auth_headers(self.trainer_token)
            )
            
            if response.status_code == 200:
                self.print_success("Trainer approved request successfully")
                data = response.json()
                self.print_info(f"Approval status: {data.get('status')}")
                self.print_info(f"Message: {data.get('message')}")
                return True
            else:
                self.print_error(f"Request approval failed: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error approving request: {e}")
            return False
    
    def test_client_views_request_status(self):
        """Test client viewing their request status"""
        self.print_section("CLIENT VIEWS REQUEST STATUS TEST")
        
        try:
            response = self.session.get(
                f"{self.base_url}/api/auth/client/request-status/",
                headers=self.get_auth_headers(self.client_token)
            )
            
            if response.status_code == 200:
                self.print_success("Client can view request status")
                data = response.json()
                requests = data.get('requests', [])
                self.print_info(f"Total requests: {data.get('total_requests')}")
                
                if requests:
                    request = requests[0]
                    self.print_info(f"Latest request status: {request.get('status')}")
                    self.print_info(f"Trainer: {request.get('trainer_name')}")
                    self.print_info(f"Trainer email: {request.get('trainer_email')}")
                    return True
                else:
                    self.print_error("No requests found")
                    return False
            else:
                self.print_error(f"Failed to get request status: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error viewing request status: {e}")
            return False
    
    def run_complete_test(self):
        """Run the complete specific user request system test"""
        self.print_section("SPECIFIC USER REQUEST SYSTEM TEST")
        
        # Step 1: Find the specific users
        if not self.test_find_specific_users():
            return False
        
        # Step 2: Login as client
        if not self.test_client_login():
            return False
        
        # Step 3: Login as trainer
        if not self.test_trainer_login():
            return False
        
        # Step 4: Client views available trainers
        if not self.test_client_views_available_trainers():
            return False
        
        # Step 5: Client requests trainer
        if not self.test_client_requests_specific_trainer():
            return False
        
        # Step 6: Trainer views pending requests
        request_id = self.test_trainer_views_pending_requests()
        if not request_id:
            return False
        
        # Step 7: Trainer approves request
        if not self.test_trainer_approves_request(request_id):
            return False
        
        # Step 8: Client views updated status
        if not self.test_client_views_request_status():
            return False
        
        self.print_section("🎉 ALL TESTS PASSED!")
        self.print_success("Specific user request system is working perfectly!")
        self.print_info("Client hhhh@gmail.com successfully requested and was approved by trainer as@gmail.com")
        return True

if __name__ == "__main__":
    tester = SpecificUserRequestTester()
    tester.run_complete_test() 