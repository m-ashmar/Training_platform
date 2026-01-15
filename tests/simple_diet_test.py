#!/usr/bin/env python3
"""
Simple Diet System Test Script

This script tests the core functionality of the enhanced diet system:
1. Model functionality
2. Service classes
3. Basic operations
"""

import os
import sys
import django
from datetime import date, timedelta, time

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

CustomUser = get_user_model()

def test_models():
    """Test model functionality."""
    print("🔧 Testing Models...")
    
    # Test DietPlanTemplate
    template = DietPlanTemplate.objects.first()
    if template:
        print(f"✅ Template: {template.name} - {template.total_meals_per_cycle} meals per cycle")
    
    # Test FoodCategory
    categories = FoodCategory.objects.all()
    print(f"✅ Food Categories: {categories.count()} categories")
    
    # Test FoodItem
    foods = FoodItem.objects.all()
    print(f"✅ Food Items: {foods.count()} items")
    
    if foods.exists():
        food = foods.first()
        print(f"✅ Food Item: {food.name} - {food.calories} calories")

def test_trainer_services():
    """Test trainer services."""
    print("\n👨‍🏫 Testing Trainer Services...")
    
    # Create a test trainer
    trainer, created = CustomUser.objects.get_or_create(
        username='test_trainer_services',
        defaults={
            'email': 'trainer_services@test.com',
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
        trainer.set_password('testpass123')
        trainer.save()
    
    # Create a test client
    client, created = CustomUser.objects.get_or_create(
        username='test_client_services',
        defaults={
            'email': 'client_services@test.com',
            'user_type': 'client',
            'first_name': 'Test',
            'last_name': 'Client',
            'phone_number': '0987654321',
            'height': 165,
            'weight': 60,
            'age': 25,
            'gender': 'Female',
            'activity_level': 'Light',
            'assigned_trainer': trainer
        }
    )
    if created:
        client.set_password('testpass123')
        client.save()
    
    try:
        trainer_service = TrainerDietPlanService(trainer)
        
        # Test template retrieval
        templates = trainer_service.get_available_templates()
        print(f"✅ Available templates: {templates.count()}")
        
        # Test food item retrieval
        foods = trainer_service.get_client_food_items(search_query="")
        print(f"✅ Available foods: {len(foods)}")
        
        # Test diet plan creation
        template = DietPlanTemplate.objects.first()
        if template:
            diet_plan = trainer_service.create_diet_plan(
                client=client,
                template=template,
                start_date=date.today(),
                duration_weeks=1,
                goal='Maintain',
                daily_calories=1800
            )
            print(f"✅ Created diet plan: {diet_plan.id}")
            
            # Test meal addition
            if foods:
                food_items = [
                    {'food_id': foods[0].id, 'quantity': 150},
                    {'food_id': foods[1].id, 'quantity': 100} if len(foods) > 1 else {'food_id': foods[0].id, 'quantity': 100}
                ]
                
                meal = trainer_service.add_meal_to_plan(
                    diet_plan=diet_plan,
                    meal_type='Lunch',
                    target_date=date.today(),
                    food_items=food_items,
                    scheduled_time=time(12, 30),
                    description='Test meal'
                )
                print(f"✅ Added meal: {meal.id} with {meal.components.count()} components")
                
                # Test nutrition calculation
                nutrition = meal.calculate_nutrition()
                print(f"✅ Meal nutrition: {nutrition['calories']} calories")
                
                # Test diet plan nutrition
                plan_nutrition = diet_plan.calculate_daily_nutrition()
                print(f"✅ Plan nutrition: {plan_nutrition['calories']} calories")
            
            # Cleanup
            diet_plan.delete()
        
    except Exception as e:
        print(f"❌ Trainer services error: {str(e)}")
    
    # Cleanup users
    client.delete()
    trainer.delete()

def test_client_services():
    """Test client services."""
    print("\n👤 Testing Client Services...")
    
    # Create a test client
    client, created = CustomUser.objects.get_or_create(
        username='test_client_progress',
        defaults={
            'email': 'client_progress@test.com',
            'user_type': 'client',
            'first_name': 'Test',
            'last_name': 'Client',
            'phone_number': '0987654321',
            'height': 165,
            'weight': 60,
            'age': 25,
            'gender': 'Female',
            'activity_level': 'Light'
        }
    )
    if created:
        client.set_password('testpass123')
        client.save()
    
    try:
        client_service = ClientProgressService(client)
        
        # Test daily progress (no active plan)
        progress = client_service.get_daily_progress()
        print(f"✅ Daily progress: {progress['completion_percentage']}% (no active plan)")
        
        # Test weekly progress
        week_progress = client_service.get_weekly_progress()
        print(f"✅ Weekly progress: {len(week_progress)} days")
        
    except Exception as e:
        print(f"❌ Client services error: {str(e)}")
    
    # Cleanup
    client.delete()

def test_meal_completion():
    """Test meal completion functionality."""
    print("\n🍽️ Testing Meal Completion...")
    
    # Create test data
    trainer, created = CustomUser.objects.get_or_create(
        username='test_meal_trainer',
        defaults={
            'email': 'meal_trainer@test.com',
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
        trainer.set_password('testpass123')
        trainer.save()
    
    client, created = CustomUser.objects.get_or_create(
        username='test_meal_client',
        defaults={
            'email': 'meal_client@test.com',
            'user_type': 'client',
            'first_name': 'Test',
            'last_name': 'Client',
            'phone_number': '0987654321',
            'height': 165,
            'weight': 60,
            'age': 25,
            'gender': 'Female',
            'activity_level': 'Light',
            'assigned_trainer': trainer
        }
    )
    if created:
        client.set_password('testpass123')
        client.save()
    
    try:
        # Create a diet plan
        trainer_service = TrainerDietPlanService(trainer)
        template = DietPlanTemplate.objects.first()
        
        if template:
            diet_plan = trainer_service.create_diet_plan(
                client=client,
                template=template,
                start_date=date.today(),
                duration_weeks=1,
                goal='Maintain',
                daily_calories=1800
            )
            
            # Add a meal
            foods = FoodItem.objects.all()[:2]
            if len(foods) >= 2:
                food_items = [
                    {'food_id': foods[0].id, 'quantity': 150},
                    {'food_id': foods[1].id, 'quantity': 100}
                ]
                
                meal = trainer_service.add_meal_to_plan(
                    diet_plan=diet_plan,
                    meal_type='Lunch',
                    target_date=date.today(),
                    food_items=food_items,
                    scheduled_time=time(12, 30),
                    description='Test meal for completion'
                )
                
                # Test meal completion
                client_service = ClientProgressService(client)
                components = meal.components.all()
                
                if components.exists():
                    component = components.first()
                    
                    # Complete component
                    updated_component = client_service.complete_meal_component(
                        component, 
                        actual_quantity=component.quantity
                    )
                    print(f"✅ Component completed: {updated_component.is_completed}")
                    
                    # Rate meal
                    updated_meal = client_service.rate_meal(
                        meal, 
                        is_liked=True, 
                        notes="Great meal!"
                    )
                    print(f"✅ Meal rated: {updated_meal.is_liked}")
                    
                    # Check meal completion status
                    print(f"✅ Meal completion: {meal.is_completed}")
                    print(f"✅ Meal completion percentage: {meal.completion_percentage}%")
                    
                    # Get meal details
                    meal_details = client_service.get_meal_details(meal)
                    print(f"✅ Meal details: {len(meal_details['components'])} components")
                
                # Test daily progress
                progress = client_service.get_daily_progress()
                print(f"✅ Daily progress after completion: {progress['completion_percentage']}%")
            
            # Cleanup
            diet_plan.delete()
    
    except Exception as e:
        print(f"❌ Meal completion error: {str(e)}")
    
    # Cleanup users
    client.delete()
    trainer.delete()

def test_daily_progress():
    """Test daily progress tracking."""
    print("\n📈 Testing Daily Progress Tracking...")
    
    # Create test data
    trainer, created = CustomUser.objects.get_or_create(
        username='test_progress_trainer',
        defaults={
            'email': 'progress_trainer@test.com',
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
        trainer.set_password('testpass123')
        trainer.save()
    
    client, created = CustomUser.objects.get_or_create(
        username='test_progress_client',
        defaults={
            'email': 'progress_client@test.com',
            'user_type': 'client',
            'first_name': 'Test',
            'last_name': 'Client',
            'phone_number': '0987654321',
            'height': 165,
            'weight': 60,
            'age': 25,
            'gender': 'Female',
            'activity_level': 'Light',
            'assigned_trainer': trainer
        }
    )
    if created:
        client.set_password('testpass123')
        client.save()
    
    try:
        # Create a diet plan
        trainer_service = TrainerDietPlanService(trainer)
        template = DietPlanTemplate.objects.first()
        
        if template:
            diet_plan = trainer_service.create_diet_plan(
                client=client,
                template=template,
                start_date=date.today(),
                duration_weeks=1,
                goal='Maintain',
                daily_calories=1800
            )
            
            # Add multiple meals
            foods = FoodItem.objects.all()[:3]
            meal_types = ['Breakfast', 'Lunch', 'Dinner']
            
            for i, meal_type in enumerate(meal_types):
                if i < len(foods):
                    food_items = [{'food_id': foods[i].id, 'quantity': 100}]
                    
                    meal = trainer_service.add_meal_to_plan(
                        diet_plan=diet_plan,
                        meal_type=meal_type,
                        target_date=date.today(),
                        food_items=food_items,
                        scheduled_time=time(8 + i * 4, 0),  # 8am, 12pm, 4pm
                        description=f'{meal_type} meal'
                    )
            
            # Test daily progress creation
            daily_progress, created = DailyProgress.objects.get_or_create(
                user=client,
                diet_plan=diet_plan,
                date=date.today(),
                defaults={
                    'target_calories': diet_plan.daily_calories,
                    'target_protein': (diet_plan.daily_calories * 0.30) / 4,
                    'target_carbs': (diet_plan.daily_calories * 0.50) / 4,
                    'target_fat': (diet_plan.daily_calories * 0.20) / 9
                }
            )
            print(f"✅ Daily progress created: {created}")
            
            # Test progress update
            daily_progress.update_progress()
            print(f"✅ Progress updated: {daily_progress.completion_percentage}%")
            print(f"✅ Calories consumed: {daily_progress.calories_consumed}")
            print(f"✅ Calories percentage: {daily_progress.calories_percentage}%")
            
            # Complete all meals
            client_service = ClientProgressService(client)
            meals = diet_plan.meals.filter(date=date.today())
            
            for meal in meals:
                for component in meal.components.all():
                    client_service.complete_meal_component(component)
            
            # Update progress again
            daily_progress.update_progress()
            print(f"✅ Progress after completion: {daily_progress.completion_percentage}%")
            print(f"✅ Day completed: {daily_progress.is_day_completed}")
            
            # Cleanup
            diet_plan.delete()
    
    except Exception as e:
        print(f"❌ Daily progress error: {str(e)}")
    
    # Cleanup users
    client.delete()
    trainer.delete()

def main():
    """Run all tests."""
    print("🚀 Starting Simple Diet System Tests")
    print("=" * 50)
    
    test_models()
    test_trainer_services()
    test_client_services()
    test_meal_completion()
    test_daily_progress()
    
    print("\n" + "=" * 50)
    print("✅ All tests completed!")

if __name__ == "__main__":
    main()
