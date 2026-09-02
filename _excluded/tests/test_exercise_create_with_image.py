#!/usr/bin/env python3
"""
Test script for the exercise creation with image endpoint.
Tests the POST request to /api/routine/exercises/create-with-image/
"""

import requests
import json
import os

# Configuration
BASE_URL = "http://localhost:8000"
LOGIN_URL = f"{BASE_URL}/api/auth/token/"
EXERCISE_CREATE_URL = f"{BASE_URL}/api/routine/exercises/create-with-image/"

# Test credentials (using trainer account)
TEST_CREDENTIALS = {
    "email": "realtrainer_1750810930@test.com",
    "password": "trainerpass123"
}

def test_exercise_create_with_image():
    """Test the exercise creation with image endpoint."""
    
    print("🔍 Testing Exercise Creation with Image Endpoint")
    print("=" * 50)
    
    # Step 1: Login to get JWT token
    print("\n1. Logging in to get JWT token...")
    try:
        login_response = requests.post(LOGIN_URL, json=TEST_CREDENTIALS)
        login_response.raise_for_status()
        
        token_data = login_response.json()
        access_token = token_data.get('access')
        
        if not access_token:
            print("❌ Failed to get access token")
            return False
            
        print("✅ Login successful")
        print(f"   Token: {access_token[:20]}...")
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Login failed: {e}")
        return False
    
    # Step 2: Test POST request to exercise creation endpoint
    print("\n2. Testing POST request to exercise creation endpoint...")
    
    # Prepare headers
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'multipart/form-data'
    }
    
    # Prepare form data
    form_data = {
        'name': 'Test Exercise',
        'description': 'This is a test exercise for API testing',
        'target_muscle': 'Chest',
        'difficulty_level': 'beginner'
    }
    
    # Test without image first
    print("   Testing without image...")
    try:
        response = requests.post(
            EXERCISE_CREATE_URL,
            data=form_data,
            headers={'Authorization': f'Bearer {access_token}'}
        )
        
        print(f"   Status Code: {response.status_code}")
        print(f"   Response: {response.text[:200]}...")
        
        if response.status_code == 201:
            print("✅ Exercise created successfully without image")
            exercise_data = response.json()
            print(f"   Exercise ID: {exercise_data.get('exercise', {}).get('id')}")
        else:
            print(f"❌ Failed to create exercise: {response.status_code}")
            print(f"   Error: {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Request failed: {e}")
        return False
    
    # Step 3: Test with image (if we have a test image)
    print("\n3. Testing with image...")
    
    # Check if we have a test image file
    test_image_path = "test_image.jpg"
    if os.path.exists(test_image_path):
        try:
            with open(test_image_path, 'rb') as image_file:
                files = {'image': image_file}
                response = requests.post(
                    EXERCISE_CREATE_URL,
                    data=form_data,
                    files=files,
                    headers={'Authorization': f'Bearer {access_token}'}
                )
                
                print(f"   Status Code: {response.status_code}")
                print(f"   Response: {response.text[:200]}...")
                
                if response.status_code == 201:
                    print("✅ Exercise created successfully with image")
                    exercise_data = response.json()
                    print(f"   Exercise ID: {exercise_data.get('exercise', {}).get('id')}")
                else:
                    print(f"❌ Failed to create exercise with image: {response.status_code}")
                    print(f"   Error: {response.text}")
                    
        except requests.exceptions.RequestException as e:
            print(f"❌ Request with image failed: {e}")
    else:
        print("   ⚠️  No test image found, skipping image test")
        print("   Create a test_image.jpg file to test image upload")
    
    print("\n" + "=" * 50)
    print("✅ Test completed")
    return True

if __name__ == "__main__":
    test_exercise_create_with_image() 