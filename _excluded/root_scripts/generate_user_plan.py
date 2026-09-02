#!/usr/bin/env python
"""
Script to generate a diet plan for a user and print detailed information.
Usage: python manage.py shell < generate_user_plan.py
Or run interactively in Django shell.
"""

import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from diet.models import UserFoodCategoryPreference, FoodItem
from diet.services.rule_based_planner import RuleBasedPlanner
from diet.utils.nutrition import get_macro_densities_for_food

def print_user_preferences(user):
    """Print user's food preferences grouped by meal and macro."""
    print("\n" + "="*80)
    print("USER FOOD PREFERENCES")
    print("="*80)
    
    preferences = UserFoodCategoryPreference.objects.filter(user=user).select_related('food')
    
    if not preferences.exists():
        print("No food preferences found for this user.")
        return
    
    # Group by meal and macro
    preferences_by_meal = {}
    for pref in preferences:
        meal = pref.meal
        macro = pref.macro
        if meal not in preferences_by_meal:
            preferences_by_meal[meal] = {}
        if macro not in preferences_by_meal[meal]:
            preferences_by_meal[meal][macro] = []
        preferences_by_meal[meal][macro].append(pref.food.name)
    
    # Print organized by meal
    for meal in ['Breakfast', 'Lunch', 'Dinner', 'Snack']:
        if meal in preferences_by_meal:
            print(f"\n{meal}:")
            print("-" * 40)
            for macro in ['protein', 'carb', 'fat', 'vegetable', 'fruit']:
                if macro in preferences_by_meal[meal]:
                    foods = preferences_by_meal[meal][macro]
                    print(f"  {macro.capitalize()}: {', '.join(foods)}")

def calculate_meal_nutrition(meal):
    """Calculate total nutrition for a meal from its ingredients."""
    total_kcal = 0.0
    total_protein = 0.0
    total_carbs = 0.0
    total_fat = 0.0
    
    for ingredient in meal.ingredients:
        # Extract quantity (e.g., "100g" -> 100.0)
        quantity_str = ingredient.quantity
        try:
            grams = float(quantity_str.replace('g', '').strip())
        except:
            grams = 0.0
        
        # Find food item
        try:
            food = FoodItem.objects.get(name=ingredient.name)
            p_pg, c_pg, f_pg, kcal_pg = get_macro_densities_for_food(food)
            
            total_kcal += grams * kcal_pg
            total_protein += grams * p_pg
            total_carbs += grams * c_pg
            total_fat += grams * f_pg
        except FoodItem.DoesNotExist:
            pass
    
    return {
        'calories': total_kcal,
        'protein': total_protein,
        'carbs': total_carbs,
        'fat': total_fat
    }

def print_diet_plan(plan_output, daily_kcal, goal):
    """Print detailed diet plan information."""
    print("\n" + "="*80)
    print("DIET PLAN DETAILS")
    print("="*80)
    
    print(f"\nUser Goal: {goal}")
    print(f"Daily Calorie Target: {daily_kcal:.1f} kcal")
    print(f"Total Meals: {len(plan_output.plan)}")
    
    # Calculate macro ratios based on goal
    if "lose" in goal.lower():
        protein_pct = 0.35
        carb_pct = 0.40
        fat_pct = 0.25
    elif "gain" in goal.lower():
        protein_pct = 0.25
        carb_pct = 0.55
        fat_pct = 0.20
    else:  # maintain
        protein_pct = 0.30
        carb_pct = 0.50
        fat_pct = 0.20
    
    daily_protein_target = daily_kcal * protein_pct / 4.0
    daily_carb_target = daily_kcal * carb_pct / 4.0
    daily_fat_target = daily_kcal * fat_pct / 9.0
    
    print(f"\nDaily Macro Targets:")
    print(f"  Protein: {daily_protein_target:.1f}g ({protein_pct*100:.0f}%)")
    print(f"  Carbs: {daily_carb_target:.1f}g ({carb_pct*100:.0f}%)")
    print(f"  Fat: {daily_fat_target:.1f}g ({fat_pct*100:.0f}%)")
    
    # Process each meal
    total_daily_kcal = 0.0
    total_daily_protein = 0.0
    total_daily_carbs = 0.0
    total_daily_fat = 0.0
    
    snack_kcal = 200.0  # Default snack allocation
    meal_kcal_budget = daily_kcal - snack_kcal
    
    # Estimate meal targets (will be recalculated if we have access to day_ctx)
    meal_targets = {}
    if goal.lower() == "gain":
        meal_distribution = {'Breakfast': 0.40, 'Lunch': 0.40, 'Dinner': 0.20}
    elif goal.lower() == "lose":
        meal_distribution = {'Breakfast': 0.30, 'Lunch': 0.40, 'Dinner': 0.30}
    else:
        meal_distribution = {'Breakfast': 0.35, 'Lunch': 0.35, 'Dinner': 0.30}
    
    for meal_name in ['Breakfast', 'Lunch', 'Dinner']:
        if meal_name in meal_distribution:
            meal_targets[meal_name] = {
                'kcal': meal_kcal_budget * meal_distribution[meal_name],
                'protein': daily_protein_target * meal_distribution[meal_name],
                'carbs': daily_carb_target * meal_distribution[meal_name],
                'fat': daily_fat_target * meal_distribution[meal_name],
            }
    
    meal_targets['Snack'] = {
        'kcal': snack_kcal,
        'protein': 0.0,
        'carbs': 0.0,
        'fat': 0.0,
    }
    
    print("\n" + "-"*80)
    print("MEAL BREAKDOWN")
    print("-"*80)
    
    for meal in plan_output.plan:
        meal_name = meal.meal_name
        nutrition = calculate_meal_nutrition(meal)
        
        total_daily_kcal += nutrition['calories']
        total_daily_protein += nutrition['protein']
        total_daily_carbs += nutrition['carbs']
        total_daily_fat += nutrition['fat']
        
        target = meal_targets.get(meal_name, {
            'kcal': 0.0,
            'protein': 0.0,
            'carbs': 0.0,
            'fat': 0.0,
        })
        
        print(f"\n{meal_name}:")
        print(f"  Target Calories: {target['kcal']:.1f} kcal")
        print(f"  Actual Calories: {nutrition['calories']:.1f} kcal")
        diff = nutrition['calories'] - target['kcal']
        pct_diff = (diff / target['kcal'] * 100) if target['kcal'] > 0 else 0
        print(f"  Difference: {diff:+.1f} kcal ({pct_diff:+.1f}%)")
        
        print(f"\n  Ingredients:")
        for ing in meal.ingredients:
            print(f"    - {ing.name}: {ing.quantity}")
        
        print(f"\n  Nutrition Breakdown:")
        print(f"    Protein: {nutrition['protein']:.1f}g (target: {target['protein']:.1f}g)")
        print(f"    Carbs: {nutrition['carbs']:.1f}g (target: {target['carbs']:.1f}g)")
        print(f"    Fat: {nutrition['fat']:.1f}g (target: {target['fat']:.1f}g)")
        
        # Calculate percentage of daily calories
        if daily_kcal > 0:
            meal_pct = (nutrition['calories'] / daily_kcal) * 100
            print(f"    Percentage of Daily Calories: {meal_pct:.1f}%")
    
    print("\n" + "-"*80)
    print("DAILY TOTALS")
    print("-"*80)
    print(f"Total Calories: {total_daily_kcal:.1f} / {daily_kcal:.1f} kcal ({total_daily_kcal/daily_kcal*100:.1f}%)")
    print(f"Total Protein: {total_daily_protein:.1f}g / {daily_protein_target:.1f}g ({total_daily_protein/daily_protein_target*100:.1f}%)")
    print(f"Total Carbs: {total_daily_carbs:.1f}g / {daily_carb_target:.1f}g ({total_daily_carbs/daily_carb_target*100:.1f}%)")
    print(f"Total Fat: {total_daily_fat:.1f}g / {daily_fat_target:.1f}g ({total_daily_fat/daily_fat_target*100:.1f}%)")
    
    # Macro percentage breakdown
    if total_daily_kcal > 0:
        protein_kcal = total_daily_protein * 4
        carb_kcal = total_daily_carbs * 4
        fat_kcal = total_daily_fat * 9
        
        print(f"\nMacro Percentage Breakdown:")
        print(f"  Protein: {protein_kcal:.1f} kcal ({protein_kcal/total_daily_kcal*100:.1f}%)")
        print(f"  Carbs: {carb_kcal:.1f} kcal ({carb_kcal/total_daily_kcal*100:.1f}%)")
        print(f"  Fat: {fat_kcal:.1f} kcal ({fat_kcal/total_daily_kcal*100:.1f}%)")

def main():
    email = "oo@gmail.com"
    
    try:
        user = CustomUser.objects.get(email=email)
        print(f"\n{'='*80}")
        print(f"GENERATING DIET PLAN FOR USER: {user.email}")
        print(f"{'='*80}")
        print(f"Name: {user.get_full_name() or 'N/A'}")
        print(f"User ID: {user.id}")
        
        # Print user preferences
        print_user_preferences(user)
        
        # Calculate daily calories
        try:
            daily_kcal = float(user.calculate_daily_calories() or 2000.0)
        except:
            daily_kcal = 2000.0
            print(f"\nWarning: Could not calculate daily calories, using default: {daily_kcal} kcal")
        
        # Determine goal
        planner = RuleBasedPlanner(user)
        goal = planner._resolve_goal()
        
        # Generate diet plan
        print(f"\n{'='*80}")
        print("GENERATING PLAN...")
        print(f"{'='*80}")
        
        plan_output = planner.generate(
            daily_kcal=daily_kcal,
            meal_count=3,
            snack_count=1,
            duration_days=1,
            no_repeat_days=3,
        )
        
        # Print plan details
        print_diet_plan(plan_output, daily_kcal, goal)
        
        print(f"\n{'='*80}")
        print("PLAN GENERATION COMPLETE")
        print(f"{'='*80}\n")
        
    except CustomUser.DoesNotExist:
        print(f"ERROR: User with email '{email}' not found!")
    except Exception as e:
        print(f"ERROR: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()


