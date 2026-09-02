#!/usr/bin/env python3
import requests
import json
import time

# Test the complete client-trainer request system
class ClientTrainerRequestTester:
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
    
    def test_client_registration(self):
        """Test client registration"""
        self.print_section("CLIENT REGISTRATION TEST")
        
        timestamp = int(time.time())
        self.client_email = f"testclient_{timestamp}@test.com"
        client_data = {
            "username": f"testclient_{timestamp}",
            "email": self.client_email,
            "password1": "clientpass123",
            "password2": "clientpass123",
            "user_type": "client",
            "first_name": "Test",
            "last_name": "Client",
            "phone_number": "+1234567890"
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/api/auth/register/",
                json=client_data
            )
            
            if response.status_code == 201:
                self.print_success("Client registration successful")
                # Now login to get access token
                token, user_id = self.login_user(self.client_email, "clientpass123")
                self.client_token = token
                self.client_id = user_id
                self.print_info(f"Client ID: {self.client_id}")
                return True
            else:
                self.print_error(f"Client registration failed: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Client registration error: {e}")
            return False
    
    def test_trainer_registration(self):
        """Test trainer registration"""
        self.print_section("TRAINER REGISTRATION TEST")
        
        timestamp = int(time.time())
        self.trainer_email = f"testtrainer_{timestamp}@test.com"
        trainer_data = {
            "username": f"testtrainer_{timestamp}",
            "email": self.trainer_email,
            "password1": "trainerpass123",
            "password2": "trainerpass123",
            "user_type": "trainer",
            "first_name": "Test",
            "last_name": "Trainer",
            "phone_number": "+1234567891"
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/api/auth/register/",
                json=trainer_data
            )
            
            if response.status_code == 201:
                self.print_success("Trainer registration successful")
                # Now login to get access token
                token, user_id = self.login_user(self.trainer_email, "trainerpass123")
                self.trainer_token = token
                self.trainer_id = user_id
                self.print_info(f"Trainer ID: {self.trainer_id}")
                return True
            else:
                self.print_error(f"Trainer registration failed: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Trainer registration error: {e}")
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
                
                if trainers:
                    trainer = trainers[0]
                    self.print_info(f"Sample trainer: {trainer.get('first_name')} {trainer.get('last_name')}")
                    return True
                else:
                    self.print_error("No trainers available")
                    return False
            else:
                self.print_error(f"Failed to get available trainers: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error viewing trainers: {e}")
            return False
    
    def test_client_requests_trainer(self):
        """Test client requesting a trainer"""
        self.print_section("CLIENT REQUESTS TRAINER TEST")
        
        request_data = {
            "trainer_id": self.trainer_id,
            "message": "I would like to work with you for strength training"
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
                
                if requests:
                    request = requests[0]
                    self.print_info(f"Request from: {request.get('client_name')}")
                    self.print_info(f"Request ID: {request.get('request_id')}")
                    return request.get('request_id')
                else:
                    self.print_error("No pending requests found")
                    return None
            else:
                self.print_error(f"Failed to get pending requests: {response.status_code}")
                print(f"Response: {response.text}")
                return None
                
        except Exception as e:
            self.print_error(f"Error viewing pending requests: {e}")
            return None
    
    def test_trainer_approves_request(self, request_id):
        """Test trainer approving a request"""
        self.print_section("TRAINER APPROVES REQUEST TEST")
        
        approve_data = {
            "request_id": request_id,
            "action": "approve",
            "reason": "Welcome to the team!"
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
        """Run the complete client-trainer request system test"""
        self.print_section("COMPLETE CLIENT-TRAINER REQUEST SYSTEM TEST")
        
        # Step 1: Register users
        if not self.test_client_registration():
            return False
        
        if not self.test_trainer_registration():
            return False
        
        # Step 2: Client views available trainers
        if not self.test_client_views_available_trainers():
            return False
        
        # Step 3: Client requests trainer
        if not self.test_client_requests_trainer():
            return False
        
        # Step 4: Trainer views pending requests
        request_id = self.test_trainer_views_pending_requests()
        if not request_id:
            return False
        
        # Step 5: Trainer approves request
        if not self.test_trainer_approves_request(request_id):
            return False
        
        # Step 6: Client views updated status
        if not self.test_client_views_request_status():
            return False
        
        self.print_section("🎉 ALL TESTS PASSED!")
        self.print_success("Client-trainer request system is working perfectly!")
        return True

if __name__ == "__main__":
    tester = ClientTrainerRequestTester()
    tester.run_complete_test() 