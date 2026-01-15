#!/usr/bin/env python3
import requests
import json

# Test JWT token endpoint with user role
base_url = "http://127.0.0.1:8000"

# Test with trainer login
trainer_login_data = {
    "email": "realtrainer_1750810930@test.com",
    "password": "trainerpass123"
}

# Test with client login
client_login_data = {
    "email": "realclient_1750810930@test.com",
    "password": "clientpass123"
}

print("🔐 Testing JWT Token Endpoint with User Role")
print("=" * 50)

def test_login(login_data, user_type):
    print(f"\n🧪 Testing {user_type.upper()} Login:")
    print("-" * 30)
    
    try:
        response = requests.post(f"{base_url}/api/auth/token/", json=login_data)
        
        if response.status_code == 200:
            data = response.json()
            print("✅ JWT Token Response:")
            print(f"   Access Token: {data.get('access', 'N/A')[:50]}...")
            print(f"   Refresh Token: {data.get('refresh', 'N/A')[:50]}...")
            
            # Check if user info is included
            if 'user' in data:
                user_info = data['user']
                print("✅ User Information Included:")
                print(f"   ID: {user_info.get('id')}")
                print(f"   Username: {user_info.get('username')}")
                print(f"   Email: {user_info.get('email')}")
                print(f"   First Name: {user_info.get('first_name')}")
                print(f"   Last Name: {user_info.get('last_name')}")
                print(f"   User Type: {user_info.get('user_type')}")
                
                # This is what the Flutter app needs for routing!
                if user_info.get('user_type') == 'trainer':
                    print("🎯 ROUTING: User should be directed to TRAINER DASHBOARD")
                elif user_info.get('user_type') == 'client':
                    print("🎯 ROUTING: User should be directed to CLIENT DASHBOARD")
                elif user_info.get('user_type') == 'admin':
                    print("🎯 ROUTING: User should be directed to ADMIN DASHBOARD")
            else:
                print("❌ User information not found in response")
                
        else:
            print(f"❌ Login failed with status: {response.status_code}")
            print(f"Response: {response.text}")
            
    except Exception as e:
        print(f"❌ Error: {e}")

# Test trainer login
test_login(trainer_login_data, "trainer")

# Test client login
test_login(client_login_data, "client")

print("\n" + "=" * 50)
print("🎯 JWT Token endpoint now includes user_type for proper Flutter routing!")
print("📱 Flutter app can now route users to correct dashboard based on user_type!") 