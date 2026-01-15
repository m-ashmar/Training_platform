#!/usr/bin/env python3
"""
Simple test to check nutrition calculation and understand why calories are showing as 0.
"""

import requests
import json
from datetime import date

def test_nutrition_calculation():
    """Test nutrition calculation step by step"""
    base_url = "http://127.0.0.1:8000/api"
    
    print("=" * 60)
    print("TESTING NUTRITION CALCULATION")
    print("=" * 60)
    
    # Step 1: Login as trainer
    print("\n1. Logging in as trainer...")
    login_data = {
        "username": "test_trainer_comprehensive_20250713234700",
        "password": "testpass123"
    }
    
    response = requests.post(f"{base_url}/auth/token/", json=login_data)
    if response.status_code != 200:
        print(f"❌ Login failed: {response.status_code}")
        return False
    
    trainer_data = response.json()
    trainer_token = trainer_data.get("access")
    trainer_headers = {"Authorization": f"Bearer {trainer_token}"}
    print("✅ Trainer logged in")
    
    # Step 2: Get food items
    print("\n2. Getting food items...")
    response = requests.get(f"{base_url}/diet/api/food/list/", headers=trainer_headers)
    if response.status_code != 200:
        print(f"❌ Food list failed: {response.status_code}")
        return False
    
    food_data = response.json()
    food_items = food_data.get("results", [])
    print(f"✅ Found {len(food_items)} food items")
    
    # Step 3: Check first few food items
    print("\n3. Checking food item nutrition:")
    for i, food in enumerate(food_items[:5]):
        print(f"\nFood {i+1}: {food['name']}")
        print(f"  Calories: {food['calories']} per {food['serving_size']}")
        print(f"  Protein: {food['protein']}g")
        print(f"  Carbs: {food['carbs']}g")
        print(f"  Fat: {food['fat']}g")
        print(f"  Serving size grams: {food['serving_size_grams']}")
        print(f"  Calories per gram: {food['calories_per_gram']}")
        
        # Calculate nutrition for 100g
        scale_factor = 100 / food['serving_size_grams'] if food['serving_size_grams'] > 0 else 0
        calories_100g = food['calories'] * scale_factor
        print(f"  Calories for 100g: {calories_100g}")
    
    # Step 4: Check recent diet plans
    print("\n4. Checking recent diet plans...")
    response = requests.get(f"{base_url}/diet/api/trainer/diet-plans/", headers=trainer_headers)
    if response.status_code == 200:
        plans_data = response.json()
        plans = plans_data.get("results", [])
        if plans:
            latest_plan = plans[0]
            plan_id = latest_plan.get("id")
            print(f"✅ Latest plan ID: {plan_id}")
            
            # Step 5: Get plan nutrition
            print(f"\n5. Getting nutrition for plan {plan_id}...")
            response = requests.get(f"{base_url}/diet/api/nutrition/plan/{plan_id}/", headers=trainer_headers)
            if response.status_code == 200:
                nutrition_data = response.json()
                plan_nutrition = nutrition_data.get("plan_nutrition", {})
                meals = nutrition_data.get("meals", [])
                
                print(f"✅ Plan nutrition:")
                print(f"  Total calories: {plan_nutrition.get('calories', 0)}")
                print(f"  Total protein: {plan_nutrition.get('protein', 0)}g")
                print(f"  Total carbs: {plan_nutrition.get('carbs', 0)}g")
                print(f"  Total fat: {plan_nutrition.get('fat', 0)}g")
                
                print(f"\n✅ Meals ({len(meals)}):")
                for meal in meals:
                    meal_nutrition = meal.get("nutrition", {})
                    print(f"  {meal.get('meal_type')}: {meal_nutrition.get('calories', 0)} calories")
                    
                    # Check components
                    components_count = meal.get("components_count", 0)
                    completed_components = meal.get("completed_components", 0)
                    print(f"    Components: {completed_components}/{components_count} completed")
            else:
                print(f"❌ Nutrition fetch failed: {response.status_code}")
        else:
            print("❌ No diet plans found")
    else:
        print(f"❌ Diet plans fetch failed: {response.status_code}")
    
    return True

if __name__ == "__main__":
    test_nutrition_calculation() 