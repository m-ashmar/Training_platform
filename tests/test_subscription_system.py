#!/usr/bin/env python3
import requests
import json
import time

# Test the complete subscription system
base_url = "http://127.0.0.1:8000"

class SubscriptionSystemTester:
    def __init__(self):
        self.base_url = "http://127.0.0.1:8000"
        self.session = requests.Session()
        self.user_token = None
        self.user_id = None
        self.user_email = None
        
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
    
    def test_user_registration_and_login(self):
        """Test user registration and login"""
        self.print_section("USER REGISTRATION AND LOGIN TEST")
        
        timestamp = int(time.time())
        self.user_email = f"testuser_{timestamp}@test.com"
        
        user_data = {
            "username": f"testuser_{timestamp}",
            "email": self.user_email,
            "password1": "userpass123",
            "password2": "userpass123",
            "user_type": "client",
            "first_name": "Test",
            "last_name": "User",
            "phone_number": "+1234567890"
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/api/auth/register/",
                json=user_data
            )
            
            if response.status_code == 201:
                self.print_success("User registration successful")
                
                # Login to get access token
                token, user_id = self.login_user(self.user_email, "userpass123")
                self.user_token = token
                self.user_id = user_id
                self.print_info(f"User ID: {self.user_id}")
                return True
            else:
                self.print_error(f"User registration failed: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"User registration error: {e}")
            return False
    
    def test_get_available_plans(self):
        """Test getting available subscription plans"""
        self.print_section("GET AVAILABLE SUBSCRIPTION PLANS TEST")
        
        try:
            response = self.session.get(
                f"{self.base_url}/api/subscription/v1/plans/",
                headers=self.get_auth_headers(self.user_token)
            )
            
            if response.status_code == 200:
                self.print_success("Successfully retrieved subscription plans")
                data = response.json()
                
                # Handle different response formats
                if isinstance(data, list):
                    plans = data
                elif isinstance(data, dict) and 'results' in data:
                    plans = data.get('results', [])
                else:
                    plans = []
                
                self.print_info(f"Found {len(plans)} subscription plans")
                
                for plan in plans:
                    self.print_info(f"Plan: {plan.get('name')} - ${plan.get('price')} - {plan.get('plan_type')}")
                
                if plans:
                    self.selected_plan = plans[0]  # Use first plan for testing
                    self.print_info(f"Selected plan for testing: {self.selected_plan.get('name')}")
                    return True
                else:
                    self.print_error("No subscription plans available")
                    return False
            else:
                self.print_error(f"Failed to get plans: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error getting plans: {e}")
            return False
    
    def test_check_subscription_access(self):
        """Test checking subscription access before subscription"""
        self.print_section("CHECK SUBSCRIPTION ACCESS (BEFORE SUBSCRIPTION) TEST")
        
        try:
            response = self.session.get(
                f"{self.base_url}/api/subscription/v1/access/check/",
                headers=self.get_auth_headers(self.user_token)
            )
            
            if response.status_code == 200:
                self.print_success("Successfully checked subscription access")
                data = response.json()
                self.print_info(f"Has diet access: {data.get('has_diet_access')}")
                self.print_info(f"Has routine access: {data.get('has_routine_access')}")
                self.print_info(f"Has challenges access: {data.get('has_challenges_access')}")
                self.print_info(f"Has AI advice: {data.get('has_ai_advice')}")
                self.print_info(f"Subscription status: {data.get('subscription_status')}")
                return True
            else:
                self.print_error(f"Failed to check access: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error checking access: {e}")
            return False
    
    def test_create_subscription(self):
        """Test creating a subscription"""
        self.print_section("CREATE SUBSCRIPTION TEST")
        
        if not hasattr(self, 'selected_plan'):
            self.print_error("No plan selected for subscription")
            return False
        
        subscription_data = {
            "plan": self.selected_plan.get('id'),
            "auto_renew": True
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/api/subscription/v1/subscriptions/",
                json=subscription_data,
                headers=self.get_auth_headers(self.user_token)
            )
            
            if response.status_code == 201:
                self.print_success("Subscription created successfully")
                data = response.json()
                self.subscription_id = data.get('id')
                self.print_info(f"Subscription ID: {self.subscription_id}")
                self.print_info(f"Plan: {data.get('plan', {}).get('name')}")
                self.print_info(f"Status: {data.get('status')}")
                self.print_info(f"Start date: {data.get('start_date')}")
                self.print_info(f"End date: {data.get('end_date')}")
                return True
            else:
                self.print_error(f"Failed to create subscription: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error creating subscription: {e}")
            return False
    
    def test_get_current_subscription(self):
        """Test getting current user subscription"""
        self.print_section("GET CURRENT SUBSCRIPTION TEST")
        
        try:
            response = self.session.get(
                f"{self.base_url}/api/subscription/v1/subscriptions/current/",
                headers=self.get_auth_headers(self.user_token)
            )
            
            if response.status_code == 200:
                self.print_success("Successfully retrieved current subscription")
                data = response.json()
                self.print_info(f"Subscription ID: {data.get('id')}")
                self.print_info(f"Plan: {data.get('plan', {}).get('name')}")
                self.print_info(f"Status: {data.get('status')}")
                self.print_info(f"Auto renew: {data.get('auto_renew')}")
                return True
            else:
                self.print_error(f"Failed to get current subscription: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error getting current subscription: {e}")
            return False
    
    def test_check_subscription_access_after(self):
        """Test checking subscription access after subscription"""
        self.print_section("CHECK SUBSCRIPTION ACCESS (AFTER SUBSCRIPTION) TEST")
        
        try:
            response = self.session.get(
                f"{self.base_url}/api/subscription/v1/access/check/",
                headers=self.get_auth_headers(self.user_token)
            )
            
            if response.status_code == 200:
                self.print_success("Successfully checked subscription access after subscription")
                data = response.json()
                self.print_info(f"Has diet access: {data.get('has_diet_access')}")
                self.print_info(f"Has routine access: {data.get('has_routine_access')}")
                self.print_info(f"Has challenges access: {data.get('has_challenges_access')}")
                self.print_info(f"Has AI advice: {data.get('has_ai_advice')}")
                self.print_info(f"Subscription status: {data.get('subscription_status')}")
                self.print_info(f"Days remaining: {data.get('days_remaining')}")
                return True
            else:
                self.print_error(f"Failed to check access: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error checking access: {e}")
            return False
    
    def test_get_payment_history(self):
        """Test getting payment history"""
        self.print_section("GET PAYMENT HISTORY TEST")
        
        try:
            response = self.session.get(
                f"{self.base_url}/api/subscription/v1/payments/",
                headers=self.get_auth_headers(self.user_token)
            )
            
            if response.status_code == 200:
                self.print_success("Successfully retrieved payment history")
                data = response.json()
                payments = data.get('results', [])
                self.print_info(f"Found {len(payments)} payments")
                
                for payment in payments:
                    self.print_info(f"Payment: ${payment.get('amount')} - {payment.get('status')} - {payment.get('created_at')}")
                return True
            else:
                self.print_error(f"Failed to get payments: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error getting payments: {e}")
            return False
    
    def test_cancel_subscription(self):
        """Test canceling subscription (set auto_renew to false)"""
        self.print_section("CANCEL SUBSCRIPTION TEST")
        
        if not hasattr(self, 'subscription_id'):
            self.print_error("No subscription to cancel")
            return False
        
        cancel_data = {
            "auto_renew": False
        }
        
        try:
            response = self.session.patch(
                f"{self.base_url}/api/subscription/v1/subscriptions/{self.subscription_id}/",
                json=cancel_data,
                headers=self.get_auth_headers(self.user_token)
            )
            
            if response.status_code == 200:
                self.print_success("Subscription canceled successfully")
                data = response.json()
                self.print_info(f"Auto renew: {data.get('auto_renew')}")
                return True
            else:
                self.print_error(f"Failed to cancel subscription: {response.status_code}")
                print(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Error canceling subscription: {e}")
            return False
    
    def run_complete_test(self):
        """Run the complete subscription system test"""
        self.print_section("COMPLETE SUBSCRIPTION SYSTEM TEST")
        
        # Step 1: Register and login user
        if not self.test_user_registration_and_login():
            return False
        
        # Step 2: Get available plans
        if not self.test_get_available_plans():
            return False
        
        # Step 3: Check access before subscription
        if not self.test_check_subscription_access():
            return False
        
        # Step 4: Create subscription
        if not self.test_create_subscription():
            return False
        
        # Step 5: Get current subscription
        if not self.test_get_current_subscription():
            return False
        
        # Step 6: Check access after subscription
        if not self.test_check_subscription_access_after():
            return False
        
        # Step 7: Get payment history
        if not self.test_get_payment_history():
            return False
        
        # Step 8: Cancel subscription
        if not self.test_cancel_subscription():
            return False
        
        self.print_section("🎉 ALL SUBSCRIPTION TESTS PASSED!")
        self.print_success("Subscription system is working perfectly!")
        return True

if __name__ == "__main__":
    tester = SubscriptionSystemTester()
    tester.run_complete_test() 