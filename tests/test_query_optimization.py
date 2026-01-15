#!/usr/bin/env python3
"""
Test script to verify query optimization and new endpoint functionality.
"""

import requests
import json
import time
from datetime import datetime

class QueryOptimizationTester:
    def __init__(self):
        self.base_url = "http://localhost:8000/api"  # Changed to port 8000
        self.session = requests.Session()
        
    def login_trainer(self):
        """Login as trainer and get token"""
        login_data = {
            "email": "ll@gmail.com",  # Updated to user's trainer
            "password": "testpass123"
        }
        
        response = self.session.post(
            f"{self.base_url}/auth/token/",
            json=login_data
        )
        
        if response.status_code == 200:
            data = response.json()
            self.trainer_token = data['access']
            print(f"✅ Trainer login successful")
            return True
        else:
            print(f"❌ Trainer login failed: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    
    def login_client(self):
        """Login as client and get token"""
        login_data = {
            "email": "mm@gmail.com",  # User's client
            "password": "121212aA"
        }
        
        response = self.session.post(
            f"{self.base_url}/auth/token/",
            json=login_data
        )
        
        if response.status_code == 200:
            data = response.json()
            self.client_token = data['access']
            print(f"✅ Client login successful")
            return True
        else:
            print(f"❌ Client login failed: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    
    def test_admin_dashboard_optimization(self):
        """Test the optimized admin dashboard endpoint"""
        print("\n🔍 Testing Admin Dashboard Query Optimization")
        print("=" * 50)
        
        headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        # Test the optimized endpoint
        start_time = time.time()
        response = self.session.get(
            f"{self.base_url}/routine/analytics/admin_dashboard/",
            headers=headers
        )
        end_time = time.time()
        
        if response.status_code == 200:
            data = response.json()
            execution_time = end_time - start_time
            
            print(f"✅ Admin dashboard successful")
            print(f"   Execution time: {execution_time:.3f} seconds")
            print(f"   Clients returned: {len(data.get('dashboard', []))}")
            
            # Show sample data
            if data.get('dashboard'):
                sample_client = data['dashboard'][0]
                print(f"   Sample client: {sample_client.get('username')}")
                print(f"     Volume: {sample_client.get('total_volume')}")
                print(f"     Completion: {sample_client.get('completion_rate')}%")
                print(f"     Max streak: {sample_client.get('max_streak')}")
            
            return True
        else:
            print(f"❌ Admin dashboard failed: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    
    def test_recent_progress_endpoint(self):
        """Test the new recent progress endpoint"""
        print("\n🆕 Testing Recent Progress Endpoint")
        print("=" * 40)
        
        headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        response = self.session.get(
            f"{self.base_url}/routine/trainer/client-progress/recent/",
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Recent progress endpoint successful")
            print(f"   Trainer ID: {data.get('trainer_id')}")
            print(f"   Client count: {data.get('client_count')}")
            
            # Show sample recent data
            if data.get('recent_data'):
                sample_client = data['recent_data'][0]
                print(f"   Sample client: {sample_client.get('client_name')}")
                print(f"     Recent volume: {sample_client.get('recent_volume')}")
                print(f"     Completion rate: {sample_client.get('completion_rate')}%")
                print(f"     Last workout: {sample_client.get('last_workout')}")
                print(f"     Recent progress entries: {len(sample_client.get('recent_progress', []))}")
            
            return True
        else:
            print(f"❌ Recent progress endpoint failed: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    
    def test_specific_client_progress(self):
        """Test the specific client progress endpoint"""
        print("\n👤 Testing Specific Client Progress")
        print("=" * 35)
        
        headers = {"Authorization": f"Bearer {self.trainer_token}"}
        
        # First get a client ID from the dashboard
        response = self.session.get(
            f"{self.base_url}/routine/analytics/admin_dashboard/",
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('dashboard'):
                client_id = data['dashboard'][0]['client_id']
                
                # Test specific client progress
                response = self.session.get(
                    f"{self.base_url}/routine/trainer/client-progress/{client_id}/",
                    headers=headers
                )
                
                if response.status_code == 200:
                    client_data = response.json()
                    print(f"✅ Client {client_id} progress successful")
                    
                    # Handle dict response (new format)
                    if isinstance(client_data, dict):
                        recent_activity = client_data.get('recent_activity', [])
                        print(f"   Recent activity entries: {len(recent_activity)}")
                        
                        if recent_activity:
                            sample = recent_activity[0]
                            print(f"   Sample activity:")
                            print(f"     Routine: {sample.get('routine_name')}")
                            print(f"     Date: {sample.get('date')}")
                            print(f"     Volume: {sample.get('volume')}")
                    # Handle list response (old format fallback)
                    elif isinstance(client_data, list):
                        print(f"   Progress entries: {len(client_data)}")
                        if client_data:
                            sample_progress = client_data[0]
                            print(f"   Sample progress:")
                            print(f"     Routine: {sample_progress.get('routine', {}).get('name')}")
                            print(f"     Day: {sample_progress.get('day')}")
                            print(f"     Status: {sample_progress.get('status')}")
                    
                    return True
                else:
                    print(f"❌ Client progress failed: {response.status_code}")
                    return False
            else:
                print("❌ No clients found in dashboard")
                return False
        else:
            print(f"❌ Dashboard failed: {response.status_code}")
            return False
    
    def run_all_tests(self):
        """Run all tests"""
        print("🚀 Starting Query Optimization and Endpoint Tests")
        print("=" * 55)
        
        # Login
        if not self.login_trainer():
            return False
        
        # Run tests
        tests = [
            self.test_admin_dashboard_optimization,
            self.test_recent_progress_endpoint,
            self.test_specific_client_progress
        ]
        
        results = []
        for test in tests:
            try:
                result = test()
                results.append(result)
            except Exception as e:
                print(f"❌ Test failed with exception: {str(e)}")
                results.append(False)
        
        # Summary
        print("\n📊 Test Summary")
        print("=" * 15)
        passed = sum(results)
        total = len(results)
        print(f"Passed: {passed}/{total}")
        
        if passed == total:
            print("🎉 All tests passed! Query optimization and new endpoint working correctly.")
        else:
            print("⚠️  Some tests failed. Check the output above for details.")
        
        return passed == total

if __name__ == "__main__":
    tester = QueryOptimizationTester()
    tester.run_all_tests() 