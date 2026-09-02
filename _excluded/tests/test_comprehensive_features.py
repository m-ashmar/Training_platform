#!/usr/bin/env python
"""
Comprehensive Test Suite for Training Platform New Features

This script tests all the newly implemented features:
- Input validation
- File upload security
- Rate limiting
- Error handling
- Caching
- Analytics
- Social features
- Performance optimizations
"""

import os
import sys
import django
import requests
import time
import json
import tempfile
from PIL import Image
from io import BytesIO

# Django setup
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

# Import our models and validators
from training_platform.validators import (
    PasswordStrengthValidator, 
    validate_user_input,
    validate_nutrition_value
)
from training_platform.file_security import (
    FileSecurityValidator,
    validate_uploaded_image,
    secure_file_upload_path
)
from analytics.models import (
    UserActivity, PerformanceMetric, UserSession, 
    FeatureUsage, UserGoal
)
from social.models import (
    UserFollow, Post, PostLike, Comment, 
    Challenge, Achievement, UserAchievement
)

User = get_user_model()


class ComprehensiveFeatureTestSuite:
    """
    Comprehensive test suite for all new features
    """
    
    def __init__(self):
        self.client = Client()
        self.base_url = 'http://localhost:8000'
        self.test_results = {
            'input_validation': {'passed': 0, 'failed': 0, 'errors': []},
            'file_security': {'passed': 0, 'failed': 0, 'errors': []},
            'rate_limiting': {'passed': 0, 'failed': 0, 'errors': []},
            'error_handling': {'passed': 0, 'failed': 0, 'errors': []},
            'caching': {'passed': 0, 'failed': 0, 'errors': []},
            'analytics': {'passed': 0, 'failed': 0, 'errors': []},
            'social_features': {'passed': 0, 'failed': 0, 'errors': []},
            'performance': {'passed': 0, 'failed': 0, 'errors': []}
        }
        self.setup_test_data()
    
    def setup_test_data(self):
        """Setup test users and data"""
        print("🔧 Setting up test data...")
        
        try:
            # Clear existing test users first
            User.objects.filter(email__in=[
                'admin@test.com', 'trainer@test.com', 'client@test.com'
            ]).delete()
            
            # Clear existing test achievements
            from social.models import Achievement
            Achievement.objects.filter(name='First Workout').delete()
            
            # Create test users
            self.admin_user = User.objects.create_user(
                username='test_admin',
                email='admin@test.com',
                password='TestAdmin123!',
                phone_number='1234567890',
                user_type='admin'
            )
            
            self.trainer_user = User.objects.create_user(
                username='test_trainer',
                email='trainer@test.com',
                password='TestTrainer123!',
                phone_number='1234567891',
                user_type='trainer'
            )
            
            self.client_user = User.objects.create_user(
                username='test_client',
                email='client@test.com',
                password='TestClient123!',
                phone_number='1234567892',
                user_type='client'
            )
            
            print("✅ Test data setup completed")
            
        except Exception as e:
            print(f"❌ Test data setup failed: {e}")
            # Create fallback users for testing
            self.admin_user = User.objects.filter(user_type='admin').first()
            self.trainer_user = User.objects.filter(user_type='trainer').first()
            self.client_user = User.objects.filter(user_type='client').first()
            
            if not any([self.admin_user, self.trainer_user, self.client_user]):
                print("⚠️ No fallback users available. Some tests will fail.")
    
    def test_input_validation(self):
        """Test comprehensive input validation"""
        print("\n🔍 Testing Input Validation...")
        
        tests = [
            ('password_strength', self._test_password_validation),
            ('xss_prevention', self._test_xss_prevention),
            ('sql_injection', self._test_sql_injection),
            ('nutrition_values', self._test_nutrition_validation),
            ('email_validation', self._test_email_validation),
        ]
        
        for test_name, test_func in tests:
            try:
                result = test_func()
                if result:
                    self.test_results['input_validation']['passed'] += 1
                    print(f"  ✅ {test_name}: PASSED")
                else:
                    self.test_results['input_validation']['failed'] += 1
                    print(f"  ❌ {test_name}: FAILED")
            except Exception as e:
                self.test_results['input_validation']['failed'] += 1
                self.test_results['input_validation']['errors'].append(f"{test_name}: {e}")
                print(f"  💥 {test_name}: ERROR - {e}")
    
    def _test_password_validation(self):
        """Test password strength validation"""
        validator = PasswordStrengthValidator()
        
        # Valid passwords should pass
        try:
            validator.validate('StrongPass123!')
            valid_passed = True
        except:
            valid_passed = False
        
        # Weak passwords should fail
        weak_failed = False
        try:
            validator.validate('weak')
            weak_failed = False
        except:
            weak_failed = True
        
        return valid_passed and weak_failed
    
    def _test_xss_prevention(self):
        """Test XSS prevention"""
        try:
            # Should raise ValidationError
            validate_user_input('<script>alert("xss")</script>')
            return False
        except:
            return True
    
    def _test_sql_injection(self):
        """Test SQL injection prevention"""
        try:
            # Should raise ValidationError
            validate_user_input("'; DROP TABLE users; --")
            return False
        except:
            return True
    
    def _test_nutrition_validation(self):
        """Test nutrition value validation"""
        try:
            validate_nutrition_value(500)  # Valid
            validate_nutrition_value(-100)  # Should fail
            return False
        except:
            return True
    
    def _test_email_validation(self):
        """Test email validation"""
        from training_platform.validators import ValidatedEmailField
        field = ValidatedEmailField()
        
        try:
            field.run_validation('test@example.com')  # Valid
            field.run_validation('invalid<script>')  # Should fail
            return False
        except:
            return True
    
    def test_file_security(self):
        """Test file upload security"""
        print("\n🔒 Testing File Upload Security...")
        
        tests = [
            ('image_validation', self._test_image_validation),
            ('malicious_file_detection', self._test_malicious_file_detection),
            ('file_size_limits', self._test_file_size_limits),
            ('path_sanitization', self._test_path_sanitization),
        ]
        
        for test_name, test_func in tests:
            try:
                result = test_func()
                if result:
                    self.test_results['file_security']['passed'] += 1
                    print(f"  ✅ {test_name}: PASSED")
                else:
                    self.test_results['file_security']['failed'] += 1
                    print(f"  ❌ {test_name}: FAILED")
            except Exception as e:
                self.test_results['file_security']['failed'] += 1
                self.test_results['file_security']['errors'].append(f"{test_name}: {e}")
                print(f"  💥 {test_name}: ERROR - {e}")
    
    def _test_image_validation(self):
        """Test image validation"""
        # Create a valid test image
        img = Image.new('RGB', (100, 100), color='red')
        img_bytes = BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        
        test_file = SimpleUploadedFile(
            "test.png",
            img_bytes.getvalue(),
            content_type="image/png"
        )
        
        try:
            result = validate_uploaded_image(test_file)
            return result['is_valid']
        except:
            return False
    
    def _test_malicious_file_detection(self):
        """Test malicious file detection"""
        # Create a fake malicious file
        malicious_content = b'<script>alert("xss")</script>'
        test_file = SimpleUploadedFile(
            "malicious.txt",
            malicious_content,
            content_type="text/plain"
        )
        
        try:
            validate_uploaded_image(test_file)
            return False  # Should have failed
        except:
            return True  # Expected to fail
    
    def _test_file_size_limits(self):
        """Test file size limits"""
        # Create a large fake file
        large_content = b'x' * (10 * 1024 * 1024)  # 10MB
        test_file = SimpleUploadedFile(
            "large.txt",
            large_content,
            content_type="text/plain"
        )
        
        try:
            validate_uploaded_image(test_file)
            return False  # Should have failed
        except:
            return True  # Expected to fail
    
    def _test_path_sanitization(self):
        """Test path sanitization"""
        from training_platform.file_security import FileSecurityValidator
        validator = FileSecurityValidator()
        
        dangerous_filename = "../../../etc/passwd"
        safe_filename = validator.sanitize_filename(dangerous_filename)
        
        return '..' not in safe_filename and '/' not in safe_filename
    
    def test_rate_limiting(self):
        """Test rate limiting functionality"""
        print("\n⏱️ Testing Rate Limiting...")
        
        # Test rate limiting by making multiple requests
        test_url = '/api/auth/login/'
        
        try:
            # Make multiple requests quickly
            success_count = 0
            rate_limited_count = 0
            
            for i in range(10):
                response = self.client.post(test_url, {
                    'email': 'test@example.com',
                    'password': 'password'
                })
                
                if response.status_code == 429:  # Rate limited
                    rate_limited_count += 1
                else:
                    success_count += 1
            
            # Rate limiting should kick in at some point
            if rate_limited_count > 0:
                self.test_results['rate_limiting']['passed'] += 1
                print(f"  ✅ Rate limiting: PASSED ({rate_limited_count} requests blocked)")
                return True
            else:
                self.test_results['rate_limiting']['failed'] += 1
                print(f"  ❌ Rate limiting: FAILED (no requests blocked)")
                return False
                
        except Exception as e:
            self.test_results['rate_limiting']['failed'] += 1
            self.test_results['rate_limiting']['errors'].append(str(e))
            print(f"  💥 Rate limiting: ERROR - {e}")
            return False
    
    def test_caching(self):
        """Test caching functionality"""
        print("\n💾 Testing Caching...")
        
        tests = [
            ('cache_set_get', self._test_cache_set_get),
            ('cache_invalidation', self._test_cache_invalidation),
            ('query_caching', self._test_query_caching),
        ]
        
        for test_name, test_func in tests:
            try:
                result = test_func()
                if result:
                    self.test_results['caching']['passed'] += 1
                    print(f"  ✅ {test_name}: PASSED")
                else:
                    self.test_results['caching']['failed'] += 1
                    print(f"  ❌ {test_name}: FAILED")
            except Exception as e:
                self.test_results['caching']['failed'] += 1
                self.test_results['caching']['errors'].append(f"{test_name}: {e}")
                print(f"  💥 {test_name}: ERROR - {e}")
    
    def _test_cache_set_get(self):
        """Test basic cache operations"""
        cache_key = 'test_key'
        cache_value = {'test': 'data'}
        
        # Set cache
        cache.set(cache_key, cache_value, 60)
        
        # Get cache
        retrieved_value = cache.get(cache_key)
        
        return retrieved_value == cache_value
    
    def _test_cache_invalidation(self):
        """Test cache invalidation"""
        cache_key = 'test_invalidation'
        cache.set(cache_key, 'test_value', 60)
        
        # Invalidate
        cache.delete(cache_key)
        
        # Should be None now
        return cache.get(cache_key) is None
    
    def _test_query_caching(self):
        """Test query caching"""
        # Measure query count
        initial_queries = len(connection.queries)
        
        # Perform database operations
        User.objects.filter(user_type='client').count()
        
        # Check if queries were executed
        final_queries = len(connection.queries)
        
        return final_queries > initial_queries
    
    def test_analytics(self):
        """Test analytics functionality"""
        print("\n📊 Testing Analytics...")
        
        tests = [
            ('user_activity_tracking', self._test_user_activity_tracking),
            ('performance_metrics', self._test_performance_metrics),
            ('user_sessions', self._test_user_sessions),
            ('feature_usage', self._test_feature_usage),
            ('goal_tracking', self._test_goal_tracking),
        ]
        
        for test_name, test_func in tests:
            try:
                result = test_func()
                if result:
                    self.test_results['analytics']['passed'] += 1
                    print(f"  ✅ {test_name}: PASSED")
                else:
                    self.test_results['analytics']['failed'] += 1
                    print(f"  ❌ {test_name}: FAILED")
            except Exception as e:
                self.test_results['analytics']['failed'] += 1
                self.test_results['analytics']['errors'].append(f"{test_name}: {e}")
                print(f"  💥 {test_name}: ERROR - {e}")
    
    def _test_user_activity_tracking(self):
        """Test user activity tracking"""
        activity = UserActivity.objects.create(
            user=self.client_user,
            activity_type='login',
            metadata={'test': True}
        )
        
        return activity.id is not None
    
    def _test_performance_metrics(self):
        """Test performance metrics"""
        metric = PerformanceMetric.objects.create(
            user=self.client_user,
            metric_type='weight',
            value=70.5,
            unit='kg'
        )
        
        return metric.id is not None
    
    def _test_user_sessions(self):
        """Test user session tracking"""
        session = UserSession.objects.create(
            user=self.client_user,
            session_id='test_session_123',
            ip_address='127.0.0.1'
        )
        
        return session.is_active
    
    def _test_feature_usage(self):
        """Test feature usage tracking"""
        usage = FeatureUsage.objects.create(
            user=self.client_user,
            feature_name='diet_plan_generation'
        )
        
        return usage.id is not None
    
    def _test_goal_tracking(self):
        """Test goal tracking"""
        goal = UserGoal.objects.create(
            user=self.client_user,
            goal_type='weight_loss',
            title='Lose 10kg',
            target_value=10,
            unit='kg'
        )
        
        # Test progress update
        goal.update_progress(5)
        
        return goal.progress_percentage == 50
    
    def test_social_features(self):
        """Test social features"""
        print("\n👥 Testing Social Features...")
        
        tests = [
            ('user_following', self._test_user_following),
            ('posts_and_likes', self._test_posts_and_likes),
            ('comments', self._test_comments),
            ('challenges', self._test_challenges),
            ('achievements', self._test_achievements),
        ]
        
        for test_name, test_func in tests:
            try:
                result = test_func()
                if result:
                    self.test_results['social_features']['passed'] += 1
                    print(f"  ✅ {test_name}: PASSED")
                else:
                    self.test_results['social_features']['failed'] += 1
                    print(f"  ❌ {test_name}: FAILED")
            except Exception as e:
                self.test_results['social_features']['failed'] += 1
                self.test_results['social_features']['errors'].append(f"{test_name}: {e}")
                print(f"  💥 {test_name}: ERROR - {e}")
    
    def _test_user_following(self):
        """Test user following functionality"""
        follow = UserFollow.objects.create(
            follower=self.client_user,
            following=self.trainer_user
        )
        
        return follow.id is not None
    
    def _test_posts_and_likes(self):
        """Test posts and likes"""
        post = Post.objects.create(
            author=self.trainer_user,
            post_type='workout',
            content='Great workout today!',
            visibility='public'
        )
        
        like = PostLike.objects.create(
            user=self.client_user,
            post=post
        )
        
        return post.id is not None and like.id is not None
    
    def _test_comments(self):
        """Test comment functionality"""
        post = Post.objects.create(
            author=self.trainer_user,
            content='Test post',
            visibility='public'
        )
        
        comment = Comment.objects.create(
            author=self.client_user,
            post=post,
            content='Great post!'
        )
        
        return comment.id is not None
    
    def _test_challenges(self):
        """Test challenge functionality"""
        challenge = Challenge.objects.create(
            title='30-Day Fitness Challenge',
            description='Complete 30 workouts in 30 days',
            challenge_type='workout',
            creator=self.trainer_user,
            start_date=timezone.now(),
            end_date=timezone.now() + timezone.timedelta(days=30)
        )
        
        return challenge.id is not None
    
    def _test_achievements(self):
        """Test achievement system"""
        achievement = Achievement.objects.create(
            name='First Workout',
            description='Complete your first workout',
            category='workout',
            points=10
        )
        
        user_achievement = UserAchievement.objects.create(
            user=self.client_user,
            achievement=achievement
        )
        
        return user_achievement.id is not None
    
    def test_performance_optimizations(self):
        """Test performance optimizations"""
        print("\n⚡ Testing Performance Optimizations...")
        
        tests = [
            ('database_indexes', self._test_database_indexes),
            ('query_optimization', self._test_query_optimization),
            ('response_times', self._test_response_times),
        ]
        
        for test_name, test_func in tests:
            try:
                result = test_func()
                if result:
                    self.test_results['performance']['passed'] += 1
                    print(f"  ✅ {test_name}: PASSED")
                else:
                    self.test_results['performance']['failed'] += 1
                    print(f"  ❌ {test_name}: FAILED")
            except Exception as e:
                self.test_results['performance']['failed'] += 1
                self.test_results['performance']['errors'].append(f"{test_name}: {e}")
                print(f"  💥 {test_name}: ERROR - {e}")
    
    def _test_database_indexes(self):
        """Test database indexes are working"""
        # Check if indexes exist on key fields
        from django.db import connection
        cursor = connection.cursor()
        
        # This is a simplified test - in reality you'd check actual index usage
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = cursor.fetchall()
        
        return len(indexes) > 0
    
    def _test_query_optimization(self):
        """Test query optimization"""
        initial_queries = len(connection.queries)
        
        # Perform an optimized query with select_related
        users = list(User.objects.select_related().all()[:5])
        
        final_queries = len(connection.queries)
        query_count = final_queries - initial_queries
        
        # Should not generate too many queries
        return query_count <= 5
    
    def _test_response_times(self):
        """Test API response times"""
        start_time = time.time()
        
        # Make a simple API request
        response = self.client.get('/api/users/profile/')
        
        end_time = time.time()
        response_time = end_time - start_time
        
        # Response should be under 1 second
        return response_time < 1.0
    
    def run_all_tests(self):
        """Run all test suites"""
        print("🚀 Starting Comprehensive Feature Test Suite")
        print("=" * 60)
        
        # Run all test categories
        self.test_input_validation()
        self.test_file_security()
        self.test_rate_limiting()
        self.test_caching()
        self.test_analytics()
        self.test_social_features()
        self.test_performance_optimizations()
        
        # Print results summary
        self.print_results_summary()
    
    def print_results_summary(self):
        """Print comprehensive test results"""
        print("\n" + "=" * 60)
        print("📋 COMPREHENSIVE TEST RESULTS SUMMARY")
        print("=" * 60)
        
        total_passed = 0
        total_failed = 0
        total_errors = 0
        
        for category, results in self.test_results.items():
            passed = results['passed']
            failed = results['failed']
            errors = len(results['errors'])
            total = passed + failed
            
            total_passed += passed
            total_failed += failed
            total_errors += errors
            
            if total > 0:
                success_rate = (passed / total) * 100
            else:
                success_rate = 0
            
            status_icon = "✅" if success_rate >= 80 else "⚠️" if success_rate >= 60 else "❌"
            
            print(f"{status_icon} {category.replace('_', ' ').title()}: {passed}/{total} passed ({success_rate:.1f}%)")
            
            if errors > 0:
                print(f"    💥 {errors} errors occurred")
                for error in results['errors'][:3]:  # Show first 3 errors
                    print(f"       - {error}")
        
        print("\n" + "-" * 60)
        overall_total = total_passed + total_failed
        if overall_total > 0:
            overall_success = (total_passed / overall_total) * 100
        else:
            overall_success = 0
        
        print(f"🎯 OVERALL RESULTS: {total_passed}/{overall_total} tests passed ({overall_success:.1f}%)")
        
        if total_errors > 0:
            print(f"💥 Total errors: {total_errors}")
        
        # Final assessment
        if overall_success >= 90:
            print("🏆 EXCELLENT! All major features are working correctly.")
        elif overall_success >= 75:
            print("✅ GOOD! Most features are working with minor issues.")
        elif overall_success >= 50:
            print("⚠️ MODERATE! Some features need attention.")
        else:
            print("❌ CRITICAL! Major issues detected. Review implementation.")
        
        print("=" * 60)


def main():
    """Main function to run the comprehensive test suite"""
    try:
        # Initialize and run tests
        test_suite = ComprehensiveFeatureTestSuite()
        test_suite.run_all_tests()
        
    except Exception as e:
        print(f"\n💥 CRITICAL ERROR: Test suite failed to run: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main() 