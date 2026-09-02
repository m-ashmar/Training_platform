#!/usr/bin/env python3
"""
test_diet_api.py - Comprehensive Diet API Testing Script

This script provides multiple testing approaches for the diet app APIs:
1. JWT Token authentication testing
2. Session-based authentication testing
3. Admin interface testing
4. Complete workflow testing

Usage:
    python test_diet_api.py --method jwt --username mu --password your_password
    python test_diet_api.py --method session --username mu --password your_password
    python test_diet_api.py --method admin
"""

import requests
import json
import argparse
import sys
from datetime import datetime

class DietAPITester:
    def __init__(self, base_url="http://127.0.0.1:8000"):
        self.base_url = base_url
        self.session = requests.Session()
        self.access_token = None
        self.refresh_token = None
        
    def print_section(self, title):
        """Print a formatted section header."""
        print(f"\n{'='*60}")
        print(f"🔍 {title}")
        print(f"{'='*60}")
    
    def print_success(self, message):
        """Print a success message."""
        print(f"✅ {message}")
    
    def print_error(self, message):
        """Print an error message."""
        print(f"❌ {message}")
    
    def print_info(self, message):
        """Print an info message."""
        print(f"ℹ️  {message}")
    
    # ============================================================================
    # JWT AUTHENTICATION METHODS
    # ============================================================================
    
    def jwt_login(self, username, password):
        """Login using JWT authentication."""
        self.print_section("JWT Authentication")
        
        url = f"{self.base_url}/api/auth/token/"
        data = {
            "username": username,
            "password": password
        }
        
        try:
            response = self.session.post(url, json=data)
            
            if response.status_code == 200:
                tokens = response.json()
                self.access_token = tokens.get('access')
                self.refresh_token = tokens.get('refresh')
                
                self.print_success(f"JWT Login successful for user: {username}")
                self.print_info(f"Access Token: {self.access_token[:50]}...")
                self.print_info(f"Refresh Token: {self.refresh_token[:50]}...")
                
                return True
            else:
                self.print_error(f"JWT Login failed: {response.status_code}")
                self.print_error(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"JWT Login error: {str(e)}")
            return False
    
    def get_jwt_headers(self):
        """Get headers with JWT token."""
        if not self.access_token:
            return {}
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
    
    def refresh_jwt_token(self):
        """Refresh JWT access token."""
        if not self.refresh_token:
            self.print_error("No refresh token available")
            return False
        
        url = f"{self.base_url}/api/auth/token/refresh/"
        data = {"refresh": self.refresh_token}
        
        try:
            response = self.session.post(url, json=data)
            
            if response.status_code == 200:
                tokens = response.json()
                self.access_token = tokens.get('access')
                self.print_success("JWT token refreshed successfully")
                return True
            else:
                self.print_error(f"Token refresh failed: {response.status_code}")
                return False
                
        except Exception as e:
            self.print_error(f"Token refresh error: {str(e)}")
            return False
    
    # ============================================================================
    # SESSION AUTHENTICATION METHODS
    # ============================================================================
    
    def session_login(self, username, password):
        """Login using session-based authentication."""
        self.print_section("Session Authentication")
        
        url = f"{self.base_url}/api/auth/login/"
        data = {
            "username": username,
            "password": password
        }
        
        try:
            response = self.session.post(url, json=data)
            
            if response.status_code == 200:
                result = response.json()
                self.print_success(f"Session Login successful for user: {username}")
                self.print_info(f"User ID: {result.get('user', {}).get('id')}")
                return True
            else:
                self.print_error(f"Session Login failed: {response.status_code}")
                self.print_error(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Session Login error: {str(e)}")
            return False
    
    # ============================================================================
    # DIET API TESTING METHODS
    # ============================================================================
    
    def test_food_search(self, query="chicken"):
        """Test food search API."""
        self.print_section(f"Food Search Test: '{query}'")
        
        url = f"{self.base_url}/diet/api/food/search/"
        params = {"q": query}
        headers = self.get_jwt_headers()
        
        try:
            response = self.session.get(url, params=params, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                self.print_success("Food search successful")
                self.print_info(f"Query: {data.get('query')}")
                self.print_info(f"Local results: {data.get('local_count')}")
                self.print_info(f"Edamam results: {data.get('edamam_count')}")
                self.print_info(f"Total results: {data.get('total_count')}")
                
                # Show first few results
                results = data.get('results', [])
                for i, food in enumerate(results[:3]):
                    self.print_info(f"Result {i+1}: {food.get('name')} ({food.get('source')})")
                
                return data
            else:
                self.print_error(f"Food search failed: {response.status_code}")
                self.print_error(f"Response: {response.text}")
                return None
                
        except Exception as e:
            self.print_error(f"Food search error: {str(e)}")
            return None
    
    def test_food_import(self, food_data):
        """Test food import API."""
        self.print_section("Food Import Test")
        
        url = f"{self.base_url}/diet/api/food/import/"
        headers = self.get_jwt_headers()
        
        try:
            response = self.session.post(url, json=food_data, headers=headers)
            
            if response.status_code == 201:
                data = response.json()
                self.print_success("Food import successful")
                self.print_info(f"Food ID: {data.get('food_id')}")
                self.print_info(f"Food Name: {data.get('food_name')}")
                self.print_info(f"Category: {data.get('category')}")
                return data
            else:
                self.print_error(f"Food import failed: {response.status_code}")
                self.print_error(f"Response: {response.text}")
                return None
                
        except Exception as e:
            self.print_error(f"Food import error: {str(e)}")
            return None
    
    def test_user_preferences(self):
        """Test user preferences API."""
        self.print_section("User Preferences Test")
        
        url = f"{self.base_url}/diet/api/preferences/"
        headers = self.get_jwt_headers()
        
        try:
            response = self.session.get(url, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                self.print_success("User preferences retrieved successfully")
                self.print_info(f"Liked foods: {len(data.get('liked_foods', []))}")
                self.print_info(f"Disliked foods: {len(data.get('disliked_foods', []))}")
                return data
            else:
                self.print_error(f"User preferences failed: {response.status_code}")
                self.print_error(f"Response: {response.text}")
                return None
                
        except Exception as e:
            self.print_error(f"User preferences error: {str(e)}")
            return None
    
    def test_add_preference(self, food_id, action="like"):
        """Test adding food to preferences."""
        self.print_section(f"Add Preference Test: {action} food {food_id}")
        
        url = f"{self.base_url}/diet/api/preferences/"
        headers = self.get_jwt_headers()
        data = {
            "food_id": food_id,
            "action": action
        }
        
        try:
            response = self.session.post(url, json=data, headers=headers)
            
            if response.status_code == 200:
                result = response.json()
                self.print_success(f"Added food {food_id} to {action}d foods")
                self.print_info(f"Message: {result.get('message')}")
                return True
            else:
                self.print_error(f"Add preference failed: {response.status_code}")
                self.print_error(f"Response: {response.text}")
                return False
                
        except Exception as e:
            self.print_error(f"Add preference error: {str(e)}")
            return False
    
    # ============================================================================
    # COMPLETE WORKFLOW TESTING
    # ============================================================================
    
    def test_complete_workflow(self):
        """Test the complete food integration workflow."""
        self.print_section("Complete Workflow Test")
        
        # Step 1: Search for foods
        search_results = self.test_food_search("salmon")
        if not search_results:
            return False
        
        # Step 2: Get user preferences
        preferences = self.test_user_preferences()
        if preferences is None:
            return False
        
        # Step 3: Try to like a local food
        results = search_results.get('results', [])
        local_foods = [f for f in results if f.get('source') == 'local' and f.get('id')]
        
        if local_foods:
            food = local_foods[0]
            self.test_add_preference(food['id'], "like")
        
        # Step 4: Try to like an Edamam food (this should trigger import)
        edamam_foods = [f for f in results if f.get('source') == 'edamam']
        
        if edamam_foods:
            food = edamam_foods[0]
            self.print_info(f"Testing Edamam food import: {food.get('name')}")
            
            # Create import data
            import_data = {
                "api_id": food.get('api_id'),
                "name": food.get('name'),
                "image_url": food.get('image_url', ''),
                "calories": food.get('calories', 0),
                "protein": food.get('protein', 0),
                "carbs": food.get('carbs', 0),
                "fat": food.get('fat', 0),
                "serving_size": food.get('serving_size', '100g'),
                "measures": food.get('measures', [])
            }
            
            # Import the food
            import_result = self.test_food_import(import_data)
            if import_result:
                # Add to preferences
                self.test_add_preference(import_result['food_id'], "like")
        
        # Step 5: Check updated preferences
        updated_preferences = self.test_user_preferences()
        
        self.print_success("Complete workflow test finished")
        return True
    
    # ============================================================================
    # ADMIN INTERFACE TESTING
    # ============================================================================
    
    def test_admin_interface(self):
        """Test admin interface accessibility."""
        self.print_section("Admin Interface Test")
        
        url = f"{self.base_url}/admin/"
        
        try:
            response = self.session.get(url)
            
            if response.status_code == 200:
                self.print_success("Admin interface is accessible")
                self.print_info("You can access admin at: http://127.0.0.1:8000/admin/")
                return True
            else:
                self.print_error(f"Admin interface failed: {response.status_code}")
                return False
                
        except Exception as e:
            self.print_error(f"Admin interface error: {str(e)}")
            return False

def main():
    parser = argparse.ArgumentParser(description="Test Diet API endpoints")
    parser.add_argument("--method", choices=["jwt", "session", "admin"], 
                       default="jwt", help="Authentication method")
    parser.add_argument("--username", default="mu", help="Username for authentication")
    parser.add_argument("--password", required=True, help="Password for authentication")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", 
                       help="Base URL for the API")
    parser.add_argument("--workflow", action="store_true", 
                       help="Run complete workflow test")
    
    args = parser.parse_args()
    
    # Create tester instance
    tester = DietAPITester(args.base_url)
    
    print(f"🚀 Diet API Testing Tool")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🌐 Base URL: {args.base_url}")
    print(f"🔐 Method: {args.method}")
    print(f"👤 Username: {args.username}")
    
    # Test based on method
    if args.method == "jwt":
        if tester.jwt_login(args.username, args.password):
            if args.workflow:
                tester.test_complete_workflow()
            else:
                tester.test_food_search()
                tester.test_user_preferences()
    
    elif args.method == "session":
        if tester.session_login(args.username, args.password):
            if args.workflow:
                tester.test_complete_workflow()
            else:
                tester.test_food_search()
                tester.test_user_preferences()
    
    elif args.method == "admin":
        tester.test_admin_interface()
    
    print(f"\n{'='*60}")
    print("🏁 Testing completed!")
    print(f"{'='*60}")

if __name__ == "__main__":
    main() 