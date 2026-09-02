#!/usr/bin/env python3
"""
Real-world integration test for enhanced nutrition tracking features.
Creates complete users, subscriptions, diet plans, and tests all features.
"""

import requests
import json
import time
from datetime import date, timedelta
import random

# Configuration
BASE_URL = "http://127.0.0.1:8000"
API_BASE = f"{BASE_URL}/api"

def log_test(test_name, status, details=""):
    """Log test results with consistent formatting."""
    status_icon = "✅" if status else "❌"
    print(f"{status_icon} {test_name}")
    if details:
        print(f"   {details}")
    print()

def create_test_users():
    """Create test users with proper setup."""
    print("👥 Creating Test Users...")
    
    # Create trainer
    trainer_data = {
        "username": f"trainer_{random.randint(1000, 9999)}",
        "email": f"trainer_{random.randint(1000, 9999)}@test.com",
        "password1": "testpass123",
        "password2": "testpass123",
        "first_name": "Test",
        "last_name": "Trainer",
        "phone_number": "+1234567890",
        "user_type": "trainer"
    }
    
    response = requests.post(f"{API_BASE}/auth/register/", json=trainer_data)
    if response.status_code == 201:
        trainer = response.json()
        trainer_token = trainer.get('access')
        print(f"✅ Trainer created: {trainer_data['email']}")
    else:
        print(f"❌ Trainer creation failed: {response.status_code} - {response.text}")
        return None, None, None
    
    # Create client
    client_data = {
        "username": f"client_{random.randint(1000, 9999)}",
        "email": f"client_{random.randint(1000, 9999)}@test.com",
        "password1": "testpass123",
        "password2": "testpass123",
        "first_name": "Test",
        "last_name": "Client",
        "phone_number": "+1234567891",
        "user_type": "client"
    }
    
    response = requests.post(f"{API_BASE}/auth/register/", json=client_data)
    if response.status_code == 201:
        client = response.json()
        client_token = client.get('access')
        print(f"✅ Client created: {client_data['email']}")
    else:
        print(f"❌ Client creation failed: {response.status_code} - {response.text}")
        return None, None, None
    
    return trainer_token, client_token, trainer_data['email'], client_data['email']

def setup_subscription(trainer_token, client_email):
    """Setup subscription for the client."""
    print("💳 Setting up Subscription...")
    
    headers = {"Authorization": f"Bearer {trainer_token}"}
    
    # Get available subscription plans
    response = requests.get(f"{API_BASE}/subscription/plans/", headers=headers)
    if response.status_code != 200:
        print(f"❌ Failed to get subscription plans: {response.status_code}")
        return False
    
    plans = response.json()
    if not plans:
        print("❌ No subscription plans available")
        return False
    
    # Use the first available plan
    plan = plans[0]
    plan_id = plan['id']
    
    # Create subscription for client
    subscription_data = {
        "client_email": client_email,
        "plan_id": plan_id,
        "start_date": date.today().isoformat(),
        "end_date": (date.today() + timedelta(days=30)).isoformat(),
        "status": "active"
    }
    
    response = requests.post(f"{API_BASE}/subscription/subscriptions/", 
                           json=subscription_data, headers=headers)
    if response.status_code == 201:
        subscription = response.json()
        print(f"✅ Subscription created: {subscription['id']}")
        return True
    else:
        print(f"❌ Subscription creation failed: {response.status_code} - {response.text}")
        return False

def setup_trainer_client_relationship(trainer_token, client_email):
    """Setup trainer-client relationship."""
    print("🤝 Setting up Trainer-Client Relationship...")
    
    headers = {"Authorization": f"Bearer {trainer_token}"}
    
    relationship_data = {
        "client_email": client_email,
        "status": "approved"
    }
    
    response = requests.post(f"{API_BASE}/users/trainer-client-relationships/", 
                           json=relationship_data, headers=headers)
    if response.status_code == 201:
        relationship = response.json()
        print(f"✅ Trainer-client relationship created: {relationship['id']}")
        return True
    else:
        print(f"❌ Relationship creation failed: {response.status_code} - {response.text}")
        return False

def create_diet_plan(trainer_token, client_email):
    """Create a diet plan for the client."""
    print("🥗 Creating Diet Plan...")
    
    headers = {"Authorization": f"Bearer {trainer_token}"}
    
    # Get client's food preferences first
    response = requests.get(f"{API_BASE}/diet/api/client/preferences/", headers=headers)
    if response.status_code != 200:
        print(f"❌ Failed to get client preferences: {response.status_code}")
        return None
    
    preferences = response.json()
    
    # Create diet plan
    diet_plan_data = {
        "client_email": client_email,
        "name": "Test Diet Plan",
        "description": "Comprehensive test diet plan for nutrition tracking",
        "duration_weeks": 2,
        "calories_target": 2000,
        "protein_target": 150,
        "carbs_target": 200,
        "fat_target": 67,
        "generation_strategy": "balanced"
    }
    
    response = requests.post(f"{API_BASE}/diet/api/plans/generate/", 
                           json=diet_plan_data, headers=headers)
    if response.status_code == 201:
        diet_plan = response.json()
        print(f"✅ Diet plan created: {diet_plan['id']}")
        return diet_plan
    else:
        print(f"❌ Diet plan creation failed: {response.status_code} - {response.text}")
        return None

def test_enhanced_nutrition_features(client_token, diet_plan):
    """Test all enhanced nutrition features."""
    print("🧪 Testing Enhanced Nutrition Features...")
    
    headers = {"Authorization": f"Bearer {client_token}"}
    plan_id = diet_plan['id']
    
    # Test 1: Enhanced Diet Plan Nutrition
    print("\n📊 Testing Enhanced Diet Plan Nutrition...")
    response = requests.get(f"{API_BASE}/diet/api/nutrition/plan/{plan_id}/", headers=headers)
    if response.status_code == 200:
        nutrition_data = response.json()
        
        # Verify structure
        required_fields = ['plan_nutrition', 'nutritional_summary', 'meals']
        for field in required_fields:
            if field not in nutrition_data:
                log_test(f"Nutrition field: {field}", False, "Field missing")
                return False
        
        # Log plan nutrition
        summary = nutrition_data['nutritional_summary']
        print(f"   📈 Plan Summary:")
        print(f"      Calories: {summary['total_calories']} / {summary['calories_target']} ({summary['calories_percentage']}%)")
        print(f"      Protein: {summary['total_protein']}g / {summary['protein_target']}g ({summary['protein_percentage']}%)")
        print(f"      Carbs: {summary['total_carbs']}g / {summary['carbs_target']}g ({summary['carbs_percentage']}%)")
        print(f"      Fat: {summary['total_fat']}g / {summary['fat_target']}g ({summary['fat_percentage']}%)")
        
        # Test 2: Per-meal macros
        print(f"\n🍽️ Per-Meal Macros:")
        for meal in nutrition_data['meals']:
            meal_id = meal['id']
            meal_type = meal.get('meal_type', 'Unknown')
            macros = meal.get('nutrition', {})
            
            print(f"   {meal_type} (ID: {meal_id}):")
            print(f"      Protein: {macros.get('protein', 'N/A')}g")
            print(f"      Carbs: {macros.get('carbs', 'N/A')}g")
            print(f"      Fat: {macros.get('fat', 'N/A')}g")
            
            # Verify macros are present
            for macro in ['protein', 'carbs', 'fat']:
                if macro not in macros:
                    log_test(f"Meal {meal_id} {macro}", False, f"Missing {macro}")
                    return False
        
        log_test("Enhanced diet plan nutrition", True)
        
        # Test 3: Meal Components
        print(f"\n🥗 Meal Components:")
        for meal in nutrition_data['meals']:
            meal_id = meal['id']
            meal_type = meal.get('meal_type', 'Unknown')
            
            response = requests.get(f"{API_BASE}/diet/api/meals/{meal_id}/components/", headers=headers)
            if response.status_code == 200:
                meal_data = response.json()
                components = meal_data.get('components', [])
                
                print(f"   {meal_type} (ID: {meal_id}) - {len(components)} components:")
                for comp in components:
                    food = comp.get('food', {})
                    nutrition = comp.get('nutrition', {})
                    print(f"      - {food.get('name', 'Unknown')}: P:{nutrition.get('protein', '?')}g, C:{nutrition.get('carbs', '?')}g, F:{nutrition.get('fat', '?')}g")
                
                if len(components) == 0:
                    log_test(f"Meal {meal_id} components", False, "No components found")
                    return False
            else:
                log_test(f"Get meal {meal_id} components", False, f"Status: {response.status_code}")
                return False
        
        log_test("Meal components", True)
        
        # Test 4: Enhanced Client Progress
        print(f"\n📈 Enhanced Client Progress:")
        response = requests.get(f"{API_BASE}/diet/api/client/progress/enhanced/", headers=headers)
        if response.status_code == 200:
            progress_data = response.json()
            
            progress = progress_data.get('progress', {})
            print(f"   Daily Progress:")
            print(f"      Meals: {progress.get('meals_completed', 0)}/{progress.get('total_meals', 0)} ({progress.get('completion_percentage', 0)}%)")
            print(f"      Calories: {progress.get('calories_consumed', 0)}/{progress.get('calories_target', 0)} ({progress.get('calories_percentage', 0)}%)")
            print(f"      Protein: {progress.get('protein_consumed', 0)}g/{progress.get('protein_target', 0)}g ({progress.get('protein_percentage', 0)}%)")
            print(f"      Carbs: {progress.get('carbs_consumed', 0)}g/{progress.get('carbs_target', 0)}g ({progress.get('carbs_percentage', 0)}%)")
            print(f"      Fat: {progress.get('fat_consumed', 0)}g/{progress.get('fat_target', 0)}g ({progress.get('fat_percentage', 0)}%)")
            
            log_test("Enhanced client progress", True)
        else:
            log_test("Enhanced client progress", False, f"Status: {response.status_code}")
            return False
        
        # Test 5: Meal Completion
        print(f"\n✅ Testing Meal Completion:")
        uncompleted_meals = [meal for meal in nutrition_data['meals'] if not meal.get('is_completed', False)]
        if uncompleted_meals:
            meal_to_complete = uncompleted_meals[0]
            meal_id = meal_to_complete['id']
            
            response = requests.post(f"{API_BASE}/diet/api/client/meals/{meal_id}/complete/", headers=headers)
            if response.status_code == 200:
                completion_data = response.json()
                print(f"   ✅ Completed meal {meal_id}: {completion_data.get('message', 'Success')}")
                
                # Verify progress was updated
                time.sleep(1)
                response = requests.get(f"{API_BASE}/diet/api/client/progress/enhanced/", headers=headers)
                if response.status_code == 200:
                    updated_progress = response.json()
                    updated_meals = updated_progress.get('meals', [])
                    
                    completed_meal = next((meal for meal in updated_meals if meal['id'] == meal_id), None)
                    if completed_meal and completed_meal.get('is_completed', False):
                        log_test("Meal completion with progress update", True)
                    else:
                        log_test("Meal completion verification", False, "Meal not marked as completed")
                        return False
                else:
                    log_test("Progress verification", False, f"Status: {response.status_code}")
                    return False
            else:
                log_test("Complete meal", False, f"Status: {response.status_code}")
                return False
        else:
            print("   ℹ️ No uncompleted meals to test")
            log_test("Meal completion", True, "No meals to complete")
        
        return True
    else:
        log_test("Get diet plan nutrition", False, f"Status: {response.status_code}")
        return False

def main():
    """Run the complete real-world integration test."""
    print("🚀 Starting Real-World Integration Test")
    print("=" * 60)
    
    # Step 1: Create test users
    trainer_token, client_token, trainer_email, client_email = create_test_users()
    if not trainer_token or not client_token:
        print("❌ User creation failed. Cannot proceed.")
        return
    
    print()
    
    # Step 2: Setup subscription
    if not setup_subscription(trainer_token, client_email):
        print("❌ Subscription setup failed. Cannot proceed.")
        return
    
    print()
    
    # Step 3: Setup trainer-client relationship
    if not setup_trainer_client_relationship(trainer_token, client_email):
        print("❌ Relationship setup failed. Cannot proceed.")
        return
    
    print()
    
    # Step 4: Create diet plan
    diet_plan = create_diet_plan(trainer_token, client_email)
    if not diet_plan:
        print("❌ Diet plan creation failed. Cannot proceed.")
        return
    
    print()
    
    # Step 5: Test all enhanced nutrition features
    success = test_enhanced_nutrition_features(client_token, diet_plan)
    
    print("=" * 60)
    if success:
        print("🎉 Real-World Integration Test Completed Successfully!")
        print("\n📋 Test Summary:")
        print("   ✅ User creation and authentication")
        print("   ✅ Subscription setup with diet access")
        print("   ✅ Trainer-client relationship")
        print("   ✅ Diet plan generation")
        print("   ✅ Enhanced nutrition tracking (plan level)")
        print("   ✅ Per-meal macros (protein, carbs, fat)")
        print("   ✅ Meal components with food items")
        print("   ✅ Enhanced progress tracking")
        print("   ✅ Meal completion with progress updates")
        print("\n🔧 Test Users Created:")
        print(f"   Trainer: {trainer_email}")
        print(f"   Client: {client_email}")
        print("   Password: testpass123")
    else:
        print("❌ Real-World Integration Test Failed!")
        print("Check the logs above for specific issues.")

if __name__ == "__main__":
    main() 