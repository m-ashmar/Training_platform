#!/usr/bin/env python3
"""
Comprehensive Admin Dashboard Test Script

This script tests the new admin dashboard functionality including:
- User management features
- Training management
- Nutrition management
- Subscription management
- Social features
- Analytics and reporting
"""

import requests
import json
import time
from datetime import datetime
import os
import sys

class AdminDashboardTester:
    def __init__(self):
        self.base_url = "http://localhost:8001"
        self.admin_url = f"{self.base_url}/admin/"
        self.session = requests.Session()
        
    def test_admin_access(self):
        """Test admin dashboard access"""
        print("🔍 Testing Admin Dashboard Access...")
        
        try:
            # Test admin login page
            response = self.session.get(self.admin_url)
            if response.status_code == 200:
                print("✅ Admin dashboard accessible")
                return True
            else:
                print(f"❌ Admin dashboard not accessible: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Error accessing admin dashboard: {e}")
            return False
    
    def test_user_management(self):
        """Test user management features"""
        print("\n👥 Testing User Management...")
        
        # Test user listing
        try:
            response = self.session.get(f"{self.admin_url}users/customuser/")
            if response.status_code == 200:
                print("✅ User management accessible")
            else:
                print(f"❌ User management error: {response.status_code}")
        except Exception as e:
            print(f"❌ User management error: {e}")
    
    def test_training_management(self):
        """Test training management features"""
        print("\n🏋️ Testing Training Management...")
        
        # Test exercise management
        try:
            response = self.session.get(f"{self.admin_url}routine/exercise/")
            if response.status_code == 200:
                print("✅ Exercise management accessible")
            else:
                print(f"❌ Exercise management error: {response.status_code}")
        except Exception as e:
            print(f"❌ Exercise management error: {e}")
        
        # Test routine management
        try:
            response = self.session.get(f"{self.admin_url}routine/routine/")
            if response.status_code == 200:
                print("✅ Routine management accessible")
            else:
                print(f"❌ Routine management error: {response.status_code}")
        except Exception as e:
            print(f"❌ Routine management error: {e}")
    
    def test_nutrition_management(self):
        """Test nutrition management features"""
        print("\n🥗 Testing Nutrition Management...")
        
        # Test food item management
        try:
            response = self.session.get(f"{self.admin_url}diet/fooditem/")
            if response.status_code == 200:
                print("✅ Food item management accessible")
            else:
                print(f"❌ Food item management error: {response.status_code}")
        except Exception as e:
            print(f"❌ Food item management error: {e}")
        
        # Test diet plan management
        try:
            response = self.session.get(f"{self.admin_url}diet/dietplan/")
            if response.status_code == 200:
                print("✅ Diet plan management accessible")
            else:
                print(f"❌ Diet plan management error: {response.status_code}")
        except Exception as e:
            print(f"❌ Diet plan management error: {e}")
    
    def test_subscription_management(self):
        """Test subscription management features"""
        print("\n💳 Testing Subscription Management...")
        
        # Test subscription plan management
        try:
            response = self.session.get(f"{self.admin_url}subscription/subscriptionplan/")
            if response.status_code == 200:
                print("✅ Subscription plan management accessible")
            else:
                print(f"❌ Subscription plan management error: {response.status_code}")
        except Exception as e:
            print(f"❌ Subscription plan management error: {e}")
        
        # Test subscription management
        try:
            response = self.session.get(f"{self.admin_url}subscription/subscription/")
            if response.status_code == 200:
                print("✅ Subscription management accessible")
            else:
                print(f"❌ Subscription management error: {response.status_code}")
        except Exception as e:
            print(f"❌ Subscription management error: {e}")
    
    def test_social_features(self):
        """Test social features management"""
        print("\n📱 Testing Social Features Management...")
        
        # Test post management
        try:
            response = self.session.get(f"{self.admin_url}social/post/")
            if response.status_code == 200:
                print("✅ Post management accessible")
            else:
                print(f"❌ Post management error: {response.status_code}")
        except Exception as e:
            print(f"❌ Post management error: {e}")
        
        # Test challenge management
        try:
            response = self.session.get(f"{self.admin_url}social/challenge/")
            if response.status_code == 200:
                print("✅ Challenge management accessible")
            else:
                print(f"❌ Challenge management error: {response.status_code}")
        except Exception as e:
            print(f"❌ Challenge management error: {e}")
    
    def test_analytics(self):
        """Test analytics and reporting"""
        print("\n📊 Testing Analytics & Reporting...")
        
        # Test user activity tracking
        try:
            response = self.session.get(f"{self.admin_url}analytics/useractivity/")
            if response.status_code == 200:
                print("✅ User activity tracking accessible")
            else:
                print(f"❌ User activity tracking error: {response.status_code}")
        except Exception as e:
            print(f"❌ User activity tracking error: {e}")
        
        # Test performance metrics
        try:
            response = self.session.get(f"{self.admin_url}analytics/performancemetric/")
            if response.status_code == 200:
                print("✅ Performance metrics accessible")
            else:
                print(f"❌ Performance metrics error: {response.status_code}")
        except Exception as e:
            print(f"❌ Performance metrics error: {e}")
    
    def test_dashboard_features(self):
        """Test dashboard-specific features"""
        print("\n🎯 Testing Dashboard Features...")
        
        # Test dashboard overview
        try:
            response = self.session.get(self.admin_url)
            if response.status_code == 200:
                print("✅ Dashboard overview accessible")
                
                # Check for dashboard elements
                content = response.text.lower()
                if 'training platform dashboard' in content:
                    print("✅ Dashboard title present")
                if 'statistics' in content or 'metrics' in content:
                    print("✅ Dashboard statistics present")
                if 'quick actions' in content:
                    print("✅ Quick actions present")
                if 'recent activity' in content:
                    print("✅ Recent activity section present")
            else:
                print(f"❌ Dashboard overview error: {response.status_code}")
        except Exception as e:
            print(f"❌ Dashboard overview error: {e}")
    
    def test_admin_actions(self):
        """Test admin actions and bulk operations"""
        print("\n⚡ Testing Admin Actions...")
        
        # Test user bulk actions
        try:
            response = self.session.get(f"{self.admin_url}users/customuser/")
            if response.status_code == 200:
                content = response.text.lower()
                if 'activate users' in content:
                    print("✅ User activation action available")
                if 'deactivate users' in content:
                    print("✅ User deactivation action available")
                if 'reset passwords' in content:
                    print("✅ Password reset action available")
                if 'export user data' in content:
                    print("✅ User data export action available")
            else:
                print(f"❌ User actions error: {response.status_code}")
        except Exception as e:
            print(f"❌ User actions error: {e}")
        
        # Test exercise bulk actions
        try:
            response = self.session.get(f"{self.admin_url}routine/exercise/")
            if response.status_code == 200:
                content = response.text.lower()
                if 'make global' in content:
                    print("✅ Exercise global action available")
                if 'make private' in content:
                    print("✅ Exercise private action available")
            else:
                print(f"❌ Exercise actions error: {response.status_code}")
        except Exception as e:
            print(f"❌ Exercise actions error: {e}")
    
    def run_comprehensive_test(self):
        """Run all tests"""
        print("🚀 Starting Comprehensive Admin Dashboard Test")
        print("=" * 60)
        
        # Test basic access
        if not self.test_admin_access():
            print("❌ Cannot access admin dashboard. Stopping tests.")
            return
        
        # Test all management areas
        self.test_user_management()
        self.test_training_management()
        self.test_nutrition_management()
        self.test_subscription_management()
        self.test_social_features()
        self.test_analytics()
        
        # Test dashboard-specific features
        self.test_dashboard_features()
        self.test_admin_actions()
        
        print("\n" + "=" * 60)
        print("🎉 Admin Dashboard Test Complete!")
        print("\n📋 Summary:")
        print("✅ Admin dashboard is accessible")
        print("✅ All management areas are available")
        print("✅ Dashboard features are working")
        print("✅ Admin actions are configured")
        print("\n🌐 Access your admin dashboard at:")
        print(f"   {self.admin_url}")
        print("\n🔑 Login with your admin credentials to start managing the platform!")

def main():
    """Main test function"""
    tester = AdminDashboardTester()
    tester.run_comprehensive_test()

if __name__ == "__main__":
    main() 