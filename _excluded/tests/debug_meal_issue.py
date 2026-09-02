#!/usr/bin/env python3
"""
Debug script to identify the exact issue with meal creation and nutrition endpoint.
"""

import requests
import json
from datetime import date

def debug_meal_issue():
    """Debug the meal creation and nutrition endpoint issue"""
    base_url = "http://127.0.0.1:8000/api"
    
    print("=" * 60)
    print("DEBUGGING MEAL CREATION AND NUTRITION ISSUE")
    print("=" * 60)
    
    # Step 1: Register trainer and client
    print("\n1. Registering users...")
    
    # Register trainer
    trainer_data = {
        "username": "debug_trainer",
        "email": "debug_trainer@test.com",
        "password1": "testpass123",
        "password2": "testpass123",
        "phone_number": "+1234567890",
        "user_type": "trainer"
    }
    response = requests.post(f"{base_url}/auth/register/", json=trainer_data)
    if response.status_code not in (201, 400):
        print(f"❌ Trainer registration failed: {response.status_code}")
        return False
    
    # Register client
    client_data = {
        "username": "debug_client",
        "email": "debug_client@test.com",
        "password1": "testpass123",
        "password2": "testpass123",
        "phone_number": "+1234567891",
        "user_type": "client"
    }
    response = requests.post(f"{base_url}/auth/register/", json=client_data)
    if response.status_code not in (201, 400):
        print(f"❌ Client registration failed: {response.status_code}")
        return False
    print("✅ Users registered (or already exist)")
    
    # Step 2: Login as trainer
    print("\n2. Logging in as trainer...")
    login_data = {
        "username": "debug_trainer",
        "email": "debug_trainer@test.com",
        "password": "testpass123"
    }
    response = requests.post(f"{base_url}/auth/token/", json=login_data)
    if response.status_code != 200:
        print(f"❌ Trainer login failed: {response.status_code}")
        return False
    trainer_login = response.json()
    trainer_token = trainer_login.get("access")
    trainer_headers = {"Authorization": f"Bearer {trainer_token}"}
    print("✅ Trainer logged in")
    
    # Step 3: Update trainer profile
    print("\n3. Updating trainer profile...")
    profile_data = {
        "trainer_bio": "Debug trainer",
        "trainer_specializations": "Nutrition",
        "trainer_experience_years": 5,
        "trainer_hourly_rate": 50.0
    }
    response = requests.post(f"{base_url}/users/trainer/profile/", json=profile_data, headers=trainer_headers)
    if response.status_code not in (200, 400):
        print(f"❌ Profile update failed: {response.status_code}")
        return False
    print("✅ Trainer profile updated (or already set)")
    
    # Step 4: Create subscription for trainer
    print("\n4. Creating trainer subscription...")
    response = requests.post(f"{base_url}/subscription/v1/subscriptions/", json={"plan_id": 3}, headers=trainer_headers)
    # Confirm payment
    response = requests.get(f"{base_url}/subscription/v1/payments/", headers=trainer_headers)
    if response.status_code == 200:
        payments = response.json()
        if payments:
            payment_id = payments[0]["id"]
            requests.post(f"{base_url}/subscription/v1/payments/{payment_id}/confirm/", headers=trainer_headers)
    print("✅ Trainer subscription activated (or already active)")
    
    # Step 5: Login as client
    print("\n5. Logging in as client...")
    login_data = {
        "username": "debug_client",
        "email": "debug_client@test.com",
        "password": "testpass123"
    }
    response = requests.post(f"{base_url}/auth/token/", json=login_data)
    if response.status_code != 200:
        print(f"❌ Client login failed: {response.status_code}")
        return False
    client_login = response.json()
    client_token = client_login.get("access")
    client_headers = {"Authorization": f"Bearer {client_token}"}
    print("✅ Client logged in")
    
    # Step 6: Create subscription for client
    print("\n6. Creating client subscription...")
    response = requests.post(f"{base_url}/subscription/v1/subscriptions/", json={"plan_id": 3}, headers=client_headers)
    # Confirm payment
    response = requests.get(f"{base_url}/subscription/v1/payments/", headers=client_headers)
    if response.status_code == 200:
        payments = response.json()
        if payments:
            payment_id = payments[0]["id"]
            requests.post(f"{base_url}/subscription/v1/payments/{payment_id}/confirm/", headers=client_headers)
    print("✅ Client subscription activated (or already active)")
    
    # Step 7: Get client and trainer IDs
    print("\n7. Getting client and trainer IDs...")
    response = requests.get(f"{base_url}/users/client/profile/", headers=client_headers)
    if response.status_code == 200:
        client_profile = response.json()
        client_id = client_profile.get("id")
        print(f"✅ Client ID: {client_id}")
    else:
        print(f"❌ Failed to get client profile: {response.status_code}")
        return False
    response = requests.get(f"{base_url}/users/trainer/profile/", headers=trainer_headers)
    if response.status_code == 200:
        trainer_profile = response.json()
        trainer_id = trainer_profile.get("id")
        print(f"✅ Trainer ID: {trainer_id}")
    else:
        print(f"❌ Failed to get trainer profile: {response.status_code}")
        return False
    
    # Step 7.5: Establish trainer-client relationship
    print("\n7.5. Establishing trainer-client relationship...")
    response = requests.post(f"{base_url}/users/client/request-trainer/", json={"trainer_id": trainer_id}, headers=client_headers)
    if response.status_code not in (200, 201, 400):
        print(f"❌ Client request failed: {response.status_code}")
        return False
    # Trainer approves client
    response = requests.get(f"{base_url}/users/trainer/pending-requests/", headers=trainer_headers)
    if response.status_code == 200:
        requests_data = response.json()
        print(f"DEBUG: Pending requests raw: {requests_data}")
        # Use 'pending_requests' key if present
        if isinstance(requests_data, dict) and "pending_requests" in requests_data:
            pending = requests_data["pending_requests"]
        elif isinstance(requests_data, dict) and "results" in requests_data:
            pending = requests_data["results"]
        elif isinstance(requests_data, list):
            pending = requests_data
        else:
            pending = []
        if pending and isinstance(pending, list):
            request_id = pending[0].get("request_id") or pending[0].get("id")
            if request_id:
                response = requests.post(f"{base_url}/users/trainer/respond-to-request/", json={"request_id": request_id, "action": "approve"}, headers=trainer_headers)
                if response.status_code in (200, 201):
                    print("✅ Trainer-client relationship established")
                else:
                    print(f"❌ Trainer approval failed: {response.status_code}")
                    return False
            else:
                print("No valid request ID found in pending requests.")
        else:
            print("No pending requests to approve (may already be approved)")
    else:
        print(f"❌ Failed to get pending requests: {response.status_code}")
        return False
    
    # Step 8: Get a valid template ID
    print("\n8. Getting a valid template ID...")
    response = requests.get(f"{base_url}/diet/api/trainer/templates/", headers=trainer_headers)
    if response.status_code == 200:
        templates = response.json().get("results", [])
        if not templates:
            print("❌ No templates found!")
            return False
        template_id = templates[0]["id"]
        print(f"✅ Using template ID: {template_id}")
    else:
        print(f"❌ Failed to get templates: {response.status_code}")
        return False
    
    # Step 9: Create diet plan
    print("\n9. Creating diet plan...")
    plan_data = {
        "client_id": client_id,
        "template_id": template_id,
        "goal": "Lose",
        "daily_calories": 1800,
        "start_date": date.today().isoformat(),
        "duration_weeks": 1
    }
    response = requests.post(f"{base_url}/diet/api/trainer/diet-plans/", json=plan_data, headers=trainer_headers)
    if response.status_code not in (200, 201):
        print(f"❌ Diet plan creation failed: {response.status_code}")
        print(f"Response: {response.text}")
        return False
    plan_response = response.json()
    plan_id = plan_response.get("diet_plan", {}).get("id") or plan_response.get("id")
    print(f"✅ Diet plan created with ID: {plan_id}")
    
    # Step 10: Get food items
    print("\n10. Getting food items...")
    response = requests.get(f"{base_url}/diet/api/food/list/", headers=trainer_headers)
    if response.status_code != 200:
        print(f"❌ Food list failed: {response.status_code}")
        return False
    food_data = response.json()
    food_items = food_data.get("results", [])
    if len(food_items) < 2:
        print("❌ Not enough food items")
        return False
    print(f"✅ Found {len(food_items)} food items")
    
    # Step 11: Create meal for today
    print("\n11. Creating meal for today...")
    today = date.today()
    meal_data = {
        "diet_plan_id": plan_id,
        "meal_type": "Breakfast",
        "target_date": today.isoformat(),
        "food_items": [
            {"food_id": food_items[0]["id"], "quantity": 200},
            {"food_id": food_items[1]["id"], "quantity": 150}
        ],
        "description": "Debug breakfast"
    }
    response = requests.post(f"{base_url}/diet/api/trainer/meals/", json=meal_data, headers=trainer_headers)
    if response.status_code not in (200, 201):
        print(f"❌ Meal creation failed: {response.status_code}")
        print(f"Response: {response.text}")
        return False
    meal_response = response.json()
    print(f"✅ Meal created: {meal_response.get('message')}")
    
    # Step 12: Test nutrition endpoint as TRAINER
    print(f"\n12. Testing nutrition endpoint as TRAINER...")
    response = requests.get(f"{base_url}/diet/api/nutrition/plan/{plan_id}/", headers=trainer_headers)
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        nutrition_data = response.json()
        meals = nutrition_data.get("meals", [])
        print(f"✅ Trainer can see {len(meals)} meals")
        for meal in meals:
            print(f"  - {meal['meal_type']}: {meal['nutrition']['calories']} calories")
    else:
        print(f"❌ Trainer nutrition failed: {response.text}")
    
    # Step 13: Test nutrition endpoint as CLIENT
    print(f"\n13. Testing nutrition endpoint as CLIENT...")
    response = requests.get(f"{base_url}/diet/api/nutrition/plan/{plan_id}/", headers=client_headers)
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        nutrition_data = response.json()
        meals = nutrition_data.get("meals", [])
        print(f"✅ Client can see {len(meals)} meals")
        for meal in meals:
            print(f"  - {meal['meal_type']}: {meal['nutrition']['calories']} calories")
    else:
        print(f"❌ Client nutrition failed: {response.text}")
    
    # Step 14: Test with explicit date parameter
    print(f"\n14. Testing nutrition endpoint with explicit date parameter...")
    response = requests.get(f"{base_url}/diet/api/nutrition/plan/{plan_id}/?date={today.isoformat()}", headers=trainer_headers)
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        nutrition_data = response.json()
        meals = nutrition_data.get("meals", [])
        print(f"✅ With date parameter: {len(meals)} meals")
    else:
        print(f"❌ With date parameter failed: {response.text}")
    
    return True

if __name__ == "__main__":
    debug_meal_issue() 