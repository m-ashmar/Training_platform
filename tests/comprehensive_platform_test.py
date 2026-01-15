#!/usr/bin/env python3
"""
Comprehensive Training Platform Test Script

This script tests the complete workflow of the training platform:
1. User creation and authentication
2. Trainer-client relationship management
3. Diet plan creation and assignment
4. Training routine creation and assignment
5. Progress tracking for both diet and exercise
6. Detailed nutritional and exercise analytics

Features tested:
- JWT authentication
- User registration and login
- Trainer-client request and approval system
- Diet plan creation with detailed nutrition tracking
- Training routine creation with exercise tracking
- Meal completion and component tracking
- Exercise progress and set logging
- Weekly and daily analytics
- Total calories, proteins, carbs, fats tracking
- Exercise sets, reps, and training volume tracking
"""

import requests
import json
import time
from datetime import datetime, date, timedelta
import random

# Generate a unique suffix for this test run
UNIQUE_SUFFIX = datetime.now().strftime('%Y%m%d%H%M%S')

# Configuration
BASE_URL = "http://127.0.0.1:8000"
API_BASE = f"{BASE_URL}/api"

# Test data
TEST_DATA = {
    "trainer": {
        "username": f"test_trainer_comprehensive_{UNIQUE_SUFFIX}",
        "email": f"trainer_comprehensive_{UNIQUE_SUFFIX}@test.com",
        "password": "testpass123",
        "phone_number": "1234567890",
        "user_type": "trainer",
        "first_name": "John",
        "last_name": "Trainer",
        "trainer_bio": "Experienced fitness trainer",
        "trainer_specializations": ["Strength Training", "Cardio"],
        "trainer_experience_years": 5,
        "trainer_is_available": True,
        "is_active": True,
        "trainer_is_verified": True
    },
    "client": {
        "username": f"test_client_comprehensive_{UNIQUE_SUFFIX}",
        "email": f"client_comprehensive_{UNIQUE_SUFFIX}@test.com",
        "password": "testpass123",
        "phone_number": "0987654321",
        "user_type": "client",
        "first_name": "Jane",
        "last_name": "Client",
        "height": 165.0,
        "weight": 65.0,
        "age": 25,
        "gender": "Female",
        "activity_level": "Moderate",
        "client_goals": ["Weight Loss", "Muscle Gain"],
        "client_preferences": {"preferred_days": ["Monday", "Wednesday", "Friday"]}
    }
}

class ComprehensivePlatformTest:
    def __init__(self):
        self.session = requests.Session()
        self.trainer_token = None
        self.client_token = None
        self.trainer_id = None
        self.client_id = None
        self.diet_plan_id = None
        self.routine_id = None
        self.test_results = []
        
    def log_test(self, test_name, success, details=None):
        """Log test results"""
        result = {
            "test": test_name,
            "success": success,
            "timestamp": datetime.now().isoformat(),
            "details": details
        }
        self.test_results.append(result)
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} {test_name}")
        if details:
            print(f"   Details: {details}")
        print()

    def make_request(self, method, endpoint, data=None, headers=None, expected_status=200):
        """Make HTTP request with error handling"""
        url = f"{API_BASE}{endpoint}"
        
        if headers is None:
            headers = {}
        
        try:
            if method.upper() == "GET":
                response = self.session.get(url, headers=headers)
            elif method.upper() == "POST":
                response = self.session.post(url, json=data, headers=headers)
            elif method.upper() == "PUT":
                response = self.session.put(url, json=data, headers=headers)
            elif method.upper() == "DELETE":
                response = self.session.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            if response.status_code != expected_status:
                print(f"Request failed: {method} {url}")
                print(f"Expected status: {expected_status}, Got: {response.status_code}")
                print(f"Response: {response.text}")
                return None
                
            return response.json() if response.content else {}
            
        except Exception as e:
            print(f"Request error: {method} {url}")
            print(f"Error: {str(e)}")
            return None

    def update_trainer_profile(self):
        """Update trainer profile to ensure availability and verification"""
        print("Updating trainer profile to set available, active, and verified...")
        trainer_headers = {"Authorization": f"Bearer {self.trainer_token}"}
        profile_data = {
            "trainer_is_available": True,
            "is_active": True,
            "trainer_is_verified": True
        }
        response = self.make_request("POST", "/users/trainer/profile/", profile_data, trainer_headers)
        if response and response.get("message") == "Trainer profile updated successfully!":
            self.log_test("Trainer Profile Update", True, "Trainer is now available and verified.")
            return True
        else:
            self.log_test("Trainer Profile Update", False, f"Response: {response}")
            return False

    def test_user_registration(self):
        """Test user registration for both trainer and client"""
        print("=" * 60)
        print("TESTING USER REGISTRATION")
        print("=" * 60)
        
        # Register trainer
        trainer_data = TEST_DATA["trainer"].copy()
        # Do NOT remove user_type for trainer
        trainer_data["password1"] = trainer_data.pop("password")
        trainer_data["password2"] = trainer_data["password1"]
        
        response = self.make_request("POST", "/auth/register/", trainer_data, expected_status=201)
        if response:
            self.trainer_id = response.get("user", {}).get("id")
            self.log_test("Trainer Registration", True, f"Trainer ID: {self.trainer_id}")
        else:
            self.log_test("Trainer Registration", False)
            return False
        
        # Register client
        client_data = TEST_DATA["client"].copy()
        client_data.pop("user_type")  # Remove user_type from registration data for client
        client_data["password1"] = client_data.pop("password")
        client_data["password2"] = client_data["password1"]
        
        response = self.make_request("POST", "/auth/register/", client_data, expected_status=201)
        if response:
            self.client_id = response.get("user", {}).get("id")
            self.log_test("Client Registration", True, f"Client ID: {self.client_id}")
        else:
            self.log_test("Client Registration", False)
            return False
        
        return True

    def test_user_login(self):
        """Test user login and token generation"""
        print("=" * 60)
        print("TESTING USER LOGIN")
        print("=" * 60)
        
        # Login trainer
        trainer_login_data = {
            "email": TEST_DATA["trainer"]["email"],
            "password": TEST_DATA["trainer"]["password"]
        }
        
        response = self.make_request("POST", "/auth/token/", trainer_login_data)
        if response and "access" in response:
            self.trainer_token = response["access"]
            self.log_test("Trainer Login", True, f"Token: {self.trainer_token[:20]}...")
        else:
            self.log_test("Trainer Login", False)
            return False
        
        # Update trainer profile to ensure availability and verification
        if not self.update_trainer_profile():
            return False
        
        # Login client
        client_login_data = {
            "email": TEST_DATA["client"]["email"],
            "password": TEST_DATA["client"]["password"]
        }
        
        response = self.make_request("POST", "/auth/token/", client_login_data)
        if response and "access" in response:
            self.client_token = response["access"]
            self.log_test("Client Login", True, f"Token: {self.client_token[:20]}...")
        else:
            self.log_test("Client Login", False)
            return False
        
        return True

    def test_subscription_creation(self):
        """Create active subscriptions for both users"""
        print("=" * 60)
        print("TESTING SUBSCRIPTION CREATION")
        print("=" * 60)

        # Fetch available plans
        trainer_headers = {"Authorization": f"Bearer {self.trainer_token}"}
        plans_response = self.make_request("GET", "/subscription/v1/plans/", headers=trainer_headers)
        if not plans_response or not isinstance(plans_response, list) or not plans_response:
            self.log_test("Fetch Subscription Plans", False, "No plans found or bad response")
            return False
        # Select a plan with diet access
        plan = next((p for p in plans_response if p.get("has_diet_access")), None)
        if not plan:
            self.log_test("Fetch Subscription Plans", False, "No plan with diet access found")
            return False
        plan_id = plan.get("id")
        self.log_test("Fetch Subscription Plans", True, f"Using plan_id: {plan_id}")

        # Create subscription for trainer
        trainer_sub_data = {
            "plan_id": plan_id,
            "status": "active"
        }
        response = self.make_request("POST", "/subscription/v1/subscriptions/", trainer_sub_data, trainer_headers, expected_status=201)
        if response:
            self.log_test("Trainer Subscription Creation", True, f"Subscription ID: {response.get('id')}")
            # --- NEW: Confirm payment for trainer subscription ---
            trainer_sub = self.make_request("GET", "/subscription/v1/subscriptions/current/", headers=trainer_headers)
            trainer_sub_id = trainer_sub.get("id") if trainer_sub else None
            if trainer_sub_id:
                # Fetch payments for this subscription
                payments = self.make_request("GET", "/subscription/v1/payments/", headers=trainer_headers)
                payment_id = None
                if payments and isinstance(payments, list):
                    for p in payments:
                        if p.get("subscription") == trainer_sub_id and p.get("status") == "pending":
                            payment_id = p.get("id")
                            break
                if payment_id:
                    confirm_url = f"/subscription/v1/payments/{payment_id}/confirm/"
                    confirm_resp = self.make_request("POST", confirm_url, headers=trainer_headers, expected_status=200)
                    if confirm_resp and confirm_resp.get("message") == "Payment confirmed":
                        self.log_test("Trainer Payment Confirmation", True, f"Payment ID: {payment_id}")
                    else:
                        self.log_test("Trainer Payment Confirmation", False, f"Response: {confirm_resp}")
                        return False
                else:
                    self.log_test("Trainer Payment Fetch", False, "No pending payment found for trainer subscription")
                    return False
                # Re-fetch subscription to verify activation
                trainer_sub = self.make_request("GET", "/subscription/v1/subscriptions/current/", headers=trainer_headers)
                if not trainer_sub or trainer_sub.get("status") != "active" or not trainer_sub.get("is_active"):
                    self.log_test("Trainer Subscription Activation", False, f"Subscription: {trainer_sub}")
                    return False
            else:
                self.log_test("Trainer Subscription Activation", False, "No subscription ID found")
                return False
        else:
            self.log_test("Trainer Subscription Creation", False)
            return False

        # Create subscription for client
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        client_sub_data = {
            "plan_id": plan_id,
            "status": "active"
        }
        response = self.make_request("POST", "/subscription/v1/subscriptions/", client_sub_data, client_headers, expected_status=201)
        if response:
            self.log_test("Client Subscription Creation", True, f"Subscription ID: {response.get('id')}")
            # --- NEW: Confirm payment for client subscription ---
            client_sub = self.make_request("GET", "/subscription/v1/subscriptions/current/", headers=client_headers)
            client_sub_id = client_sub.get("id") if client_sub else None
            if client_sub_id:
                payments = self.make_request("GET", "/subscription/v1/payments/", headers=client_headers)
                payment_id = None
                if payments and isinstance(payments, list):
                    for p in payments:
                        if p.get("subscription") == client_sub_id and p.get("status") == "pending":
                            payment_id = p.get("id")
                            break
                if payment_id:
                    confirm_url = f"/subscription/v1/payments/{payment_id}/confirm/"
                    confirm_resp = self.make_request("POST", confirm_url, headers=client_headers, expected_status=200)
                    if confirm_resp and confirm_resp.get("message") == "Payment confirmed":
                        self.log_test("Client Payment Confirmation", True, f"Payment ID: {payment_id}")
                    else:
                        self.log_test("Client Payment Confirmation", False, f"Response: {confirm_resp}")
                        return False
                else:
                    self.log_test("Client Payment Fetch", False, "No pending payment found for client subscription")
                    return False
                # Re-fetch subscription to verify activation
                client_sub = self.make_request("GET", "/subscription/v1/subscriptions/current/", headers=client_headers)
                if not client_sub or client_sub.get("status") != "active" or not client_sub.get("is_active"):
                    self.log_test("Client Subscription Activation", False, f"Subscription: {client_sub}")
                    return False
            else:
                self.log_test("Client Subscription Activation", False, "No subscription ID found")
                return False
        else:
            self.log_test("Client Subscription Creation", False)
            return False

        return True

    def test_trainer_client_request(self):
        """Test client requesting trainer and trainer approving"""
        print("=" * 60)
        print("TESTING TRAINER-CLIENT RELATIONSHIP")
        print("=" * 60)
        
        # Client requests trainer
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        # Fetch available trainers
        trainers_response = self.make_request("GET", "/users/client/available-trainers/", headers=client_headers)
        if not trainers_response or not trainers_response.get("available_trainers"):
            self.log_test("Fetch Available Trainers", False, "No trainers found or bad response")
            return False
        # Find our trainer by username or email
        trainer = next((t for t in trainers_response["available_trainers"] if t["username"] == TEST_DATA["trainer"]["username"] or t["email"] == TEST_DATA["trainer"]["email"]), None)
        if not trainer:
            self.log_test("Fetch Available Trainers", False, "Test trainer not found in available trainers")
            print("Available Trainers:")
            for t in trainers_response["available_trainers"]:
                print(f"  - Username: {t['username']}, Email: {t['email']}")
            return False
        trainer_id = trainer["id"]
        self.log_test("Fetch Available Trainers", True, f"Using trainer_id: {trainer_id}")
        request_data = {"trainer_id": trainer_id}
        
        response = self.make_request("POST", "/users/client/request-trainer/", request_data, client_headers)
        request_id = None
        if response:
            # Try to get request_id from response
            request_id = response.get("request_id") or response.get("id")
            self.log_test("Client Trainer Request", True, f"Request ID: {request_id}")
        else:
            self.log_test("Client Trainer Request", False)
            return False
        
        # Trainer approves client
        trainer_headers = {"Authorization": f"Bearer {self.trainer_token}"}
        if not request_id:
            # Fallback: fetch pending requests and get the latest one for this client
            pending_resp = self.make_request("GET", "/users/trainer/pending-requests/", headers=trainer_headers)
            if pending_resp and "pending_requests" in pending_resp:
                for req in pending_resp["pending_requests"]:
                    if req["client_id"] == self.client_id:
                        request_id = req["request_id"]
                        break
        if not request_id:
            self.log_test("Trainer Client Approval", False, "No request_id found for approval")
            return False
        approve_data = {"request_id": request_id, "action": "approve"}
        response = self.make_request("POST", "/users/trainer/respond-to-request/", approve_data, trainer_headers)
        if response:
            self.log_test("Trainer Client Approval", True, f"Relation ID: {response.get('id')}")
        else:
            self.log_test("Trainer Client Approval", False)
            return False
        
        # --- NEW: Verify trainer and client have diet access and client is assigned to trainer ---
        # Fetch trainer subscription
        trainer_sub = self.make_request("GET", "/subscription/v1/subscriptions/current/", headers=trainer_headers)
        if not trainer_sub or not trainer_sub.get("is_active"):
            self.log_test("Trainer Diet Access", False, f"Trainer subscription: {trainer_sub}")
            return False
        self.log_test("Trainer Diet Access", True, "Trainer has active diet subscription.")
        # Fetch client subscription
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        client_sub = self.make_request("GET", "/subscription/v1/subscriptions/current/", headers=client_headers)
        if not client_sub or not client_sub.get("is_active"):
            self.log_test("Client Diet Access", False, f"Client subscription: {client_sub}")
            return False
        self.log_test("Client Diet Access", True, "Client has active diet subscription.")
        # Fetch client profile and check assigned_trainer
        client_profile = self.make_request("GET", "/users/client/profile/", headers=client_headers)
        assigned_trainer_id = client_profile.get("assigned_trainer")
        if assigned_trainer_id != self.trainer_id:
            self.log_test("Client Assigned Trainer", False, f"assigned_trainer: {assigned_trainer_id}, expected: {self.trainer_id}")
            return False
        self.log_test("Client Assigned Trainer", True, f"Client assigned_trainer: {assigned_trainer_id}")
        # --- END NEW ---
        
        return True

    def test_diet_plan_creation(self):
        """Test diet plan creation by trainer for client"""
        print("=" * 60)
        print("TESTING DIET PLAN CREATION")
        print("=" * 60)
        
        trainer_headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        # Fetch available templates
        templates_response = self.make_request("GET", "/diet/api/trainer/templates/", headers=trainer_headers)
        if not templates_response or not templates_response.get("results"):
            self.log_test("Fetch Diet Plan Templates", False, "No templates found or bad response")
            return False
        template = templates_response["results"][0]
        template_id = template["id"]
        self.log_test("Fetch Diet Plan Templates", True, f"Using template_id: {template_id}")
        
        # Create diet plan for client
        diet_plan_data = {
            "client_id": self.client_id,
            "template_id": template_id,
            "goal": "Lose",
            "daily_calories": 1800,
            "start_date": date.today().isoformat(),
            "duration_weeks": 1
        }
        
        response = self.make_request("POST", "/diet/api/trainer/diet-plans/", diet_plan_data, trainer_headers)
        if response:
            self.diet_plan_id = response.get("diet_plan", {}).get("id") or response.get("id")
            self.log_test("Diet Plan Creation", True, f"Plan ID: {self.diet_plan_id}")
        else:
            self.log_test("Diet Plan Creation", False)
            return False
        
        # Fetch available food items
        food_list_resp = self.make_request("GET", "/diet/api/food/list/", headers=trainer_headers)
        if not food_list_resp or not food_list_resp.get("results"):
            self.log_test("Fetch Food Items", False, "No food items found or bad response")
            return False
        food_items = food_list_resp["results"]
        if len(food_items) < 6:
            self.log_test("Fetch Food Items", False, f"Not enough food items for test (found {len(food_items)})")
            return False
        # DEBUG: Print food items used for meal creation
        print("\nDEBUG: Food items used for meal creation:")
        for i, food in enumerate(food_items[:6]):
            print(f"  {i+1}. {food['name']} (ID: {food['id']}) - Calories: {food['calories']}, Protein: {food['protein']}, Carbs: {food['carbs']}, Fat: {food['fat']}")
        # Use the first 6 food items for test meals
        meal_food_ids = [food["id"] for food in food_items[:6]]
        # Add meals to the diet plan
        meals_data = [
            {
                "meal_type": "Breakfast",
                "target_date": date.today().isoformat(),
                "food_items": [
                    {"food_id": meal_food_ids[0], "quantity": 2},
                    {"food_id": meal_food_ids[1], "quantity": 1}
                ],
                "description": "Healthy breakfast"
            },
            {
                "meal_type": "Lunch",
                "target_date": date.today().isoformat(),
                "food_items": [
                    {"food_id": meal_food_ids[2], "quantity": 1},
                    {"food_id": meal_food_ids[3], "quantity": 2}
                ],
                "description": "Balanced lunch"
            },
            {
                "meal_type": "Dinner",
                "target_date": date.today().isoformat(),
                "food_items": [
                    {"food_id": meal_food_ids[4], "quantity": 1},
                    {"food_id": meal_food_ids[5], "quantity": 1}
                ],
                "description": "Light dinner"
            }
        ]
        meal_ids = []
        for meal in meals_data:
            meal_payload = {
                "diet_plan_id": self.diet_plan_id,
                "meal_type": meal["meal_type"],
                "target_date": meal["target_date"],
                "food_items": meal["food_items"],
                "description": meal.get("description", "")
            }
            response = self.make_request("POST", "/diet/api/trainer/meals/", meal_payload, trainer_headers)
            if response and response.get("message") == "Meal added successfully":
                self.log_test(f"Meal Creation - {meal['meal_type']}", True)
                # DEBUG: Store meal ID if available
                if "meal_id" in response:
                    meal_ids.append(response["meal_id"])
            else:
                self.log_test(f"Meal Creation - {meal['meal_type']}", False)
                return False
        # DEBUG: Fetch and print meals and their components
        print("\nDEBUG: Meals created and their components:")
        nutrition_url = f"/diet/api/nutrition/plan/{self.diet_plan_id}/"
        response = self.make_request("GET", nutrition_url, headers=trainer_headers)
        if response and response.get("meals"):
            for meal in response["meals"]:
                print(f"  Meal: {meal['meal_type']} (ID: {meal['id']})")
                print(f"    Nutrition: {meal['nutrition']}")
                print(f"    Components: {meal['components_count']}")
        else:
            print("  No meals found in plan nutrition response.")
        return True

    def test_training_routine_creation(self):
        """Test training routine creation by trainer for client"""
        print("=" * 60)
        print("TESTING TRAINING ROUTINE CREATION")
        print("=" * 60)
        
        trainer_headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        # Create routine
        routine_data = {
            "name": "Strength Training Routine",
            "description": "3-day strength training program",
            "days": 3,
            "difficulty_level": "intermediate",
            "estimated_duration": 60,
            "assigned_to": [self.client_id]
        }
        
        response = self.make_request("POST", "/routine/routines/", routine_data, trainer_headers, expected_status=201)
        if response:
            self.routine_id = response.get("id")
            self.log_test("Routine Creation", True, f"Routine ID: {self.routine_id}")
        else:
            self.log_test("Routine Creation", False)
            return False
        
        # Fetch available exercises
        exercises_resp = self.make_request("GET", "/routine/exercises/", headers=trainer_headers)
        if not exercises_resp or not isinstance(exercises_resp, list) or len(exercises_resp) < 2:
            self.log_test("Fetch Exercises", False, f"Not enough exercises for test (found {len(exercises_resp) if exercises_resp else 0})")
            return False
        exercise_ids = [ex["id"] for ex in exercises_resp[:2]]
        # Add exercises to the routine for Day 1
        for idx, ex_id in enumerate(exercise_ids):
            payload = {
                "routine": self.routine_id,
                "exercise": ex_id,
                "day": 1,
                "sets": 3,
                "reps": 10,
                "rest_time": 60,
                "order": idx + 1
            }
            response = self.make_request("POST", "/routine/routineexercises/", payload, trainer_headers, expected_status=201)
            if response and response.get("id"):
                self.log_test(f"Exercise Addition - Day 1 - Exercise {idx+1}", True)
            else:
                self.log_test(f"Exercise Addition - Day 1 - Exercise {idx+1}", False)
                return False
        
        return True

    def test_diet_progress_tracking(self):
        """Test diet progress tracking and meal completion"""
        print("=" * 60)
        print("TESTING DIET PROGRESS TRACKING")
        print("=" * 60)
        
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        
        # Fetch diet plan nutrition details
        nutrition_url = f"/diet/api/nutrition/plan/{self.diet_plan_id}/"
        response = self.make_request("GET", nutrition_url, headers=client_headers)
        if response and response.get("diet_plan"):
            self.log_test("Diet Plan Nutrition Details", True)
            meals = response.get("meals", [])
        else:
            self.log_test("Diet Plan Nutrition Details", False)
            return False
        
        # Complete meal components
        if meals:
            meal_id = meals[0].get("id")
            if meal_id:
                # Complete entire meal
                completion_data = {
                    "action": "complete_meal"
                }
                response = self.make_request("POST", f"/diet/api/client/meals/{meal_id}/complete/", completion_data, client_headers)
                if response:
                    self.log_test("Meal Component Completion", True, f"Completed meal {meal_id}")
                    # DEBUG: Fetch and print nutrition after completion
                    nutrition_url = f"/diet/api/nutrition/plan/{self.diet_plan_id}/"
                    response = self.make_request("GET", nutrition_url, headers=client_headers)
                    if response and response.get("meals"):
                        for meal in response["meals"]:
                            if meal["id"] == meal_id:
                                print(f"\nDEBUG: Nutrition after meal completion for {meal['meal_type']}:")
                                print(f"  Total Calories: {meal['nutrition']['calories']}")
                                print(f"  Total Protein: {meal['nutrition']['protein']}")
                                print(f"  Total Carbs: {meal['nutrition']['carbs']}")
                                print(f"  Total Fat: {meal['nutrition']['fat']}")
                                break
                else:
                    self.log_test("Meal Component Completion", False)
                    return False
            else:
                self.log_test("Meal Component Completion", False, "No meal ID found")
                return False
        else:
            self.log_test("Meal Component Completion", False, "No meals found")
            return False
        
        return True

    def test_exercise_progress_tracking(self):
        """Test exercise progress tracking and set logging"""
        print("=" * 60)
        print("TESTING EXERCISE PROGRESS TRACKING")
        print("=" * 60)
        
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        
        # Create workout session
        session_data = {
            "routine": self.routine_id,
            "user": self.client_id,
            "start_time": "2025-07-11T10:00:00Z",
            "end_time": "2025-07-11T11:00:00Z",
            "notes": "Great workout session"
        }
        response = self.make_request("POST", "/routine/workoutsessions/", session_data, client_headers, expected_status=201)
        if response:
            session_id = response.get("id")
            self.log_test("Workout Session Creation", True, f"Session ID: {session_id}")
        else:
            self.log_test("Workout Session Creation", False)
            return False
        
        # Log exercise progress
        progress_data = [
            {
                "exercise": 1,
                "date": date.today().isoformat(),
                "completed_sets": 3,
                "target_sets": 3,
                "total_weight": 60.0,
                "total_repetitions": 36,
                "duration": "00:45:00"
            },
            {
                "exercise": 2,
                "date": date.today().isoformat(),
                "completed_sets": 3,
                "target_sets": 3,
                "total_weight": 90.0,
                "total_repetitions": 30,
                "duration": "00:50:00"
            }
        ]
        
        for progress in progress_data:
            response = self.make_request("POST", "/routine/user-exercise-progress/", progress, client_headers, expected_status=201)
            if response:
                progress_id = response.get("id")
                self.log_test(f"Exercise Progress - Exercise {progress['exercise']}", True, 
                             f"Sets: {progress['completed_sets']}, Weight: {progress['total_weight']}kg, Reps: {progress['total_repetitions']}")
                
                # Log individual sets
                set_logs = [
                    {"set_number": 1, "weight": 20.0, "reps": 12, "rest_time": 90},
                    {"set_number": 2, "weight": 20.0, "reps": 12, "rest_time": 90},
                    {"set_number": 3, "weight": 20.0, "reps": 12, "rest_time": 90}
                ]
                
                for set_log in set_logs:
                    set_log["user_exercise_progress"] = progress_id
                    set_log["workout_session"] = session_id
                    
                    set_response = self.make_request("POST", "/routine/exercisesetlogs/", set_log, client_headers)
                    if set_response:
                        self.log_test(f"Set Log - Set {set_log['set_number']}", True,
                                     f"Weight: {set_log['weight']}kg, Reps: {set_log['reps']}")
            else:
                self.log_test(f"Exercise Progress - Exercise {progress['exercise']}", False)
        
        # Complete workout session
        complete_data = {
            "status": "completed",
            "end_time": "2025-07-11T11:00:00Z",
            "user": self.client_id,
            "routine": self.routine_id
        }
        response = self.make_request("PUT", f"/routine/workoutsessions/{session_id}/", complete_data, client_headers)
        if response:
            self.log_test("Workout Session Completion", True, "Session marked as completed")
        else:
            self.log_test("Workout Session Completion", False)
            return False
        
        return True

    def test_analytics_and_reporting(self):
        """Test comprehensive analytics and reporting"""
        print("=" * 60)
        print("TESTING ANALYTICS AND REPORTING")
        print("=" * 60)
        
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        trainer_headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        # Get client progress (from client perspective)
        response = self.make_request("GET", "/diet/api/client/progress/", headers=client_headers)
        if response:
            self.log_test("Client Diet Progress", True, f"Daily progress retrieved")
            
            # Show detailed progress
            daily_progress = response.get("daily_progress", [])
            for progress in daily_progress:
                date_str = progress.get("date")
                calories = progress.get("calories_consumed")
                protein = progress.get("protein_consumed")
                carbs = progress.get("carbs_consumed")
                fat = progress.get("fat_consumed")
                completion = progress.get("completion_percentage")
                
                self.log_test(f"Daily Progress - {date_str}", True,
                             f"Cals: {calories}, P: {protein}g, C: {carbs}g, F: {fat}g, Completion: {completion}%")
        else:
            self.log_test("Client Diet Progress", False)
        
        # Get weekly progress
        response = self.make_request("GET", "/diet/api/client/progress/weekly/", headers=client_headers)
        if response:
            self.log_test("Client Weekly Progress", True, "Weekly progress retrieved")
            
            weekly_data = response.get("weekly_progress", {})
            total_calories = weekly_data.get("total_calories", 0)
            total_protein = weekly_data.get("total_protein", 0)
            total_carbs = weekly_data.get("total_carbs", 0)
            total_fat = weekly_data.get("total_fat", 0)
            
            self.log_test("Weekly Totals", True,
                         f"Cals: {total_calories}, P: {total_protein}g, C: {total_carbs}g, F: {total_fat}g")
        else:
            self.log_test("Client Weekly Progress", False)
        
        # Get exercise analytics
        response = self.make_request("GET", "/routine/analytics/summary/", headers=client_headers)
        if response:
            self.log_test("Exercise Analytics Summary", True, "Analytics summary retrieved")
            
            summary = response.get("summary", {})
            total_volume = summary.get("total_volume", 0)
            total_workouts = summary.get("total_workouts", 0)
            avg_duration = summary.get("avg_duration", 0)
            
            self.log_test("Exercise Summary", True,
                         f"Total Volume: {total_volume}kg, Workouts: {total_workouts}, Avg Duration: {avg_duration}min")
        else:
            self.log_test("Exercise Analytics Summary", False)
        
        # Get trainer's view of client progress
        # response = self.make_request("GET", f"/routine/routines/{self.routine_id}/my_clients_progress/", headers=trainer_headers)
        # if response:
        #     self.log_test("Trainer Client Progress View", True, "Trainer can view client progress")
        # else:
        #     self.log_test("Trainer Client Progress View", False)
        
        return True

    def test_detailed_nutrition_breakdown(self):
        """Test detailed nutritional breakdown for diet plans and meals"""
        print("=" * 60)
        print("TESTING DETAILED NUTRITION BREAKDOWN")
        print("=" * 60)
        
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        
        # Get detailed nutrition for the entire diet plan using the correct endpoint
        response = self.make_request("GET", f"/diet/api/nutrition/plan/{self.diet_plan_id}/", headers=client_headers)
        if response:
            self.log_test("Complete Diet Plan Nutrition", True, "Full nutrition breakdown retrieved")
            
            # Show plan totals
            plan_nutrition = response.get("plan_nutrition", {})
            total_calories = plan_nutrition.get("calories", 0)
            total_protein = plan_nutrition.get("protein", 0)
            total_carbs = plan_nutrition.get("carbs", 0)
            total_fat = plan_nutrition.get("fat", 0)
            
            self.log_test("Diet Plan Totals", True,
                         f"Total Calories: {total_calories}, Protein: {total_protein}g, Carbs: {total_carbs}g, Fat: {total_fat}g")
            
            # Show nutritional targets and percentages
            targets = plan_nutrition.get("targets", {})
            percentages = plan_nutrition.get("percentages", {})
            
            self.log_test("Nutritional Targets", True,
                         f"Calories: {targets.get('calories', 0)}, Protein: {targets.get('protein', 0)}g, Carbs: {targets.get('carbs', 0)}g, Fat: {targets.get('fat', 0)}g")
            
            self.log_test("Nutritional Percentages", True,
                         f"Calories: {percentages.get('calories', 0)}%, Protein: {percentages.get('protein', 0)}%, Carbs: {percentages.get('carbs', 0)}%, Fat: {percentages.get('fat', 0)}%")
            
            # Show meal details with components
            meals = response.get("meals", [])
            for meal in meals:
                meal_id = meal.get("id")
                meal_type = meal.get("meal_type")
                meal_nutrition = meal.get("nutrition", {})
                
                self.log_test(f"Meal Details - {meal_type}", True,
                             f"Calories: {meal_nutrition.get('calories', 0)}, Protein: {meal_nutrition.get('protein', 0)}g, Carbs: {meal_nutrition.get('carbs', 0)}g, Fat: {meal_nutrition.get('fat', 0)}g")
                
                # Get detailed meal components using the correct endpoint
                components_response = self.make_request("GET", f"/diet/api/meals/{meal_id}/components/", headers=client_headers)
                if components_response:
                    components = components_response.get("components", [])
                    
                    self.log_test(f"Meal Components - {meal_type}", True,
                                 f"Components: {len(components)}")
                    
                    for component in components:
                        food_name = component.get("food", {}).get("name", "Unknown")
                        quantity = component.get("quantity", 0)
                        nutrition = component.get("nutrition", {})
                        calories = nutrition.get("calories", 0)
                        protein = nutrition.get("protein", 0)
                        carbs = nutrition.get("carbs", 0)
                        fat = nutrition.get("fat", 0)
                        
                        self.log_test(f"  - {food_name}", True,
                                     f"Qty: {quantity}g, Cals: {calories}, P: {protein}g, C: {carbs}g, F: {fat}g")
                else:
                    self.log_test(f"Meal Components - {meal_type}", False, "Could not fetch components")
        else:
            self.log_test("Complete Diet Plan Nutrition", False)
            return False
        
        return True

    def test_exercise_detailed_tracking(self):
        """Test detailed exercise tracking and volume calculations"""
        print("=" * 60)
        print("TESTING DETAILED EXERCISE TRACKING")
        print("=" * 60)
        
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        
        # Get user exercise progress aggregation
        response = self.make_request("GET", "/routine/exercisesetlogs/my-progress/", headers=client_headers)
        if response is not None:
            self.log_test("User Exercise Progress Aggregation", True)
            if isinstance(response, list):
                for progress in response:
                    exercise = progress.get("exercise", "Unknown")
                    total_volume = progress.get("total_volume", 0)
                    sets_completed = progress.get("sets_completed", 0)
                    avg_weight = progress.get("average_weight", 0)
                    avg_reps = progress.get("average_reps", 0)
                    self.log_test(f"Progress - {exercise}", True,
                                 f"Total Volume: {total_volume}kg, Sets: {sets_completed}, Avg Weight: {avg_weight}kg, Avg Reps: {avg_reps}")
            else:
                self.log_test("User Exercise Progress Aggregation", False, "Unexpected response format")
        else:
            self.log_test("User Exercise Progress Aggregation", False)
            return False
        
        # Get all set logs for the client
        set_logs_response = self.make_request("GET", "/routine/exercisesetlogs/", headers=client_headers)
        if set_logs_response is not None:
            self.log_test("Exercise Set Logs", True, f"Total logs: {len(set_logs_response) if isinstance(set_logs_response, list) else 'unknown'}")
        else:
            self.log_test("Exercise Set Logs", False)
            return False
        
        return True

    def test_trainer_client_progress_view(self):
        """Test trainer viewing client progress and analytics"""
        print("=" * 60)
        print("TESTING TRAINER CLIENT PROGRESS VIEW")
        print("=" * 60)
        
        trainer_headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        # Test trainer viewing client progress via routine progress
        response = self.make_request("GET", "/routine/routine-progress/", headers=trainer_headers)
        if response is not None:
            self.log_test("Trainer Routine Progress View", True, f"Can see {len(response) if isinstance(response, list) else 'unknown'} progress entries")
        else:
            self.log_test("Trainer Routine Progress View", False)
        
        # Test trainer viewing client exercise progress
        response = self.make_request("GET", "/routine/user-exercise-progress/", headers=trainer_headers)
        if response is not None:
            self.log_test("Trainer Exercise Progress View", True, f"Can see {len(response) if isinstance(response, list) else 'unknown'} exercise progress entries")
        else:
            self.log_test("Trainer Exercise Progress View", False)
        
        # Test trainer viewing client set logs
        response = self.make_request("GET", "/routine/exercisesetlogs/", headers=trainer_headers)
        if response is not None:
            self.log_test("Trainer Set Logs View", True, f"Can see {len(response) if isinstance(response, list) else 'unknown'} set logs")
        else:
            self.log_test("Trainer Set Logs View", False)
        
        # Test trainer analytics dashboard
        response = self.make_request("GET", "/routine/analytics/admin_dashboard/", headers=trainer_headers)
        if response is not None:
            self.log_test("Trainer Analytics Dashboard", True, "Trainer can view client analytics dashboard")
        else:
            self.log_test("Trainer Analytics Dashboard", False)
        
        # Test trainer viewing client profile
        response = self.make_request("GET", f"/users/client-profiles/{self.client_id}/", headers=trainer_headers)
        if response is not None:
            self.log_test("Trainer Client Profile View", True, "Trainer can view client profile")
            client_data = response
            bmi = client_data.get("bmi")
            bmr = client_data.get("bmr")
            tdee = client_data.get("tdee")
            self.log_test("Client Metrics", True, f"BMI: {bmi}, BMR: {bmr}, TDEE: {tdee}")
        else:
            self.log_test("Trainer Client Profile View", False)
        
        return True

    def test_volume_tracking_apis(self):
        """Test volume tracking APIs for total volume, sets, and reps"""
        print("=" * 60)
        print("TESTING VOLUME TRACKING APIS")
        print("=" * 60)
        
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        trainer_headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        # Test client viewing their own volume analytics
        response = self.make_request("GET", "/routine/analytics/summary/", headers=client_headers)
        if response is not None:
            self.log_test("Client Volume Analytics", True, "Client can view volume analytics")
            summary = response.get("summary", {})
            total_volume = summary.get("total_volume", 0)
            total_workouts = summary.get("total_workouts", 0)
            avg_duration = summary.get("avg_duration", 0)
            self.log_test("Volume Summary", True, f"Total Volume: {total_volume}kg, Workouts: {total_workouts}, Avg Duration: {avg_duration}min")
        else:
            self.log_test("Client Volume Analytics", False)
        
        # Test volume trends
        response = self.make_request("GET", "/routine/analytics/trends/?period=week", headers=client_headers)
        if response is not None:
            self.log_test("Volume Trends", True, "Client can view volume trends")
            trends = response
            volume_trend = trends.get("volume_trend", [])
            completion_trend = trends.get("completion_trend", [])
            self.log_test("Trends Data", True, f"Volume trend points: {len(volume_trend)}, Completion trend points: {len(completion_trend)}")
        else:
            self.log_test("Volume Trends", False)
        
        # Test trainer viewing client volume analytics
        response = self.make_request("GET", f"/routine/analytics/summary/?user_id={self.client_id}", headers=trainer_headers)
        if response is not None:
            self.log_test("Trainer Client Volume Analytics", True, "Trainer can view client volume analytics")
            summary = response.get("summary", {})
            total_volume = summary.get("total_volume", 0)
            total_workouts = summary.get("total_workouts", 0)
            self.log_test("Trainer Client Summary", True, f"Client Total Volume: {total_volume}kg, Workouts: {total_workouts}")
        else:
            self.log_test("Trainer Client Volume Analytics", False)
        
        # Test trainer viewing client volume trends
        response = self.make_request("GET", f"/routine/analytics/trends/?user_id={self.client_id}&period=week", headers=trainer_headers)
        if response is not None:
            self.log_test("Trainer Client Volume Trends", True, "Trainer can view client volume trends")
        else:
            self.log_test("Trainer Client Volume Trends", False)
        
        # Test exercise-specific volume aggregation
        response = self.make_request("GET", "/routine/exercisesetlogs/my-progress/?group_by=exercise", headers=client_headers)
        if response is not None:
            self.log_test("Exercise Volume Aggregation", True, "Client can view exercise-specific volume")
            if isinstance(response, list):
                for exercise_data in response:
                    exercise_name = exercise_data.get("exercise", "Unknown")
                    total_volume = exercise_data.get("total_volume", 0)
                    sets_completed = exercise_data.get("sets_completed", 0)
                    avg_weight = exercise_data.get("average_weight", 0)
                    avg_reps = exercise_data.get("average_reps", 0)
                    self.log_test(f"Exercise - {exercise_name}", True,
                                 f"Volume: {total_volume}kg, Sets: {sets_completed}, Avg: {avg_weight}kg x {avg_reps} reps")
        else:
            self.log_test("Exercise Volume Aggregation", False)
        
        return True

    def test_client_daily_progress(self):
        """Test client viewing their daily progress"""
        print("=" * 60)
        print("TESTING CLIENT DAILY PROGRESS")
        print("=" * 60)
        
        client_headers = {"Authorization": f"Bearer {self.client_token}"}
        
        # Test enhanced daily progress (detailed meal and nutrition info)
        response = self.make_request("GET", "/diet/api/client/enhanced-progress/", headers=client_headers)
        if response is not None:
            self.log_test("Enhanced Daily Progress", True, "Client can view enhanced daily progress")
            progress_data = response
            has_active_plan = progress_data.get("has_active_plan", False)
            meals_completed = progress_data.get("meals_completed", 0)
            total_meals = progress_data.get("total_meals", 0)
            completion_percentage = progress_data.get("completion_percentage", 0)
            calories_consumed = progress_data.get("calories_consumed", 0)
            target_calories = progress_data.get("target_calories", 0)
            
            self.log_test("Daily Progress Summary", True,
                         f"Active Plan: {has_active_plan}, Meals: {meals_completed}/{total_meals} ({completion_percentage}%), Calories: {calories_consumed}/{target_calories}")
        else:
            self.log_test("Enhanced Daily Progress", False)
        
        # Test regular daily progress
        response = self.make_request("GET", "/diet/api/client/progress/", headers=client_headers)
        if response is not None:
            self.log_test("Regular Daily Progress", True, "Client can view regular daily progress")
        else:
            self.log_test("Regular Daily Progress", False)
        
        # Test weekly progress
        response = self.make_request("GET", "/diet/api/client/progress/weekly/", headers=client_headers)
        if response is not None:
            self.log_test("Weekly Progress", True, "Client can view weekly progress")
            weekly_data = response.get("weekly_progress", {})
            total_calories = weekly_data.get("total_calories", 0)
            total_protein = weekly_data.get("total_protein", 0)
            total_carbs = weekly_data.get("total_carbs", 0)
            total_fat = weekly_data.get("total_fat", 0)
            
            self.log_test("Weekly Totals", True,
                         f"Cals: {total_calories}, P: {total_protein}g, C: {total_carbs}g, F: {total_fat}g")
        else:
            self.log_test("Weekly Progress", False)
        
        return True

    def run_comprehensive_test(self):
        """Run all comprehensive tests"""
        print("🚀 STARTING COMPREHENSIVE PLATFORM TEST")
        print("=" * 80)
        
        test_functions = [
            self.test_user_registration,
            self.test_user_login,
            self.test_subscription_creation,
            self.test_trainer_client_request,
            self.test_diet_plan_creation,
            self.test_training_routine_creation,
            self.test_diet_progress_tracking,
            self.test_exercise_progress_tracking,
            self.test_analytics_and_reporting,
            self.test_detailed_nutrition_breakdown,
            self.test_exercise_detailed_tracking,
            self.test_trainer_client_progress_view,
            self.test_volume_tracking_apis,
            self.test_client_daily_progress
        ]
        
        all_passed = True
        
        for test_func in test_functions:
            try:
                if not test_func():
                    all_passed = False
                    break
            except Exception as e:
                self.log_test(test_func.__name__, False, f"Exception: {str(e)}")
                all_passed = False
                break
        
        # Print summary
        print("=" * 80)
        print("📊 TEST SUMMARY")
        print("=" * 80)
        
        passed_tests = sum(1 for result in self.test_results if result["success"])
        total_tests = len(self.test_results)
        
        print(f"Total Tests: {total_tests}")
        print(f"Passed: {passed_tests}")
        print(f"Failed: {total_tests - passed_tests}")
        print(f"Success Rate: {(passed_tests/total_tests)*100:.1f}%")
        
        if all_passed:
            print("\n🎉 ALL TESTS PASSED! The platform is working correctly.")
        else:
            print("\n❌ SOME TESTS FAILED. Please check the details above.")
        
        # Print detailed results
        print("\n📋 DETAILED RESULTS:")
        for result in self.test_results:
            status = "✅ PASS" if result["success"] else "❌ FAIL"
            print(f"{status} {result['test']}")
            if result["details"]:
                print(f"   Details: {result['details']}")
        
        return all_passed

if __name__ == "__main__":
    # Run the comprehensive test
    tester = ComprehensivePlatformTest()
    success = tester.run_comprehensive_test()
    
    if success:
        print("\n🎯 COMPREHENSIVE TEST COMPLETED SUCCESSFULLY!")
        print("The platform supports all requested features:")
        print("✅ User creation and authentication")
        print("✅ Trainer-client relationship management")
        print("✅ Diet plan creation with detailed nutrition tracking")
        print("✅ Training routine creation with exercise tracking")
        print("✅ Meal completion and component tracking")
        print("✅ Exercise progress and set logging")
        print("✅ Weekly and daily analytics")
        print("✅ Total calories, proteins, carbs, fats tracking")
        print("✅ Exercise sets, reps, and training volume tracking")
    else:
        print("\n⚠️  SOME TESTS FAILED. Please review the errors above.")
    
    exit(0 if success else 1) 