#!/usr/bin/env python3
"""
Comprehensive Diet API Testing Script
Tests all diet endpoints to understand request/response formats
"""

import requests
import json
import time
from datetime import date, datetime

# Configuration
BASE_URL = "http://localhost:8000"
API_BASE = f"{BASE_URL}/api"

# Test users
TRAINER_EMAIL = "ll@gmail.com"
CLIENT_EMAIL = "mm@gmail.com"
PASSWORD = "testpass123"

class DietAPITester:
    def __init__(self):
        self.session = requests.Session()
        self.trainer_token = None
        self.client_token = None
        
    def login(self, email, password):
        """Login and get JWT token"""
        try:
            response = self.session.post(f"{BASE_URL}/api/auth/token/", json={
                'email': email,
                'password': password
            })
            
            if response.status_code == 200:
                data = response.json()
                return data['access']
            else:
                print(f"Login failed for {email}: {response.status_code}")
                return None
        except Exception as e:
            print(f"Login error for {email}: {e}")
            return None
    
    def setup_tokens(self):
        """Setup tokens for both trainer and client"""
        print("🔐 Setting up authentication tokens...")
        
        self.trainer_token = self.login(TRAINER_EMAIL, PASSWORD)
        self.client_token = self.login(CLIENT_EMAIL, PASSWORD)
        
        if self.trainer_token:
            print(f"✅ Trainer token obtained: {self.trainer_token[:20]}...")
        else:
            print("❌ Failed to get trainer token")
            
        if self.client_token:
            print(f"✅ Client token obtained: {self.client_token[:20]}...")
        else:
            print("❌ Failed to get client token")
    
    def make_request(self, method, endpoint, token=None, data=None, params=None):
        """Make authenticated API request"""
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        
        # Fix the URL structure for diet endpoints
        if endpoint.startswith('/diet/'):
            url = f"{BASE_URL}/api{endpoint}"
        else:
            url = f"{API_BASE}{endpoint}"
        
        print(f"DEBUG: Making {method} request to {url}")
        
        try:
            if method.upper() == 'GET':
                response = self.session.get(url, headers=headers, params=params)
            elif method.upper() == 'POST':
                response = self.session.post(url, headers=headers, json=data)
            elif method.upper() == 'PUT':
                response = self.session.put(url, headers=headers, json=data)
            elif method.upper() == 'DELETE':
                response = self.session.delete(url, headers=headers)
            
            print(f"DEBUG: Response status: {response.status_code if response else 'No response'}")
            return response
        except Exception as e:
            print(f"Request error: {e}")
            return None
    
    def test_food_endpoints(self):
        """Test food-related endpoints"""
        print("\n🍎 Testing Food Endpoints...")
        
        # Test food categories
        print("\n1. Testing Food Categories...")
        response = self.make_request('GET', '/diet/api/food/categories/', self.client_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Food categories: {len(data.get('results', []))} categories found")
            if data.get('results'):
                print(f"   Sample: {data['results'][0]}")
        else:
            print(f"❌ Food categories failed: {response.status_code if response else 'No response'}")
        
        # Test food list
        print("\n2. Testing Food List...")
        response = self.make_request('GET', '/diet/api/food/list/', self.client_token, params={'page': 1, 'page_size': 5})
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Food list: {len(data.get('results', []))} items found")
            if data.get('results'):
                print(f"   Sample: {data['results'][0]['name']}")
        else:
            print(f"❌ Food list failed: {response.status_code if response else 'No response'}")
        
        # Test food search
        print("\n3. Testing Food Search...")
        response = self.make_request('GET', '/diet/api/food/search/', self.client_token, params={'query': 'chicken'})
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Food search: {len(data.get('results', []))} results found")
        else:
            print(f"❌ Food search failed: {response.status_code if response else 'No response'}")
    
    def test_user_preferences(self):
        """Test user preferences endpoints"""
        print("\n👤 Testing User Preferences...")
        
        # Get current preferences
        response = self.make_request('GET', '/diet/api/preferences/', self.client_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Current preferences retrieved")
            print(f"   Allergies: {data.get('allergies', 'None')}")
            print(f"   Liked foods: {len(data.get('liked_foods', []))}")
            print(f"   Disliked foods: {len(data.get('disliked_foods', []))}")
        else:
            print(f"❌ Get preferences failed: {response.status_code if response else 'No response'}")
    
    def test_trainer_endpoints(self):
        """Test trainer-specific endpoints"""
        print("\n👨‍🏫 Testing Trainer Endpoints...")
        
        # Test trainer templates
        print("\n1. Testing Trainer Templates...")
        response = self.make_request('GET', '/diet/api/trainer/templates/', self.trainer_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Trainer templates: {len(data.get('results', []))} templates found")
            if data.get('results'):
                print(f"   Sample: {data['results'][0]['name']}")
        else:
            print(f"❌ Trainer templates failed: {response.status_code if response else 'No response'}")
        
        # Test trainer diet plans
        print("\n2. Testing Trainer Diet Plans...")
        response = self.make_request('GET', '/diet/api/trainer/diet-plans/', self.trainer_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Trainer diet plans: {len(data.get('results', []))} plans found")
        else:
            print(f"❌ Trainer diet plans failed: {response.status_code if response else 'No response'}")
        
        # Test creating a diet plan template
        print("\n3. Testing Create Diet Plan...")
        plan_data = {
            "client_id": 2,  # Assuming client ID
            "template_id": 1,  # Assuming template ID
            "goal": "Lose",
            "daily_calories": 1800,
            "start_date": date.today().isoformat(),
            "end_date": (date.today().replace(day=date.today().day + 7)).isoformat(),
            "meals": [
                {
                    "meal_type": "Breakfast",
                    "scheduled_time": "08:00",
                    "components": [
                        {"food_id": 1, "quantity": 100},
                        {"food_id": 2, "quantity": 50}
                    ]
                }
            ]
        }
        
        response = self.make_request('POST', '/diet/api/trainer/diet-plans/', self.trainer_token, plan_data)
        if response and response.status_code == 201:
            data = response.json()
            print(f"✅ Diet plan created: ID {data.get('id')}")
            self.created_plan_id = data.get('id')
        else:
            print(f"❌ Create diet plan failed: {response.status_code if response else 'No response'}")
            if response:
                print(f"   Error: {response.text}")
    
    def test_client_endpoints(self):
        """Test client-specific endpoints"""
        print("\n👤 Testing Client Endpoints...")
        
        # Test client progress
        print("\n1. Testing Client Progress...")
        response = self.make_request('GET', '/diet/api/client/progress/', self.client_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Client progress retrieved")
            print(f"   Current plan: {data.get('current_plan', 'None')}")
            print(f"   Today's progress: {data.get('today_progress', {})}")
        else:
            print(f"❌ Client progress failed: {response.status_code if response else 'No response'}")
        
        # Test enhanced client progress
        print("\n2. Testing Enhanced Client Progress...")
        response = self.make_request('GET', '/diet/api/client/progress/enhanced/', self.client_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Enhanced progress retrieved")
            print(f"   Daily breakdown: {len(data.get('daily_breakdown', []))} days")
        else:
            print(f"❌ Enhanced progress failed: {response.status_code if response else 'No response'}")
        
        # Test weekly progress
        print("\n3. Testing Weekly Progress...")
        response = self.make_request('GET', '/diet/api/client/progress/weekly/', self.client_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Weekly progress retrieved")
            print(f"   Week data: {len(data.get('week_data', []))} days")
        else:
            print(f"❌ Weekly progress failed: {response.status_code if response else 'No response'}")
    
    def test_meal_interaction(self):
        """Test meal interaction endpoints"""
        print("\n🍽️ Testing Meal Interaction...")
        
        # Test meal interaction
        interaction_data = {
            "meal_id": 1,  # Assuming meal ID
            "action": "like",
            "notes": "Delicious meal!"
        }
        
        response = self.make_request('POST', '/diet/api/client/meals/interact/', self.client_token, interaction_data)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Meal interaction successful")
        else:
            print(f"❌ Meal interaction failed: {response.status_code if response else 'No response'}")
    
    def test_nutrition_endpoints(self):
        """Test nutrition-related endpoints"""
        print("\n📊 Testing Nutrition Endpoints...")
        
        # Test diet plan nutrition
        plan_id = 1  # Assuming plan ID
        response = self.make_request('GET', f'/diet/api/nutrition/plan/{plan_id}/', self.client_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Diet plan nutrition retrieved")
            print(f"   Total calories: {data.get('total_nutrition', {}).get('calories', 0)}")
        else:
            print(f"❌ Diet plan nutrition failed: {response.status_code if response else 'No response'}")
        
        # Test meal components
        meal_id = 1  # Assuming meal ID
        response = self.make_request('GET', f'/diet/api/meals/{meal_id}/components/', self.client_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Meal components retrieved")
            print(f"   Components: {len(data.get('components', []))}")
        else:
            print(f"❌ Meal components failed: {response.status_code if response else 'No response'}")
    
    def test_ai_generation(self):
        """Test AI diet plan generation"""
        print("\n🤖 Testing AI Diet Plan Generation...")
        
        generation_data = {
            "goal": "Lose",
            "daily_calories": 1800,
            "duration_weeks": 2,
            "preferences": {
                "allergies": "nuts",
                "liked_foods": ["chicken", "rice"],
                "disliked_foods": ["fish"]
            }
        }
        
        response = self.make_request('POST', '/diet/v1/plans/generate/', self.client_token, generation_data)
        if response and response.status_code == 202:
            data = response.json()
            print(f"✅ AI generation started: {data.get('message', '')}")
        else:
            print(f"❌ AI generation failed: {response.status_code if response else 'No response'}")
            if response:
                print(f"   Error: {response.text}")
    
    def test_daily_advice(self):
        """Test daily advice endpoint"""
        print("\n💡 Testing Daily Advice...")
        
        response = self.make_request('GET', '/diet/v1/advice/latest/', self.client_token)
        if response and response.status_code == 200:
            data = response.json()
            print(f"✅ Daily advice retrieved")
            print(f"   Advice: {data.get('text', '')[:100]}...")
        else:
            print(f"❌ Daily advice failed: {response.status_code if response else 'No response'}")
    
    def run_all_tests(self):
        """Run all diet API tests"""
        print("🚀 Starting Comprehensive Diet API Testing")
        print("=" * 50)
        
        # Setup authentication
        self.setup_tokens()
        
        if not self.trainer_token or not self.client_token:
            print("❌ Cannot proceed without authentication tokens")
            return
        
        # Test all endpoint categories
        self.test_food_endpoints()
        self.test_user_preferences()
        self.test_trainer_endpoints()
        self.test_client_endpoints()
        self.test_meal_interaction()
        self.test_nutrition_endpoints()
        self.test_ai_generation()
        self.test_daily_advice()
        
        print("\n" + "=" * 50)
        print("✅ Diet API Testing Complete!")

if __name__ == "__main__":
    tester = DietAPITester()
    tester.run_all_tests() 