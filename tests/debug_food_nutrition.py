#!/usr/bin/env python3
"""
Debug script to check food item nutritional values and understand why calories are showing as 0.
"""

import os
import sys
import django
from datetime import date

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from diet.models import FoodItem, Meal, MealComponent, DietPlan
from users.models import CustomUser

def debug_food_nutrition():
    """Debug food item nutritional values"""
    print("=" * 60)
    print("DEBUGGING FOOD NUTRITION VALUES")
    print("=" * 60)
    
    # Get all food items
    food_items = FoodItem.objects.all()
    print(f"Total food items in database: {food_items.count()}")
    
    # Check first 10 food items
    print("\nFirst 10 food items:")
    print("-" * 80)
    for i, food in enumerate(food_items[:10]):
        print(f"{i+1}. {food.name}")
        print(f"   Calories: {food.calories} per {food.serving_size}")
        print(f"   Protein: {food.protein}g")
        print(f"   Carbs: {food.carbs}g")
        print(f"   Fat: {food.fat}g")
        print(f"   Serving size: {food.serving_size}")
        print()
    
    # Check if there are any food items with zero calories
    zero_calorie_foods = food_items.filter(calories=0)
    print(f"Food items with zero calories: {zero_calorie_foods.count()}")
    
    # Check recent diet plans and meals
    print("\nRecent diet plans:")
    print("-" * 40)
    recent_plans = DietPlan.objects.order_by('-created_at')[:5]
    for plan in recent_plans:
        print(f"Plan ID: {plan.id}, User: {plan.user.username}, Calories: {plan.daily_calories}")
        
        # Check meals in this plan
        meals = plan.meals.all()
        print(f"  Meals: {meals.count()}")
        
        for meal in meals:
            print(f"    Meal ID: {meal.id}, Type: {meal.meal_type}")
            
            # Check components
            components = meal.components.all()
            print(f"      Components: {components.count()}")
            
            for comp in components:
                print(f"        - {comp.food.name}: {comp.quantity}g")
                print(f"          Food calories: {comp.food.calories} per {comp.food.serving_size}")
                
                # Calculate nutrition for this component
                scale_factor = comp.quantity / comp.food.serving_size_grams if comp.food.serving_size_grams > 0 else 0
                comp_calories = comp.food.calories * scale_factor
                print(f"          Component calories: {comp_calories}")
            
            # Calculate meal nutrition
            meal_nutrition = meal.calculate_nutrition()
            print(f"      Meal total calories: {meal_nutrition['calories']}")
            print(f"      Meal is completed: {meal.is_completed}")
            print()

def debug_meal_completion():
    """Debug meal completion process"""
    print("=" * 60)
    print("DEBUGGING MEAL COMPLETION")
    print("=" * 60)
    
    # Get the most recent diet plan
    try:
        latest_plan = DietPlan.objects.latest('created_at')
        print(f"Latest diet plan: ID {latest_plan.id}, User: {latest_plan.user.username}")
        
        # Get meals
        meals = latest_plan.meals.all()
        print(f"Total meals: {meals.count()}")
        
        for meal in meals:
            print(f"\nMeal ID: {meal.id}, Type: {meal.meal_type}")
            print(f"Description: {meal.description}")
            print(f"Is completed: {meal.is_completed}")
            print(f"Completion percentage: {meal.completion_percentage}")
            
            # Check components
            components = meal.components.all()
            print(f"Components: {components.count()}")
            
            total_calories = 0
            for comp in components:
                print(f"  - {comp.food.name}: {comp.quantity}g")
                print(f"    Food calories: {comp.food.calories} per {comp.food.serving_size}")
                print(f"    Is completed: {comp.is_completed}")
                
                # Calculate component nutrition
                scale_factor = comp.quantity / comp.food.serving_size_grams if comp.food.serving_size_grams > 0 else 0
                comp_calories = comp.food.calories * scale_factor
                total_calories += comp_calories
                print(f"    Component calories: {comp_calories}")
            
            print(f"  Total meal calories: {total_calories}")
            
            # Calculate meal nutrition using the model method
            meal_nutrition = meal.calculate_nutrition()
            print(f"  Model calculated calories: {meal_nutrition['calories']}")
            
    except DietPlan.DoesNotExist:
        print("No diet plans found")

if __name__ == "__main__":
    debug_food_nutrition()
    debug_meal_completion() 