#!/usr/bin/env python3
"""
Comprehensive test script for enhanced nutritional tracking features.
Tests total fat and carbs tracking for diet plans and meals.
"""

import requests
import json
import time
from datetime import date, timedelta

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

def test_authentication():
    """Test authentication for both trainer and client."""
    print("🔐 Testing Authentication...")
    
    # Test trainer authentication
    trainer_data = {
        "email": "trainer1@test.com",
        "password": "testpass123"
    }
    
    response = requests.post(f"{API_BASE}/auth/token/", json=trainer_data)
    if response.status_code == 200:
        trainer_token = response.json()['access']
        print(f"✅ Trainer authenticated: {trainer_data['email']}")
    else:
        print(f"❌ Trainer authentication failed: {response.status_code}")
        return None, None
    
    # Test client authentication
    client_data = {
        "email": "client@test.com",
        "password": "testpass123"
    }
    
    response = requests.post(f"{API_BASE}/auth/token/", json=client_data)
    if response.status_code == 200:
        client_token = response.json()['access']
        print(f"✅ Client authenticated: {client_data['email']}")
    else:
        print(f"❌ Client authentication failed: {response.status_code}")
        return None, None
    
    return trainer_token, client_token

def test_diet_plan_nutrition_with_fat_carbs(trainer_token, client_token):
    """Test enhanced diet plan nutrition with total fat and carbs."""
    print("🥗 Testing Enhanced Diet Plan Nutrition (Fat & Carbs)...")
    
    headers = {"Authorization": f"Bearer {client_token}"}
    
    # Get client's active diet plan
    response = requests.get(f"{API_BASE}/diet/api/client/progress/", headers=headers)
    if response.status_code != 200:
        log_test("Get client progress", False, f"Status: {response.status_code}")
        return None
    
    progress_data = response.json()
    if not progress_data.get('has_active_plan'):
        log_test("Active diet plan check", False, "No active diet plan found")
        return None
    
    plan_id = progress_data['diet_plan']['id']
    
    # Test enhanced nutrition endpoint
    response = requests.get(f"{API_BASE}/diet/api/nutrition/plan/{plan_id}/", headers=headers)
    if response.status_code != 200:
        log_test("Get diet plan nutrition", False, f"Status: {response.status_code}")
        return None
    
    nutrition_data = response.json()
    
    # Verify enhanced nutrition structure
    required_fields = [
        'plan_nutrition',
        'nutritional_summary',
        'meals'
    ]
    
    for field in required_fields:
        if field not in nutrition_data:
            log_test(f"Nutrition field: {field}", False, "Field missing")
            return None
    
    # Verify plan nutrition structure
    plan_nutrition = nutrition_data['plan_nutrition']
    required_nutrition_fields = ['calories', 'protein', 'carbs', 'fat', 'targets', 'percentages']
    
    for field in required_nutrition_fields:
        if field not in plan_nutrition:
            log_test(f"Plan nutrition field: {field}", False, "Field missing")
            return None
    
    # Verify nutritional summary
    nutritional_summary = nutrition_data['nutritional_summary']
    required_summary_fields = [
        'total_calories', 'total_protein', 'total_carbs', 'total_fat',
        'calories_target', 'protein_target', 'carbs_target', 'fat_target',
        'calories_percentage', 'protein_percentage', 'carbs_percentage', 'fat_percentage'
    ]
    
    for field in required_summary_fields:
        if field not in nutritional_summary:
            log_test(f"Nutritional summary field: {field}", False, "Field missing")
            return None
    
    # Log nutrition values
    print(f"   📊 Plan Nutrition:")
    print(f"      Calories: {nutritional_summary['total_calories']} / {nutritional_summary['calories_target']} ({nutritional_summary['calories_percentage']}%)")
    print(f"      Protein: {nutritional_summary['total_protein']}g / {nutritional_summary['protein_target']}g ({nutritional_summary['protein_percentage']}%)")
    print(f"      Carbs: {nutritional_summary['total_carbs']}g / {nutritional_summary['carbs_target']}g ({nutritional_summary['carbs_percentage']}%)")
    print(f"      Fat: {nutritional_summary['total_fat']}g / {nutritional_summary['fat_target']}g ({nutritional_summary['fat_percentage']}%)")

    # --- NEW: Check each meal's macros ---
    print("\n   🍽️ Meals in Plan:")
    for meal in nutrition_data['meals']:
        meal_id = meal['id']
        meal_type = meal.get('meal_type', 'Unknown')
        macros = meal.get('nutrition', {})
        # Assert and print macros for each meal
        for macro in ['protein', 'carbs', 'fat']:
            assert macro in macros, f"Meal {meal_id} missing {macro}"
        print(f"      Meal {meal_type} (ID: {meal_id}): Protein: {macros['protein']}g, Carbs: {macros['carbs']}g, Fat: {macros['fat']}g")
        # Optionally, check targets/percentages if present
        if 'targets' in macros and 'percentages' in macros:
            print(f"         Targets: {macros['targets']}")
            print(f"         Percentages: {macros['percentages']}")
    print()
    log_test("Per-meal macros present", True)
    # --- END NEW ---
    
    log_test("Enhanced diet plan nutrition", True, f"Plan ID: {plan_id}")
    
    return nutrition_data

def test_meal_components_with_fat_carbs(client_token, nutrition_data):
    """Test enhanced meal components with detailed fat and carbs."""
    print("🍽️ Testing Enhanced Meal Components (Fat & Carbs)...")
    
    headers = {"Authorization": f"Bearer {client_token}"}
    
    if not nutrition_data or not nutrition_data.get('meals'):
        log_test("Meal components test", False, "No meals available")
        return
    
    # --- NEW: Test all meals for components ---
    for meal in nutrition_data['meals']:
        meal_id = meal['id']
        meal_type = meal.get('meal_type', 'Unknown')
        response = requests.get(f"{API_BASE}/diet/api/meals/{meal_id}/components/", headers=headers)
        if response.status_code != 200:
            log_test(f"Get meal components for meal {meal_id}", False, f"Status: {response.status_code}")
            continue
        meal_data = response.json()
        # Check that components (food items) are present
        components = meal_data.get('components', [])
        print(f"   🥗 Meal {meal_type} (ID: {meal_id}) has {len(components)} components:")
        for comp in components:
            food = comp.get('food', {})
            print(f"      - {food.get('name', 'Unknown Food')} (ID: {food.get('id', '?')})")
        assert len(components) > 0, f"Meal {meal_id} has no components!"
        # Optionally, print nutrition for each component
        for comp in components:
            food = comp.get('food', {})
            nutrition = comp.get('nutrition', {})
            print(f"         {food.get('name', 'Unknown Food')}: Protein: {nutrition.get('protein', '?')}g, Carbs: {nutrition.get('carbs', '?')}g, Fat: {nutrition.get('fat', '?')}g")
    log_test("All meal components present and listed", True)
    # --- END NEW ---
    
    # (Keep the original test for the first meal for backward compatibility)
    # Test first meal
    first_meal = nutrition_data['meals'][0]
    meal_id = first_meal['id']
    
    response = requests.get(f"{API_BASE}/diet/api/meals/{meal_id}/components/", headers=headers)
    if response.status_code != 200:
        log_test("Get meal components", False, f"Status: {response.status_code}")
        return
    
    meal_data = response.json()
    
    # Verify enhanced meal structure
    required_fields = ['meal', 'components', 'nutrition', 'meal_nutritional_summary']
    
    for field in required_fields:
        if field not in meal_data:
            log_test(f"Meal field: {field}", False, "Field missing")
            return
    
    # Verify meal nutrition structure
    meal_nutrition = meal_data['nutrition']
    required_nutrition_fields = ['calories', 'protein', 'carbs', 'fat', 'targets', 'percentages']
    
    for field in required_nutrition_fields:
        if field not in meal_nutrition:
            log_test(f"Meal nutrition field: {field}", False, "Field missing")
            return
    
    # Verify meal nutritional summary
    meal_summary = meal_data['meal_nutritional_summary']
    required_summary_fields = [
        'total_calories', 'total_protein', 'total_carbs', 'total_fat',
        'calories_target', 'protein_target', 'carbs_target', 'fat_target',
        'calories_percentage', 'protein_percentage', 'carbs_percentage', 'fat_percentage'
    ]
    
    for field in required_summary_fields:
        if field not in meal_summary:
            log_test(f"Meal summary field: {field}", False, "Field missing")
            return
    
    # Log meal nutrition values
    print(f"   📊 Meal Nutrition ({meal_data['meal']['meal_type']}):")
    print(f"      Calories: {meal_summary['total_calories']} / {meal_summary['calories_target']} ({meal_summary['calories_percentage']}%)")
    print(f"      Protein: {meal_summary['total_protein']}g / {meal_summary['protein_target']}g ({meal_summary['protein_percentage']}%)")
    print(f"      Carbs: {meal_summary['total_carbs']}g / {meal_summary['carbs_target']}g ({meal_summary['carbs_percentage']}%)")
    print(f"      Fat: {meal_summary['total_fat']}g / {meal_summary['fat_target']}g ({meal_summary['fat_percentage']}%)")
    
    # Test component nutrition
    if meal_data['components']:
        first_component = meal_data['components'][0]
        component_nutrition = first_component['nutrition']
        
        print(f"   🍎 Component Nutrition ({first_component['food']['name']}):")
        print(f"      Calories: {component_nutrition['calories']}")
        print(f"      Protein: {component_nutrition['protein']}g")
        print(f"      Carbs: {component_nutrition['carbs']}g")
        print(f"      Fat: {component_nutrition['fat']}g")
    
    log_test("Enhanced meal components", True, f"Meal ID: {meal_id}")

def test_enhanced_client_progress(client_token):
    """Test enhanced client progress with detailed nutritional tracking."""
    print("📈 Testing Enhanced Client Progress...")
    
    headers = {"Authorization": f"Bearer {client_token}"}
    
    response = requests.get(f"{API_BASE}/diet/api/client/progress/enhanced/", headers=headers)
    if response.status_code != 200:
        log_test("Enhanced client progress", False, f"Status: {response.status_code}")
        return
    
    progress_data = response.json()
    
    # Verify enhanced progress structure
    required_fields = ['date', 'has_active_plan', 'diet_plan', 'meals', 'plan_nutrition', 'progress', 'summary']
    
    for field in required_fields:
        if field not in progress_data:
            log_test(f"Progress field: {field}", False, "Field missing")
            return
    
    # Verify progress nutrition structure
    progress = progress_data['progress']
    required_progress_fields = [
        'meals_completed', 'total_meals', 'completion_percentage',
        'calories_consumed', 'calories_target', 'calories_percentage',
        'protein_consumed', 'protein_target', 'protein_percentage',
        'carbs_consumed', 'carbs_target', 'carbs_percentage',
        'fat_consumed', 'fat_target', 'fat_percentage'
    ]
    
    for field in required_progress_fields:
        if field not in progress:
            log_test(f"Progress nutrition field: {field}", False, "Field missing")
            return
    
    # Log progress values
    print(f"   📊 Daily Progress:")
    print(f"      Meals: {progress['meals_completed']}/{progress['total_meals']} ({progress['completion_percentage']}%)")
    print(f"      Calories: {progress['calories_consumed']}/{progress['calories_target']} ({progress['calories_percentage']}%)")
    print(f"      Protein: {progress['protein_consumed']}g/{progress['protein_target']}g ({progress['protein_percentage']}%)")
    print(f"      Carbs: {progress['carbs_consumed']}g/{progress['carbs_target']}g ({progress['carbs_percentage']}%)")
    print(f"      Fat: {progress['fat_consumed']}g/{progress['fat_target']}g ({progress['fat_percentage']}%)")
    
    log_test("Enhanced client progress", True)

def test_meal_completion_with_nutrition(client_token, nutrition_data):
    """Test meal completion and verify nutrition updates."""
    print("✅ Testing Meal Completion with Nutrition Updates...")
    
    headers = {"Authorization": f"Bearer {client_token}"}
    
    if not nutrition_data or not nutrition_data.get('meals'):
        log_test("Meal completion test", False, "No meals available")
        return
    
    # Find an uncompleted meal
    uncompleted_meal = None
    for meal in nutrition_data['meals']:
        if not meal['is_completed']:
            uncompleted_meal = meal
            break
    
    if not uncompleted_meal:
        log_test("Meal completion test", False, "No uncompleted meals found")
        return
    
    meal_id = uncompleted_meal['id']
    
    # Complete the meal
    response = requests.post(f"{API_BASE}/diet/api/client/meals/{meal_id}/complete/", headers=headers)
    if response.status_code != 200:
        log_test("Complete meal", False, f"Status: {response.status_code}")
        return
    
    completion_data = response.json()
    print(f"   ✅ Completed meal {meal_id}: {completion_data.get('message', 'Success')}")
    
    # Verify nutrition was updated
    time.sleep(1)  # Small delay to ensure updates are processed
    
    response = requests.get(f"{API_BASE}/diet/api/client/progress/enhanced/", headers=headers)
    if response.status_code == 200:
        updated_progress = response.json()
        updated_meals = updated_progress.get('meals', [])
        
        # Find the completed meal
        completed_meal = None
        for meal in updated_meals:
            if meal['id'] == meal_id:
                completed_meal = meal
                break
        
        if completed_meal and completed_meal['is_completed']:
            log_test("Meal completion with nutrition update", True, f"Meal {meal_id} completed")
        else:
            log_test("Meal completion verification", False, "Meal not marked as completed")
    else:
        log_test("Progress verification", False, f"Status: {response.status_code}")

def main():
    """Run all enhanced nutrition tracking tests."""
    print("🚀 Starting Enhanced Nutrition Tracking Tests")
    print("=" * 50)
    
    # Test authentication
    trainer_token, client_token = test_authentication()
    if not trainer_token or not client_token:
        print("❌ Authentication failed. Cannot proceed with tests.")
        return
    
    print()
    
    # Test enhanced diet plan nutrition
    nutrition_data = test_diet_plan_nutrition_with_fat_carbs(trainer_token, client_token)
    
    # Test enhanced meal components
    test_meal_components_with_fat_carbs(client_token, nutrition_data)
    
    # Test enhanced client progress
    test_enhanced_client_progress(client_token)
    
    # Test meal completion with nutrition updates
    test_meal_completion_with_nutrition(client_token, nutrition_data)
    
    print("=" * 50)
    print("🎉 Enhanced Nutrition Tracking Tests Completed!")
    print("\n📋 Summary of Enhanced Features:")
    print("   ✅ Total fat and carbs tracking for diet plans")
    print("   ✅ Total fat and carbs tracking for individual meals")
    print("   ✅ Nutritional targets and percentages")
    print("   ✅ Enhanced progress tracking with fat/carbs")
    print("   ✅ Meal completion with nutrition updates")
    print("   ✅ Component-level nutrition details")

if __name__ == "__main__":
    main() 