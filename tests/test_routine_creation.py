#!/usr/bin/env python3
import requests
import json

# Test routine creation with approved trainer-client relationship
base_url = "http://127.0.0.1:8000"

# Login as trainer (ID: 112)
trainer_login_data = {
    "email": "realtrainer_1750810930@test.com",
    "password": "trainerpass123"
}

response = requests.post(f"{base_url}/api/auth/token/", json=trainer_login_data)
if response.status_code == 200:
    trainer_token = response.json().get('access')
    print("✅ Trainer login successful")
    
    # Create routine
    routine_data = {
        "name": "Test Strength Routine",
        "description": "A test routine for verification",
        "is_active": True,
        "assigned_to": [113]  # Client ID: 113
    }
    
    headers = {"Authorization": f"Bearer {trainer_token}", "Content-Type": "application/json"}
    routine_response = requests.post(f"{base_url}/api/routine/routines/", json=routine_data, headers=headers)
    
    print(f"Routine creation status: {routine_response.status_code}")
    print(f"Routine creation response: {routine_response.text}")
    
    if routine_response.status_code in [200, 201]:
        print("✅ Routine creation successful!")
    else:
        print("❌ Routine creation failed")
else:
    print("❌ Trainer login failed") 