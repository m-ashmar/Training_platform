#!/usr/bin/env python3
"""
Test script for the enhanced trainer client progress recent endpoint.
Tests the updated endpoint that now includes user profile information and template IDs.
"""

import requests
import json
from datetime import datetime, timedelta

class EnhancedRecentProgressTester:
    def __init__(self):
        self.base_url = "http://localhost:8001"
        self.session = requests.Session()
        self.trainer_token = None
        self.client_token = None
        
    def authenticate_trainer(self):
        """Authenticate as a trainer"""
        print("🔐 Authenticating as trainer...")
        
        login_data = {
            "email": "apitest_trainer@example.com",
            "password": "testpass123"
        }
        
        response = self.session.post(
            f"{self.base_url}/api/auth/token/",
            json=login_data
        )
        
        if response.status_code == 200:
            data = response.json()
            self.trainer_token = data['access']
            print(f"✅ Trainer authenticated: {data['user']['username']}")
            return True
        else:
            print(f"❌ Trainer authentication failed: {response.status_code}")
            print(f"Response: {response.text}")
            return False
    
    def test_enhanced_recent_progress(self):
        """Test the enhanced recent progress endpoint"""
        print("\n🆕 Testing Enhanced Recent Progress Endpoint")
        print("=" * 50)
        
        if not self.trainer_token:
            print("❌ No trainer token available")
            return False
        
        headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        response = self.session.get(
            f"{self.base_url}/api/routine/trainer/client-progress/recent/",
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Enhanced recent progress endpoint successful")
            print(f"   Trainer ID: {data.get('trainer_id')}")
            print(f"   Trainer Name: {data.get('trainer_name')}")
            print(f"   Client count: {data.get('client_count')}")
            
            # Show sample recent data with enhanced information
            if data.get('recent_data'):
                print(f"\n📊 Sample Client Data:")
                sample_client = data['recent_data'][0]
                
                # User profile information
                user_profile = sample_client.get('user_profile', {})
                print(f"   👤 Client Profile:")
                print(f"      ID: {user_profile.get('id')}")
                print(f"      Name: {user_profile.get('full_name')}")
                print(f"      Email: {user_profile.get('email')}")
                print(f"      Age: {user_profile.get('age')}")
                print(f"      Gender: {user_profile.get('gender')}")
                print(f"      Height: {user_profile.get('height')} cm")
                print(f"      Weight: {user_profile.get('weight')} kg")
                print(f"      Activity Level: {user_profile.get('activity_level')}")
                print(f"      BMI: {user_profile.get('bmi')}")
                print(f"      BMR: {user_profile.get('bmr')}")
                print(f"      TDEE: {user_profile.get('tdee')}")
                print(f"      Goals: {user_profile.get('client_goals')}")
                print(f"      Profile Picture: {user_profile.get('profile_picture')}")
                
                # Progress information
                print(f"   📈 Progress Summary:")
                print(f"      Recent Volume: {sample_client.get('recent_volume')}")
                print(f"      Completion Rate: {sample_client.get('completion_rate')}%")
                print(f"      Last Workout: {sample_client.get('last_workout')}")
                
                # Recent progress entries with template IDs
                recent_progress = sample_client.get('recent_progress', [])
                print(f"   🏋️ Recent Progress Entries ({len(recent_progress)}):")
                
                for i, progress in enumerate(recent_progress, 1):
                    print(f"      {i}. Routine: {progress.get('routine_name')}")
                    print(f"         Routine ID: {progress.get('routine_id')}")
                    print(f"         Template ID: {progress.get('template_id')}")
                    print(f"         Day: {progress.get('day')}")
                    print(f"         Status: {progress.get('status')}")
                    print(f"         Exercises: {progress.get('exercises_completed')}/{progress.get('total_exercises')}")
                    print(f"         Completion: {progress.get('completion_percentage')}%")
                    print(f"         Updated: {progress.get('updated_at')}")
                    if progress.get('notes'):
                        print(f"         Notes: {progress.get('notes')}")
                    print()
            
            return True
        else:
            print(f"❌ Enhanced recent progress endpoint failed: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    
    def test_response_structure(self):
        """Test the response structure and data types"""
        print("\n🔍 Testing Response Structure")
        print("=" * 30)
        
        if not self.trainer_token:
            return False
        
        headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        response = self.session.get(
            f"{self.base_url}/api/routine/trainer/client-progress/recent/",
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            
            # Check top-level structure
            required_top_fields = ['trainer_id', 'trainer_name', 'client_count', 'recent_data']
            for field in required_top_fields:
                if field not in data:
                    print(f"❌ Missing top-level field: {field}")
                    return False
                print(f"✅ Found top-level field: {field}")
            
            # Check client data structure
            if data['recent_data']:
                client = data['recent_data'][0]
                required_client_fields = [
                    'client_id', 'client_name', 'user_profile', 
                    'recent_volume', 'completion_rate', 'last_workout', 'recent_progress'
                ]
                
                for field in required_client_fields:
                    if field not in client:
                        print(f"❌ Missing client field: {field}")
                        return False
                    print(f"✅ Found client field: {field}")
                
                # Check user profile structure
                user_profile = client['user_profile']
                required_profile_fields = [
                    'id', 'username', 'email', 'first_name', 'last_name', 'full_name',
                    'profile_picture', 'height', 'weight', 'age', 'gender', 
                    'activity_level', 'specific_injury', 'client_goals', 
                    'client_preferences', 'date_joined', 'last_login', 'bmi', 'bmr', 'tdee'
                ]
                
                for field in required_profile_fields:
                    if field not in user_profile:
                        print(f"❌ Missing profile field: {field}")
                        return False
                    print(f"✅ Found profile field: {field}")
                
                # Check progress entry structure
                if client['recent_progress']:
                    progress = client['recent_progress'][0]
                    required_progress_fields = [
                        'routine_id', 'routine_name', 'template_id', 'day', 'status',
                        'updated_at', 'exercises_completed', 'total_exercises',
                        'completion_percentage', 'completion_time', 'notes'
                    ]
                    
                    for field in required_progress_fields:
                        if field not in progress:
                            print(f"❌ Missing progress field: {field}")
                            return False
                        print(f"✅ Found progress field: {field}")
            
            print("✅ All required fields present in response structure")
            return True
        else:
            print(f"❌ Failed to get response for structure testing")
            return False
    
    def test_data_types(self):
        """Test that data types are correct"""
        print("\n🔢 Testing Data Types")
        print("=" * 20)
        
        if not self.trainer_token:
            return False
        
        headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        response = self.session.get(
            f"{self.base_url}/api/routine/trainer/client-progress/recent/",
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            
            # Test top-level data types
            assert isinstance(data['trainer_id'], int), "trainer_id should be int"
            assert isinstance(data['trainer_name'], str), "trainer_name should be str"
            assert isinstance(data['client_count'], int), "client_count should be int"
            assert isinstance(data['recent_data'], list), "recent_data should be list"
            
            print("✅ Top-level data types correct")
            
            if data['recent_data']:
                client = data['recent_data'][0]
                user_profile = client['user_profile']
                
                # Test user profile data types
                assert isinstance(user_profile['id'], int), "user profile id should be int"
                assert isinstance(user_profile['username'], str), "username should be str"
                assert isinstance(user_profile['email'], str), "email should be str"
                assert isinstance(user_profile['full_name'], str), "full_name should be str"
                assert user_profile['profile_picture'] is None or isinstance(user_profile['profile_picture'], str), "profile_picture should be str or None"
                
                print("✅ User profile data types correct")
                
                # Test progress data types
                if client['recent_progress']:
                    progress = client['recent_progress'][0]
                    assert isinstance(progress['routine_id'], int), "routine_id should be int"
                    assert isinstance(progress['routine_name'], str), "routine_name should be str"
                    assert progress['template_id'] is None, "template_id should be None (not implemented yet)"
                    assert isinstance(progress['day'], int), "day should be int"
                    assert isinstance(progress['status'], str), "status should be str"
                    
                    print("✅ Progress data types correct")
            
            print("✅ All data types are correct")
            return True
        else:
            print(f"❌ Failed to get response for data type testing")
            return False
    
    def run_all_tests(self):
        """Run all tests"""
        print("🚀 Starting Enhanced Recent Progress Tests")
        print("=" * 50)
        
        # Authenticate
        if not self.authenticate_trainer():
            return False
        
        # Run tests
        tests = [
            ("Enhanced Recent Progress", self.test_enhanced_recent_progress),
            ("Response Structure", self.test_response_structure),
            ("Data Types", self.test_data_types),
        ]
        
        results = []
        for test_name, test_func in tests:
            print(f"\n🧪 Running: {test_name}")
            try:
                result = test_func()
                results.append((test_name, result))
                status = "✅ PASS" if result else "❌ FAIL"
                print(f"{status}: {test_name}")
            except Exception as e:
                print(f"❌ ERROR: {test_name} - {str(e)}")
                results.append((test_name, False))
        
        # Summary
        print("\n" + "=" * 50)
        print("📊 TEST SUMMARY")
        print("=" * 50)
        
        passed = sum(1 for _, result in results if result)
        total = len(results)
        
        for test_name, result in results:
            status = "✅ PASS" if result else "❌ FAIL"
            print(f"{status}: {test_name}")
        
        print(f"\nOverall: {passed}/{total} tests passed")
        
        if passed == total:
            print("🎉 All tests passed! Enhanced recent progress endpoint is working correctly.")
        else:
            print("⚠️ Some tests failed. Please check the implementation.")
        
        return passed == total

if __name__ == "__main__":
    tester = EnhancedRecentProgressTester()
    success = tester.run_all_tests()
    exit(0 if success else 1) 