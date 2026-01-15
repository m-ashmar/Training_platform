#!/usr/bin/env python3
"""
Simple JWT-based Diet API Testing Script

Usage:
    python test_jwt_diet.py --username mu --password your_password
"""

import requests
import json
import argparse

def test_diet_apis(username, password, base_url="http://127.0.0.1:8000"):
    """Test diet APIs using JWT authentication."""
    
    print("🚀 Testing Diet APIs with JWT Authentication")
    print("=" * 50)
    
    # Step 1: Get JWT tokens
    print("1. Getting JWT tokens...")
    token_url = f"{base_url}/api/auth/token/"
    token_data = {"username": username, "password": password}
    
    try:
        token_response = requests.post(token_url, json=token_data)
        if token_response.status_code != 200:
            print(f"❌ JWT Login failed: {token_response.status_code}")
            print(f"Response: {token_response.text}")
            return False
        
        tokens = token_response.json()
        access_token = tokens.get('access')
        print(f"✅ JWT Login successful for user: {username}")
        
    except Exception as e:
        print(f"❌ JWT Login error: {str(e)}")
        return False
    
    # Headers for authenticated requests
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    # Step 2: Test Food Search
    print("\n2. Testing Food Search API...")
    search_url = f"{base_url}/diet/api/food/search/"
    search_params = {"q": "chicken"}
    
    try:
        search_response = requests.get(search_url, params=search_params, headers=headers)
        if search_response.status_code == 200:
            search_data = search_response.json()
            print(f"✅ Food search successful")
            print(f"   Query: {search_data.get('query')}")
            print(f"   Local results: {search_data.get('local_count')}")
            print(f"   Edamam results: {search_data.get('edamam_count')}")
            print(f"   Total results: {search_data.get('total_count')}")
            
            # Show first result
            results = search_data.get('results', [])
            if results:
                first_food = results[0]
                print(f"   First result: {first_food.get('name')} ({first_food.get('source')})")
        else:
            print(f"❌ Food search failed: {search_response.status_code}")
            print(f"Response: {search_response.text}")
            
    except Exception as e:
        print(f"❌ Food search error: {str(e)}")
    
    # Step 3: Test User Preferences
    print("\n3. Testing User Preferences API...")
    prefs_url = f"{base_url}/diet/api/preferences/"
    
    try:
        prefs_response = requests.get(prefs_url, headers=headers)
        if prefs_response.status_code == 200:
            prefs_data = prefs_response.json()
            print(f"✅ User preferences retrieved")
            print(f"   Liked foods: {len(prefs_data.get('liked_foods', []))}")
            print(f"   Disliked foods: {len(prefs_data.get('disliked_foods', []))}")
        else:
            print(f"❌ User preferences failed: {prefs_response.status_code}")
            print(f"Response: {prefs_response.text}")
            
    except Exception as e:
        print(f"❌ User preferences error: {str(e)}")
    
    # Step 4: Test Food Import (if we have Edamam results)
    if 'search_data' in locals() and search_data:
        edamam_foods = [f for f in search_data.get('results', []) if f.get('source') == 'edamam']
        if edamam_foods:
            print("\n4. Testing Food Import API...")
            food = edamam_foods[0]
            
            import_url = f"{base_url}/diet/api/food/import/"
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
            
            try:
                import_response = requests.post(import_url, json=import_data, headers=headers)
                if import_response.status_code == 201:
                    import_result = import_response.json()
                    print(f"✅ Food import successful")
                    print(f"   Imported: {import_result.get('food_name')}")
                    print(f"   Food ID: {import_result.get('food_id')}")
                    print(f"   Category: {import_result.get('category')}")
                else:
                    print(f"❌ Food import failed: {import_response.status_code}")
                    print(f"Response: {import_response.text}")
                    
            except Exception as e:
                print(f"❌ Food import error: {str(e)}")
    
    print("\n" + "=" * 50)
    print("🏁 Testing completed!")
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Diet APIs with JWT")
    parser.add_argument("--username", default="mu", help="Username")
    parser.add_argument("--password", required=True, help="Password")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL")
    
    args = parser.parse_args()
    test_diet_apis(args.username, args.password, args.base_url) 