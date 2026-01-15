#!/usr/bin/env python3
import requests
import json
import time

# Send request to trainer named "bdfb"
base_url = "http://127.0.0.1:8000"

def login_user(email, password):
    """Login user and return access token and user id"""
    response = requests.post(
        f"{base_url}/api/auth/token/",
        json={"email": email, "password": password}
    )
    if response.status_code == 200:
        data = response.json()
        return data.get('access'), data.get('user', {}).get('id')
    else:
        print(f"Login failed for {email}: {response.status_code}")
        print(f"Response: {response.text}")
        return None, None

def get_auth_headers(token):
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}'
    }

def main():
    print("🔐 Sending Request to Trainer 'bdfb'")
    print("=" * 50)
    
    # Step 1: Register a new client
    timestamp = int(time.time())
    client_email = f"testclient_{timestamp}@test.com"
    
    client_data = {
        "username": f"testclient_{timestamp}",
        "email": client_email,
        "password1": "clientpass123",
        "password2": "clientpass123",
        "user_type": "client",
        "first_name": "Test",
        "last_name": "Client",
        "phone_number": "+1234567890"
    }
    
    print("📝 Registering new client...")
    response = requests.post(f"{base_url}/api/auth/register/", json=client_data)
    
    if response.status_code != 201:
        print(f"❌ Client registration failed: {response.status_code}")
        print(f"Response: {response.text}")
        return
    
    print("✅ Client registered successfully")
    
    # Step 2: Login client to get token
    print("🔑 Logging in client...")
    client_token, client_id = login_user(client_email, "clientpass123")
    
    if not client_token:
        print("❌ Client login failed")
        return
    
    print(f"✅ Client logged in. ID: {client_id}")
    
    # Step 3: Get available trainers to find "bdfb"
    print("🔍 Looking for trainer 'bdfb'...")
    response = requests.get(
        f"{base_url}/api/auth/client/available-trainers/",
        headers=get_auth_headers(client_token)
    )
    
    if response.status_code != 200:
        print(f"❌ Failed to get trainers: {response.status_code}")
        print(f"Response: {response.text}")
        return
    
    data = response.json()
    trainers = data.get('available_trainers', [])
    
    # Find trainer named "bdfb"
    bdfb_trainer = None
    for trainer in trainers:
        if trainer.get('username') == 'bdfb':
            bdfb_trainer = trainer
            break
    
    if not bdfb_trainer:
        print("❌ Trainer 'bdfb' not found in available trainers")
        print("Available trainers:")
        for trainer in trainers[:5]:  # Show first 5 trainers
            print(f"  - {trainer.get('username')} ({trainer.get('first_name')} {trainer.get('last_name')})")
        return
    
    print(f"✅ Found trainer 'bdfb' with ID: {bdfb_trainer['id']}")
    
    # Step 4: Send request to bdfb
    print("📤 Sending request to trainer 'bdfb'...")
    request_data = {
        "trainer_id": bdfb_trainer['id'],
        "message": "Hi bdfb! I would like to work with you for strength training. Can you help me achieve my fitness goals?"
    }
    
    response = requests.post(
        f"{base_url}/api/auth/client/request-trainer/",
        json=request_data,
        headers=get_auth_headers(client_token)
    )
    
    if response.status_code == 200:
        data = response.json()
        print("✅ Request sent successfully!")
        print(f"   Status: {data.get('status')}")
        print(f"   Message: {data.get('message')}")
        
        # Step 5: Check request status
        print("\n📋 Checking request status...")
        response = requests.get(
            f"{base_url}/api/auth/client/request-status/",
            headers=get_auth_headers(client_token)
        )
        
        if response.status_code == 200:
            data = response.json()
            requests_list = data.get('requests', [])
            if requests_list:
                latest_request = requests_list[0]
                print(f"✅ Latest request status: {latest_request.get('status')}")
                print(f"   Trainer: {latest_request.get('trainer_name')}")
                print(f"   Requested at: {latest_request.get('requested_at')}")
        
    else:
        print(f"❌ Request failed: {response.status_code}")
        print(f"Response: {response.text}")
    
    print("\n" + "=" * 50)
    print("🎯 You can now check the pending requests as trainer 'bdfb'!")
    print("   Use: GET /api/auth/trainer/pending-requests/")

if __name__ == "__main__":
    main() 