#!/usr/bin/env python3
"""
Enhanced Diet System Test Script

This script tests all the new functionality added to the diet system:
1. AI Diet Plan Generation (with meal count and snack preferences)
2. Trainer Diet Plan Management
3. Client Progress Tracking
4. Meal Interactions and Completion
5. Template Management
6. Food Search and Import

Run this script after starting the Django server to test all features.
"""

import os
import sys
import django
import requests
import json
from datetime import date, timedelta, time
from typing import Dict, Any, List

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone
from diet.models import (
    DietPlanTemplate, FoodCategory, FoodItem, DietPlan, 
    Meal, MealComponent, DailyProgress
)
from diet.trainer_services import TrainerDietPlanService, ClientProgressService
from diet.ai_services import DietGenerator

CustomUser = get_user_model()

class DietSystemTester:
    """Comprehensive tester for the enhanced diet system."""
    
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
        self.session = requests.Session()
        self.test_results = []
        
        # Test data
        self.test_trainer = None
        self.test_client = None
        self.test_templates = []
        self.test_foods = []
        self.test_diet_plans = []
        
    def log_test(self, test_name: str, success: bool, details: str = ""):
        """Log test results."""
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} {test_name}")
        if details:
            print(f"   Details: {details}")
        
        self.test_results.append({
            'test_name': test_name,
            'success': success,
            'details': details
        })
    
    def create_test_users(self):
        """Create test users for testing."""
        print("\n🔧 Creating test users...")
        
        try:
            # Create test trainer
            self.test_trainer, created = CustomUser.objects.get_or_create(
                username='test_trainer',
                defaults={
                    'email': 'trainer@test.com',
                    'user_type': 'trainer',
                    'first_name': 'Test',
                    'last_name': 'Trainer',
                    'phone_number': '1234567890',
                    'height': 175,
                    'weight': 70,
                    'age': 30,
                    'gender': 'Male',
                    'activity_level': 'Moderate'
                }
            )
            if created:
                self.test_trainer.set_password('testpass123')
                self.test_trainer.save()
            
            # Create test client
            self.test_client, created = CustomUser.objects.get_or_create(
                username='test_client',
                defaults={
                    'email': 'client@test.com',
                    'user_type': 'client',
                    'first_name': 'Test',
                    'last_name': 'Client',
                    'phone_number': '0987654321',
                    'height': 165,
                    'weight': 60,
                    'age': 25,
                    'gender': 'Female',
                    'activity_level': 'Light',
                    'assigned_trainer': self.test_trainer
                }
            )
            if created:
                self.test_client.set_password('testpass123')
                self.test_client.save()
            
            self.log_test("Create test users", True, f"Trainer: {self.test_trainer.username}, Client: {self.test_client.username}")
            
        except Exception as e:
            self.log_test("Create test users", False, str(e))
    
    def create_test_templates(self):
        """Create test diet plan templates."""
        print("\n🔧 Creating test templates...")
        
        try:
            templates_data = [
                {
                    'name': '3 Meals + 1 Snack',
                    'description': 'Standard 3 meals with 1 snack',
                    'meals_per_day': 3,
                    'snacks_per_day': 1,
                    'days_variation': 1
                },
                {
                    'name': '4 Meals + 2 Snacks',
                    'description': '4 meals with 2 snacks for active users',
                    'meals_per_day': 4,
                    'snacks_per_day': 2,
                    'days_variation': 2
                },
                {
                    'name': '5 Meals + 1 Snack',
                    'description': '5 meals with 1 snack for muscle building',
                    'meals_per_day': 5,
                    'snacks_per_day': 1,
                    'days_variation': 3
                }
            ]
            
            for template_data in templates_data:
                template, created = DietPlanTemplate.objects.get_or_create(
                    name=template_data['name'],
                    defaults=template_data
                )
                self.test_templates.append(template)
            
            self.log_test("Create test templates", True, f"Created {len(self.test_templates)} templates")
            
        except Exception as e:
            self.log_test("Create test templates", False, str(e))
    
    def create_test_foods(self):
        """Create test food items."""
        print("\n🔧 Creating test foods...")
        
        try:
            # Create food categories
            categories_data = [
                {'name': 'Proteins', 'is_protein': True},
                {'name': 'Carbs', 'is_carb': True},
                {'name': 'Fats', 'is_fat': True},
                {'name': 'Vegetables', 'is_carb': True},
                {'name': 'Fruits', 'is_carb': True}
            ]
            
            categories = {}
            for cat_data in categories_data:
                category, created = FoodCategory.objects.get_or_create(
                    name=cat_data['name'],
                    defaults=cat_data
                )
                categories[cat_data['name']] = category
            
            # Create food items
            foods_data = [
                {
                    'name': 'Chicken Breast',
                    'calories': 165,
                    'protein': 31,
                    'carbs': 0,
                    'fat': 3.6,
                    'category': categories['Proteins'],
                    'serving_size': '100g',
                    'serving_size_grams': 100
                },
                {
                    'name': 'Brown Rice',
                    'calories': 111,
                    'protein': 2.6,
                    'carbs': 23,
                    'fat': 0.9,
                    'category': categories['Carbs'],
                    'serving_size': '100g',
                    'serving_size_grams': 100
                },
                {
                    'name': 'Avocado',
                    'calories': 160,
                    'protein': 2,
                    'carbs': 9,
                    'fat': 15,
                    'category': categories['Fats'],
                    'serving_size': '100g',
                    'serving_size_grams': 100
                },
                {
                    'name': 'Broccoli',
                    'calories': 34,
                    'protein': 2.8,
                    'carbs': 7,
                    'fat': 0.4,
                    'category': categories['Vegetables'],
                    'serving_size': '100g',
                    'serving_size_grams': 100
                },
                {
                    'name': 'Banana',
                    'calories': 89,
                    'protein': 1.1,
                    'carbs': 23,
                    'fat': 0.3,
                    'category': categories['Fruits'],
                    'serving_size': '100g',
                    'serving_size_grams': 100
                }
            ]
            
            for food_data in foods_data:
                food, created = FoodItem.objects.get_or_create(
                    name=food_data['name'],
                    defaults={
                        **food_data,
                        'api_id': f"test_{food_data['name'].lower().replace(' ', '_')}"
                    }
                )
                self.test_foods.append(food)
            
            self.log_test("Create test foods", True, f"Created {len(self.test_foods)} food items")
            
        except Exception as e:
            self.log_test("Create test foods", False, str(e))
    
    def test_ai_diet_generation(self):
        """Test AI diet plan generation."""
        print("\n🤖 Testing AI Diet Plan Generation...")
        
        try:
            # Test AI generation for client
            generator = DietGenerator(self.test_client)
            
            # Test with different meal configurations
            test_configs = [
                {'meal_count': 3, 'snack_count': 0},
                {'meal_count': 3, 'snack_count': 1},
                {'meal_count': 4, 'snack_count': 2}
            ]
            
            for config in test_configs:
                try:
                    plan_output = generator.generate_plan(
                        meal_count=config['meal_count'],
                        snack_count=config['snack_count']
                    )
                    
                    # Save to database
                    diet_plan = generator.save_plan_to_database(
                        plan_output, 
                        config['meal_count'], 
                        config['snack_count']
                    )
                    
                    self.test_diet_plans.append(diet_plan)
                    
                    self.log_test(
                        f"AI Generation ({config['meal_count']}M + {config['snack_count']}S)",
                        True,
                        f"Generated {len(plan_output.plan)} meals"
                    )
                    
                except Exception as e:
                    self.log_test(
                        f"AI Generation ({config['meal_count']}M + {config['snack_count']}S)",
                        False,
                        str(e)
                    )
            
        except Exception as e:
            self.log_test("AI Diet Generation", False, str(e))
    
    def test_trainer_services(self):
        """Test trainer diet plan services."""
        print("\n👨‍🏫 Testing Trainer Services...")
        
        try:
            trainer_service = TrainerDietPlanService(self.test_trainer)
            
            # Test template retrieval
            templates = trainer_service.get_available_templates()
            self.log_test("Get available templates", len(templates) > 0, f"Found {len(templates)} templates")
            
            # Test food item retrieval
            foods = trainer_service.get_client_food_items(search_query="chicken")
            self.log_test("Search food items", len(foods) > 0, f"Found {len(foods)} foods")
            
            # Test diet plan creation
            template = self.test_templates[0]
            diet_plan = trainer_service.create_diet_plan(
                client=self.test_client,
                template=template,
                start_date=date.today(),
                duration_weeks=2,
                goal='Lose',
                daily_calories=1800
            )
            
            self.test_diet_plans.append(diet_plan)
            self.log_test("Create trainer diet plan", True, f"Created plan {diet_plan.id}")
            
            # Test meal addition
            food_items = [
                {'food_id': self.test_foods[0].id, 'quantity': 150},  # Chicken
                {'food_id': self.test_foods[1].id, 'quantity': 100},  # Rice
                {'food_id': self.test_foods[3].id, 'quantity': 50}    # Broccoli
            ]
            
            meal = trainer_service.add_meal_to_plan(
                diet_plan=diet_plan,
                meal_type='Lunch',
                target_date=date.today(),
                food_items=food_items,
                scheduled_time=time(12, 30),
                description='Healthy lunch with chicken and rice'
            )
            
            self.log_test("Add meal to plan", True, f"Added meal {meal.id} with {meal.components.count()} components")
            
            # Test nutrition summary
            nutrition = trainer_service.get_plan_nutrition_summary(diet_plan)
            self.log_test("Get nutrition summary", nutrition['total_calories'] > 0, f"{nutrition['total_calories']} calories")
            
        except Exception as e:
            self.log_test("Trainer Services", False, str(e))
    
    def test_client_services(self):
        """Test client progress services."""
        print("\n👤 Testing Client Services...")
        
        try:
            client_service = ClientProgressService(self.test_client)
            
            # Test daily progress
            progress = client_service.get_daily_progress()
            self.log_test("Get daily progress", True, f"Progress: {progress['completion_percentage']}%")
            
            # Test weekly progress
            week_progress = client_service.get_weekly_progress()
            self.log_test("Get weekly progress", len(week_progress) == 7, f"Got {len(week_progress)} days")
            
            # Test meal completion
            if self.test_diet_plans:
                diet_plan = self.test_diet_plans[0]
                meals = diet_plan.meals.all()
                
                if meals.exists():
                    meal = meals.first()
                    components = meal.components.all()
                    
                    if components.exists():
                        component = components.first()
                        
                        # Complete component
                        updated_component = client_service.complete_meal_component(
                            component, 
                            actual_quantity=component.quantity
                        )
                        
                        self.log_test("Complete meal component", updated_component.is_completed, "Component marked as completed")
                        
                        # Rate meal
                        updated_meal = client_service.rate_meal(
                            meal, 
                            is_liked=True, 
                            notes="Delicious meal!"
                        )
                        
                        self.log_test("Rate meal", updated_meal.is_liked, "Meal rated as liked")
                        
                        # Get meal details
                        meal_details = client_service.get_meal_details(meal)
                        self.log_test("Get meal details", len(meal_details['components']) > 0, f"Got {len(meal_details['components'])} components")
            
        except Exception as e:
            self.log_test("Client Services", False, str(e))
    
    def test_api_endpoints(self):
        """Test API endpoints."""
        print("\n🌐 Testing API Endpoints...")
        
        try:
            # Test food list endpoint
            response = self.session.get(f"{self.base_url}/diet/api/food/list/")
            self.log_test("Food list API", response.status_code == 200, f"Status: {response.status_code}")
            
            # Test food search endpoint
            response = self.session.get(f"{self.base_url}/diet/api/food/search/?q=chicken")
            self.log_test("Food search API", response.status_code == 200, f"Status: {response.status_code}")
            
            # Test food categories endpoint
            response = self.session.get(f"{self.base_url}/diet/api/food/categories/")
            self.log_test("Food categories API", response.status_code == 200, f"Status: {response.status_code}")
            
        except Exception as e:
            self.log_test("API Endpoints", False, str(e))
    
    def test_model_functionality(self):
        """Test model methods and properties."""
        print("\n📊 Testing Model Functionality...")
        
        try:
            if self.test_diet_plans:
                diet_plan = self.test_diet_plans[0]
                
                # Test diet plan properties
                self.log_test("Diet plan is_ai_generated", hasattr(diet_plan, 'is_ai_generated'), "Property exists")
                self.log_test("Diet plan is_trainer_created", hasattr(diet_plan, 'is_trainer_created'), "Property exists")
                
                # Test daily nutrition calculation
                nutrition = diet_plan.calculate_daily_nutrition()
                self.log_test("Daily nutrition calculation", nutrition['calories'] >= 0, f"{nutrition['calories']} calories")
                
                # Test meal nutrition calculation
                meals = diet_plan.meals.all()
                if meals.exists():
                    meal = meals.first()
                    meal_nutrition = meal.calculate_nutrition()
                    self.log_test("Meal nutrition calculation", meal_nutrition['calories'] >= 0, f"{meal_nutrition['calories']} calories")
                    
                    # Test meal completion properties
                    self.log_test("Meal is_completed property", hasattr(meal, 'is_completed'), "Property exists")
                    self.log_test("Meal completion_percentage", hasattr(meal, 'completion_percentage'), "Property exists")
                
                # Test template functionality
                if self.test_templates:
                    template = self.test_templates[0]
                    total_meals = template.total_meals_per_cycle
                    self.log_test("Template total_meals_per_cycle", total_meals > 0, f"{total_meals} meals per cycle")
            
        except Exception as e:
            self.log_test("Model Functionality", False, str(e))
    
    def test_daily_progress_tracking(self):
        """Test daily progress tracking functionality."""
        print("\n📈 Testing Daily Progress Tracking...")
        
        try:
            if self.test_diet_plans:
                diet_plan = self.test_diet_plans[0]
                
                # Create or get daily progress
                daily_progress, created = DailyProgress.objects.get_or_create(
                    user=self.test_client,
                    diet_plan=diet_plan,
                    date=date.today(),
                    defaults={
                        'target_calories': diet_plan.daily_calories,
                        'target_protein': (diet_plan.daily_calories * 0.30) / 4,
                        'target_carbs': (diet_plan.daily_calories * 0.50) / 4,
                        'target_fat': (diet_plan.daily_calories * 0.20) / 9
                    }
                )
                
                self.log_test("Create daily progress", True, f"Progress record {'created' if created else 'found'}")
                
                # Test progress update
                daily_progress.update_progress()
                self.log_test("Update progress", True, f"Progress updated: {daily_progress.completion_percentage}%")
                
                # Test progress properties
                completion_pct = daily_progress.completion_percentage
                calories_pct = daily_progress.calories_percentage
                
                self.log_test("Progress completion_percentage", completion_pct >= 0, f"{completion_pct}%")
                self.log_test("Progress calories_percentage", calories_pct >= 0, f"{calories_pct}%")
            
        except Exception as e:
            self.log_test("Daily Progress Tracking", False, str(e))
    
    def cleanup_test_data(self):
        """Clean up test data."""
        print("\n🧹 Cleaning up test data...")
        
        try:
            # Delete test diet plans
            for diet_plan in self.test_diet_plans:
                diet_plan.delete()
            
            # Delete test foods
            for food in self.test_foods:
                food.delete()
            
            # Delete test templates
            for template in self.test_templates:
                template.delete()
            
            # Delete test users
            if self.test_client:
                self.test_client.delete()
            if self.test_trainer:
                self.test_trainer.delete()
            
            self.log_test("Cleanup test data", True, "All test data cleaned up")
            
        except Exception as e:
            self.log_test("Cleanup test data", False, str(e))
    
    def run_all_tests(self):
        """Run all tests."""
        print("🚀 Starting Enhanced Diet System Tests")
        print("=" * 50)
        
        try:
            # Setup
            self.create_test_users()
            self.create_test_templates()
            self.create_test_foods()
            
            # Run tests
            self.test_ai_diet_generation()
            self.test_trainer_services()
            self.test_client_services()
            self.test_api_endpoints()
            self.test_model_functionality()
            self.test_daily_progress_tracking()
            
            # Cleanup
            self.cleanup_test_data()
            
        except Exception as e:
            print(f"❌ Test suite failed: {str(e)}")
        
        # Print summary
        print("\n" + "=" * 50)
        print("📊 TEST SUMMARY")
        print("=" * 50)
        
        total_tests = len(self.test_results)
        passed_tests = sum(1 for result in self.test_results if result['success'])
        failed_tests = total_tests - passed_tests
        
        print(f"Total Tests: {total_tests}")
        print(f"Passed: {passed_tests}")
        print(f"Failed: {failed_tests}")
        print(f"Success Rate: {(passed_tests/total_tests)*100:.1f}%" if total_tests > 0 else "No tests run")
        
        if failed_tests > 0:
            print("\n❌ Failed Tests:")
            for result in self.test_results:
                if not result['success']:
                    print(f"  - {result['test_name']}: {result['details']}")
        
        print("\n✅ Test suite completed!")

def main():
    """Main function to run the test suite."""
    tester = DietSystemTester()
    tester.run_all_tests()

if __name__ == "__main__":
    main() 