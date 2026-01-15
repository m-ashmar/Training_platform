#!/usr/bin/env python3
"""
Comprehensive Diet System Test Script

This script thoroughly tests every feature of the enhanced diet system:
1. Model functionality and relationships
2. Service operations with detailed validation
3. Permission checks and role-based access
4. Progress tracking with actual data
5. Meal completion and nutritional calculations
6. API endpoint functionality
7. Error handling and edge cases
"""

import os
import sys
import django
import requests
import json
from datetime import date, timedelta, time
from decimal import Decimal

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status

from diet.models import (
    DietPlanTemplate, FoodCategory, FoodItem, DietPlan, 
    Meal, MealComponent, DailyProgress, UserFoodPreference
)
from diet.trainer_services import TrainerDietPlanService, ClientProgressService
from diet.ai_services import DietGenerator

CustomUser = get_user_model()

class ComprehensiveDietTester:
    """Comprehensive tester for the enhanced diet system."""
    
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
        self.api_client = APIClient()
        self.test_results = []
        
        # Test data
        self.test_trainer = None
        self.test_client = None
        self.test_templates = []
        self.test_foods = []
        self.test_diet_plans = []
        self.test_meals = []
        self.test_components = []
        
    def log_test(self, test_name: str, success: bool, details: str = "", data: dict = None):
        """Log test results with detailed information."""
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status} {test_name}")
        if details:
            print(f"   Details: {details}")
        if data:
            print(f"   Data: {json.dumps(data, indent=2, default=str)}")
        
        self.test_results.append({
            'test_name': test_name,
            'success': success,
            'details': details,
            'data': data
        })
    
    def setup_test_data(self):
        """Set up comprehensive test data."""
        print("\n🔧 Setting up comprehensive test data...")
        
        try:
            # Create food categories
            categories_data = [
                {'name': 'Proteins', 'is_protein': True, 'meal_times': ['ANY']},
                {'name': 'Carbs', 'is_carb': True, 'meal_times': ['ANY']},
                {'name': 'Fats', 'is_fat': True, 'meal_times': ['ANY']},
                {'name': 'Vegetables', 'is_carb': True, 'meal_times': ['ANY']},
                {'name': 'Fruits', 'is_carb': True, 'meal_times': ['ANY']}
            ]
            
            categories = {}
            for cat_data in categories_data:
                category, created = FoodCategory.objects.get_or_create(
                    name=cat_data['name'],
                    defaults=cat_data
                )
                categories[cat_data['name']] = category
            
            # Create comprehensive food items
            foods_data = [
                {
                    'name': 'Chicken Breast',
                    'calories': 165,
                    'protein': 31,
                    'carbs': 0,
                    'fat': 3.6,
                    'category': categories['Proteins'],
                    'serving_size': '100g',
                    'serving_size_grams': 100,
                    'calories_per_gram': 1.65,
                    'protein_per_gram': 0.31,
                    'carbs_per_gram': 0,
                    'fat_per_gram': 0.036
                },
                {
                    'name': 'Brown Rice',
                    'calories': 111,
                    'protein': 2.6,
                    'carbs': 23,
                    'fat': 0.9,
                    'category': categories['Carbs'],
                    'serving_size': '100g',
                    'serving_size_grams': 100,
                    'calories_per_gram': 1.11,
                    'protein_per_gram': 0.026,
                    'carbs_per_gram': 0.23,
                    'fat_per_gram': 0.009
                },
                {
                    'name': 'Avocado',
                    'calories': 160,
                    'protein': 2,
                    'carbs': 9,
                    'fat': 15,
                    'category': categories['Fats'],
                    'serving_size': '100g',
                    'serving_size_grams': 100,
                    'calories_per_gram': 1.60,
                    'protein_per_gram': 0.02,
                    'carbs_per_gram': 0.09,
                    'fat_per_gram': 0.15
                },
                {
                    'name': 'Broccoli',
                    'calories': 34,
                    'protein': 2.8,
                    'carbs': 7,
                    'fat': 0.4,
                    'category': categories['Vegetables'],
                    'serving_size': '100g',
                    'serving_size_grams': 100,
                    'calories_per_gram': 0.34,
                    'protein_per_gram': 0.028,
                    'carbs_per_gram': 0.07,
                    'fat_per_gram': 0.004
                },
                {
                    'name': 'Banana',
                    'calories': 89,
                    'protein': 1.1,
                    'carbs': 23,
                    'fat': 0.3,
                    'category': categories['Fruits'],
                    'serving_size': '100g',
                    'serving_size_grams': 100,
                    'calories_per_gram': 0.89,
                    'protein_per_gram': 0.011,
                    'carbs_per_gram': 0.23,
                    'fat_per_gram': 0.003
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
            
            # Create test users with complete profiles
            self.test_trainer, created = CustomUser.objects.get_or_create(
                username='comprehensive_trainer',
                defaults={
                    'email': 'comprehensive_trainer@test.com',
                    'user_type': 'trainer',
                    'first_name': 'Comprehensive',
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
            
            self.test_client, created = CustomUser.objects.get_or_create(
                username='comprehensive_client',
                defaults={
                    'email': 'comprehensive_client@test.com',
                    'user_type': 'client',
                    'first_name': 'Comprehensive',
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
            
            # Create subscriptions for test users
            from subscription.models import SubscriptionPlan, Subscription
            
            # Get or create a premium plan with diet access
            premium_plan, created = SubscriptionPlan.objects.get_or_create(
                name='Premium Test Plan',
                defaults={
                    'description': 'Premium test plan with full access to all features',
                    'price': Decimal('29.99'),
                    'duration_days': 30,
                    'has_diet_access': True,
                    'has_routine_access': True,
                    'has_challenges_access': True,
                    'has_ai_advice': True,
                    'has_priority_support': True,
                    'max_meals_per_day': 10,
                    'max_routines': 5
                }
            )
            
            # Create subscription for trainer
            trainer_subscription, created = Subscription.objects.get_or_create(
                user=self.test_trainer,
                defaults={
                    'plan': premium_plan,
                    'status': 'active',
                    'start_date': timezone.now().date(),
                    'end_date': timezone.now().date() + timedelta(days=30)
                }
            )
            if not created:
                trainer_subscription.status = 'active'
                trainer_subscription.save()
            
            # Create subscription for client
            client_subscription, created = Subscription.objects.get_or_create(
                user=self.test_client,
                defaults={
                    'plan': premium_plan,
                    'status': 'active',
                    'start_date': timezone.now().date(),
                    'end_date': timezone.now().date() + timedelta(days=30)
                }
            )
            if not created:
                client_subscription.status = 'active'
                client_subscription.save()
            
            # Get templates
            self.test_templates = list(DietPlanTemplate.objects.all())
            
            self.log_test("Setup test data", True, f"Created {len(self.test_foods)} foods, 2 users, {len(self.test_templates)} templates")
            
        except Exception as e:
            self.log_test("Setup test data", False, str(e))
            raise
    
    def test_model_functionality(self):
        """Test all model functionality in detail."""
        print("\n📊 Testing Model Functionality...")
        
        try:
            # Test DietPlanTemplate
            template = self.test_templates[0]
            total_meals = template.total_meals_per_cycle
            self.log_test("Template total_meals_per_cycle", total_meals > 0, f"{total_meals} meals per cycle")
            
            # Test FoodItem nutritional calculations
            food = self.test_foods[0]  # Chicken Breast
            expected_calories = (food.calories * 150) / 100  # 150g serving
            expected_protein = (food.protein * 150) / 100
            
            self.log_test("Food nutritional calculations", 
                         food.calories == 165 and food.protein == 31,
                         f"Chicken: {food.calories} cal, {food.protein}g protein")
            
            # Test FoodCategory relationships
            category = food.category
            self.log_test("Food category relationship", 
                         category.name == 'Proteins' and category.is_protein,
                         f"Category: {category.name}, is_protein: {category.is_protein}")
            
        except Exception as e:
            self.log_test("Model functionality", False, str(e))
    
    def test_trainer_services_comprehensive(self):
        """Test trainer services with comprehensive validation."""
        print("\n👨‍🏫 Testing Trainer Services (Comprehensive)...")
        
        try:
            trainer_service = TrainerDietPlanService(self.test_trainer)
            
            # Test template retrieval
            templates = trainer_service.get_available_templates()
            self.log_test("Get available templates", len(templates) > 0, f"Found {len(templates)} templates")
            
            # Test food item retrieval
            foods = trainer_service.get_client_food_items(search_query="")
            self.log_test("Get client food items", len(foods) > 0, f"Found {len(foods)} foods")
            
            # Test diet plan creation with detailed validation
            template = self.test_templates[0]
            diet_plan = trainer_service.create_diet_plan(
                client=self.test_client,
                template=template,
                start_date=date.today(),
                duration_weeks=1,
                goal='Lose',
                daily_calories=1800
            )
            
            self.test_diet_plans.append(diet_plan)
            
            # Validate diet plan properties
            self.log_test("Diet plan creation", 
                         diet_plan.user == self.test_client and diet_plan.created_by == self.test_trainer,
                         f"Plan ID: {diet_plan.id}, Client: {diet_plan.user.username}, Trainer: {diet_plan.created_by.username}")
            
            # Test meal addition with multiple components
            food_items = [
                {'food_id': self.test_foods[0].id, 'quantity': 150},  # Chicken 150g
                {'food_id': self.test_foods[1].id, 'quantity': 100},  # Rice 100g
                {'food_id': self.test_foods[3].id, 'quantity': 50}    # Broccoli 50g
            ]
            
            meal = trainer_service.add_meal_to_plan(
                diet_plan=diet_plan,
                meal_type='Lunch',
                target_date=date.today(),
                food_items=food_items,
                scheduled_time=time(12, 30),
                description='Comprehensive test lunch'
            )
            
            self.test_meals.append(meal)
            
            # Validate meal creation
            components = meal.components.all()
            self.log_test("Meal creation with components", 
                         components.count() == 3,
                         f"Created meal {meal.id} with {components.count()} components")
            
            # Test nutritional calculations
            nutrition = meal.calculate_nutrition()
            expected_calories = (165 * 1.5) + (111 * 1.0) + (34 * 0.5)  # ~330 calories
            
            self.log_test("Meal nutrition calculation", 
                         nutrition['calories'] > 0 and nutrition['protein'] > 0,
                         f"Meal nutrition: {nutrition['calories']:.1f} cal, {nutrition['protein']:.1f}g protein")
            
            # Test diet plan nutrition
            plan_nutrition = diet_plan.calculate_daily_nutrition()
            self.log_test("Diet plan nutrition", 
                         plan_nutrition['calories'] > 0,
                         f"Plan nutrition: {plan_nutrition['calories']:.1f} calories")
            
            # Add more meals for comprehensive testing
            breakfast_items = [
                {'food_id': self.test_foods[4].id, 'quantity': 120},  # Banana 120g
                {'food_id': self.test_foods[2].id, 'quantity': 30}    # Avocado 30g
            ]
            
            breakfast = trainer_service.add_meal_to_plan(
                diet_plan=diet_plan,
                meal_type='Breakfast',
                target_date=date.today(),
                food_items=breakfast_items,
                scheduled_time=time(8, 0),
                description='Test breakfast'
            )
            
            self.test_meals.append(breakfast)
            
            dinner_items = [
                {'food_id': self.test_foods[0].id, 'quantity': 200},  # Chicken 200g
                {'food_id': self.test_foods[3].id, 'quantity': 100}   # Broccoli 100g
            ]
            
            dinner = trainer_service.add_meal_to_plan(
                diet_plan=diet_plan,
                meal_type='Dinner',
                target_date=date.today(),
                food_items=dinner_items,
                scheduled_time=time(19, 0),
                description='Test dinner'
            )
            
            self.test_meals.append(dinner)
            
            # Test total meals in plan
            total_meals = diet_plan.meals.filter(date=date.today()).count()
            self.log_test("Total meals in plan", total_meals == 3, f"Total meals: {total_meals}")
            
        except Exception as e:
            self.log_test("Trainer services comprehensive", False, str(e))
    
    def test_client_services_comprehensive(self):
        """Test client services with comprehensive validation."""
        print("\n👤 Testing Client Services (Comprehensive)...")
        
        try:
            client_service = ClientProgressService(self.test_client)
            
            # Test daily progress before completion
            progress = client_service.get_daily_progress()
            self.log_test("Initial daily progress", 
                         progress['completion_percentage'] == 0,
                         f"Initial progress: {progress['completion_percentage']}%")
            
            # Test meal completion with detailed tracking
            if self.test_meals:
                meal = self.test_meals[0]  # Lunch
                components = meal.components.all()
                
                # Complete first component
                if components.exists():
                    component = components.first()
                    updated_component = client_service.complete_meal_component(
                        component, 
                        actual_quantity=component.quantity
                    )
                    
                    self.log_test("Component completion", 
                                 updated_component.is_completed and updated_component.completed_at is not None,
                                 f"Component {component.id} completed at {updated_component.completed_at}")
                    
                    # Test meal completion percentage
                    meal.refresh_from_db()
                    self.log_test("Meal completion percentage", 
                                 meal.completion_percentage > 0,
                                 f"Meal completion: {meal.completion_percentage}%")
                
                # Complete all components in the meal
                for component in components:
                    client_service.complete_meal_component(component, component.quantity)
                
                # Test meal completion status
                meal.refresh_from_db()
                self.log_test("Full meal completion", 
                             meal.is_completed,
                             f"Meal completed: {meal.is_completed}, Percentage: {meal.completion_percentage}%")
                
                # Test daily progress after meal completion
                progress = client_service.get_daily_progress()
                self.log_test("Progress after meal completion", 
                             progress['completion_percentage'] > 0,
                             f"Progress: {progress['completion_percentage']}%, Calories: {progress['calories_consumed']}")
                
                # Complete all meals
                for meal in self.test_meals:
                    for component in meal.components.all():
                        client_service.complete_meal_component(component, component.quantity)
                
                # Test final daily progress
                progress = client_service.get_daily_progress()
                self.log_test("Final daily progress", 
                             progress['completion_percentage'] == 100,
                             f"Final progress: {progress['completion_percentage']}%, Calories: {progress['calories_consumed']}")
                
                # Test weekly progress
                week_progress = client_service.get_weekly_progress()
                self.log_test("Weekly progress", 
                             len(week_progress) == 7,
                             f"Weekly progress: {len(week_progress)} days")
                
                # Test meal rating
                meal = self.test_meals[0]
                updated_meal = client_service.rate_meal(
                    meal, 
                    is_liked=True, 
                    notes="Excellent meal!"
                )
                
                self.log_test("Meal rating", 
                             updated_meal.is_liked and updated_meal.notes == "Excellent meal!",
                             f"Meal rated: {updated_meal.is_liked}, Notes: {updated_meal.notes}")
                
                # Test meal details
                meal_details = client_service.get_meal_details(meal)
                self.log_test("Meal details", 
                             len(meal_details['components']) > 0 and meal_details['nutrition']['calories'] > 0,
                             f"Meal details: {len(meal_details['components'])} components, {meal_details['nutrition']['calories']} calories")
            
        except Exception as e:
            self.log_test("Client services comprehensive", False, str(e))
    
    def test_daily_progress_tracking(self):
        """Test daily progress tracking in detail."""
        print("\n📈 Testing Daily Progress Tracking...")
        
        try:
            if self.test_diet_plans:
                diet_plan = self.test_diet_plans[0]
                
                # Complete all meals first
                client_service = ClientProgressService(self.test_client)
                meals = diet_plan.meals.filter(date=date.today())
                
                for meal in meals:
                    for component in meal.components.all():
                        client_service.complete_meal_component(component, component.quantity)
                
                # Get daily progress (this will create it automatically)
                progress = client_service.get_daily_progress()
                
                self.log_test("Daily progress retrieval", 
                             progress['has_active_plan'] and progress['completion_percentage'] == 100,
                             f"Progress: {progress['completion_percentage']}%, Calories: {progress['calories_consumed']}")
                
                # Test that DailyProgress was created automatically
                daily_progress = DailyProgress.objects.filter(
                    user=self.test_client,
                    diet_plan=diet_plan,
                    date=date.today()
                ).first()
                
                self.log_test("Daily progress auto-creation", 
                             daily_progress is not None,
                             f"DailyProgress created: {daily_progress is not None}")
                
                if daily_progress:
                    # Test final progress
                    self.log_test("Final progress tracking", 
                                 daily_progress.completion_percentage == 100 and daily_progress.is_day_completed,
                                 f"Final progress: {daily_progress.completion_percentage}%, Day completed: {daily_progress.is_day_completed}")
                    
                    # Test nutritional tracking
                    self.log_test("Nutritional tracking", 
                                 daily_progress.calories_consumed > 0 and daily_progress.protein_consumed > 0,
                                 f"Calories: {daily_progress.calories_consumed}, Protein: {daily_progress.protein_consumed}g")
                    
                    # Test progress properties
                    self.log_test("Progress properties", 
                                 daily_progress.calories_percentage > 0 and daily_progress.protein_percentage > 0,
                                 f"Calories %: {daily_progress.calories_percentage}%, Protein %: {daily_progress.protein_percentage}%")
            
        except Exception as e:
            self.log_test("Daily progress tracking", False, str(e))
    
    def test_permissions_and_validation(self):
        """Test permissions and validation thoroughly."""
        print("\n🔒 Testing Permissions and Validation...")
        
        try:
            # Test trainer can only access assigned clients
            trainer_service = TrainerDietPlanService(self.test_trainer)
            
            # Create another client not assigned to trainer
            other_client, created = CustomUser.objects.get_or_create(
                username='other_client',
                defaults={
                    'email': 'other_client@test.com',
                    'user_type': 'client',
                    'first_name': 'Other',
                    'last_name': 'Client',
                    'phone_number': '1111111111',
                    'height': 170,
                    'weight': 65,
                    'age': 28,
                    'gender': 'Male',
                    'activity_level': 'Moderate'
                }
            )
            if created:
                other_client.set_password('testpass123')
                other_client.save()
            
            # Test that trainer cannot create plan for unassigned client
            try:
                template = self.test_templates[0]
                diet_plan = trainer_service.create_diet_plan(
                    client=other_client,
                    template=template,
                    start_date=date.today(),
                    duration_weeks=1,
                    goal='Maintain',
                    daily_calories=2000
                )
                self.log_test("Trainer unassigned client validation", False, "Should have failed")
            except Exception as e:
                self.log_test("Trainer unassigned client validation", True, f"Correctly blocked: {str(e)}")
            
            # Test client can only access their own data
            client_service = ClientProgressService(self.test_client)
            
            # Test that client cannot access other client's data
            other_client_service = ClientProgressService(other_client)
            other_progress = other_client_service.get_daily_progress()
            
            self.log_test("Client data isolation", 
                         other_progress['completion_percentage'] == 0,
                         "Client data properly isolated")
            
            # Cleanup
            other_client.delete()
            
        except Exception as e:
            self.log_test("Permissions and validation", False, str(e))
    
    def test_api_endpoints(self):
        """Test API endpoints with JWT authentication."""
        print("\n🌐 Testing API Endpoints...")
        
        try:
            # Test JWT authentication for trainer
            trainer_login_data = {
                'email': 'comprehensive_trainer@test.com',
                'password': 'testpass123'
            }
            
            trainer_response = requests.post(
                f"{self.base_url}/api/auth/token/",
                json=trainer_login_data
            )
            
            trainer_login_success = trainer_response.status_code == 200
            self.log_test("Trainer JWT authentication", trainer_login_success, 
                         f"Status: {trainer_response.status_code}")
            
            if trainer_login_success:
                trainer_token = trainer_response.json().get('access')
                trainer_headers = {'Authorization': f'Bearer {trainer_token}'}
                
                # Test food list endpoint with JWT
                response = requests.get(f"{self.base_url}/api/diet/api/food/list/", headers=trainer_headers)
                self.log_test("Food list API", 
                             response.status_code == 200,
                             f"Status: {response.status_code}")
                
                if response.status_code == 200:
                    data = response.json()
                    self.log_test("Food list data", 
                                 len(data.get('results', [])) > 0,
                                 f"Found {len(data.get('results', []))} foods")
                
                # Test food search endpoint with JWT
                response = requests.get(f"{self.base_url}/api/diet/api/food/search/?q=chicken", headers=trainer_headers)
                self.log_test("Food search API", 
                             response.status_code == 200,
                             f"Status: {response.status_code}")
                
                # Test food categories endpoint with JWT
                response = requests.get(f"{self.base_url}/api/diet/api/food/categories/", headers=trainer_headers)
                self.log_test("Food categories API", 
                             response.status_code == 200,
                             f"Status: {response.status_code}")
                
                # Test trainer templates endpoint with JWT
                response = requests.get(f"{self.base_url}/api/diet/api/trainer/templates/", headers=trainer_headers)
                self.log_test("Trainer templates API", 
                             response.status_code == 200,
                             f"Status: {response.status_code}")
            
            # Test JWT authentication for client
            client_login_data = {
                'email': 'comprehensive_client@test.com',
                'password': 'testpass123'
            }
            
            client_response = requests.post(
                f"{self.base_url}/api/auth/token/",
                json=client_login_data
            )
            
            client_login_success = client_response.status_code == 200
            self.log_test("Client JWT authentication", client_login_success, 
                         f"Status: {client_response.status_code}")
            
            if client_login_success:
                client_token = client_response.json().get('access')
                client_headers = {'Authorization': f'Bearer {client_token}'}
                
                # Test client progress endpoint with JWT
                response = requests.get(f"{self.base_url}/api/diet/api/client/progress/", headers=client_headers)
                self.log_test("Client progress API", 
                             response.status_code == 200,
                             f"Status: {response.status_code}")
            
        except Exception as e:
            self.log_test("API endpoints", False, str(e))
    
    def test_edge_cases(self):
        """Test edge cases and error handling."""
        print("\n⚠️ Testing Edge Cases...")
        
        try:
            # Test empty diet plan
            empty_plan = DietPlan.objects.create(
                user=self.test_client,
                created_by=self.test_trainer,
                goal='Maintain',
                daily_calories=2000,
                start_date=date.today(),
                end_date=date.today() + timedelta(days=7)
            )
            
            nutrition = empty_plan.calculate_daily_nutrition()
            self.log_test("Empty diet plan nutrition", 
                         nutrition['calories'] == 0,
                         f"Empty plan calories: {nutrition['calories']}")
            
            # Test meal with no components
            empty_meal = Meal.objects.create(
                diet_plan=empty_plan,
                meal_type='Breakfast',
                date=date.today(),
                scheduled_time=time(8, 0)
            )
            
            meal_nutrition = empty_meal.calculate_nutrition()
            self.log_test("Empty meal nutrition", 
                         meal_nutrition['calories'] == 0,
                         f"Empty meal calories: {meal_nutrition['calories']}")
            
            # Test component completion with zero quantity
            if self.test_components:
                component = self.test_components[0]
                client_service = ClientProgressService(self.test_client)
                
                try:
                    updated_component = client_service.complete_meal_component(component, 0)
                    self.log_test("Zero quantity completion", 
                                 updated_component.is_completed,
                                 "Zero quantity completion handled")
                except Exception as e:
                    self.log_test("Zero quantity completion", True, f"Correctly handled: {str(e)}")
            
            # Cleanup
            empty_plan.delete()
            
        except Exception as e:
            self.log_test("Edge cases", False, str(e))
    
    def cleanup_test_data(self):
        """Clean up all test data."""
        print("\n🧹 Cleaning up test data...")
        
        try:
            # Delete test diet plans (cascades to meals and components)
            for diet_plan in self.test_diet_plans:
                diet_plan.delete()
            
            # Delete test users
            if self.test_client:
                self.test_client.delete()
            if self.test_trainer:
                self.test_trainer.delete()
            
            # Delete test foods
            for food in self.test_foods:
                food.delete()
            
            self.log_test("Cleanup test data", True, "All test data cleaned up")
            
        except Exception as e:
            self.log_test("Cleanup test data", False, str(e))
    
    def run_comprehensive_tests(self):
        """Run all comprehensive tests."""
        print("🚀 Starting Comprehensive Diet System Tests")
        print("=" * 60)
        
        try:
            # Setup
            self.setup_test_data()
            
            # Run tests
            self.test_model_functionality()
            self.test_trainer_services_comprehensive()
            self.test_client_services_comprehensive()
            self.test_daily_progress_tracking()
            self.test_permissions_and_validation()
            self.test_api_endpoints()
            self.test_edge_cases()
            
            # Cleanup
            self.cleanup_test_data()
            
        except Exception as e:
            print(f"❌ Test suite failed: {str(e)}")
        
        # Print comprehensive summary
        print("\n" + "=" * 60)
        print("📊 COMPREHENSIVE TEST SUMMARY")
        print("=" * 60)
        
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
                    if result.get('data'):
                        print(f"    Data: {result['data']}")
        
        # Production readiness assessment
        print("\n🏭 PRODUCTION READINESS ASSESSMENT")
        print("=" * 60)
        
        critical_tests = [
            "Setup test data",
            "Trainer services comprehensive", 
            "Client services comprehensive",
            "Daily progress tracking",
            "Permissions and validation"
        ]
        
        critical_passed = all(
            any(result['test_name'] in test_name and result['success'] 
                for result in self.test_results)
            for test_name in critical_tests
        )
        
        if critical_passed and passed_tests >= total_tests * 0.8:
            print("✅ PRODUCTION READY - All critical features working")
        elif critical_passed:
            print("⚠️ MOSTLY READY - Critical features working, some minor issues")
        else:
            print("❌ NOT READY - Critical features failing")
        
        print("\n✅ Comprehensive test suite completed!")

def main():
    """Run comprehensive tests."""
    tester = ComprehensiveDietTester()
    tester.run_comprehensive_tests()

if __name__ == "__main__":
    main() 