import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from diet.models import UserFoodCategoryPreference, FoodItem
from diet.services.rule_based_planner import RuleBasedPlanner
from diet.utils.nutrition import get_macro_densities_for_food
from datetime import datetime

email = "oo@gmail.com"
user = CustomUser.objects.get(email=email)
print(f"\n{'='*80}")
print(f"GENERATING 3-DAY DIET PLAN FOR USER: {user.email}")
print(f"{'='*80}")
print(f"Name: {getattr(user, 'full_name', None) or getattr(user, 'username', 'N/A')}")

# Calculate daily calories
try:
    daily_kcal = float(user.calculate_daily_calories() or 2000.0)
except:
    daily_kcal = 2000.0

# Generate plan
planner = RuleBasedPlanner(user)
goal = planner._resolve_goal()
print(f"\nUser Goal: {goal}")
print(f"Daily Calorie Target: {daily_kcal:.1f} kcal")
print(f"Duration: 3 days\n")

plan_output = planner.generate(
    daily_kcal=daily_kcal,
    meal_count=3,
    snack_count=1,
    duration_days=3,
    no_repeat_days=3,
)

# Group meals by day
meals_by_day = {}
current_day = None
meal_count = 0

for meal in plan_output.plan:
    meal_name = meal.meal_name
    
    # Determine day by counting meals (Breakfast starts a new day)
    if meal_name == "Breakfast":
        meal_count += 1
        current_day = meal_count
        meals_by_day[current_day] = []
    
    if current_day:
        meals_by_day[current_day].append(meal)

# Print plan by day
print("="*80)
print("3-DAY DIET PLAN - FOOD ITEMS BY MEAL")
print("="*80)

for day_num in sorted(meals_by_day.keys()):
    print(f"\n{'='*80}")
    print(f"DAY {day_num}")
    print(f"{'='*80}")
    
    day_meals = meals_by_day[day_num]
    day_total_calories = 0.0
    
    for meal in day_meals:
        meal_name = meal.meal_name
        
        # Calculate nutrition
        meal_kcal = 0.0
        meal_protein = 0.0
        meal_carbs = 0.0
        meal_fat = 0.0
        
        food_items = []
        for ingredient in meal.ingredients:
            try:
                grams = float(ingredient.quantity.replace('g', '').strip())
            except:
                grams = 0.0
            
            try:
                food = FoodItem.objects.filter(name=ingredient.name).first()
                if food:
                    p_pg, c_pg, f_pg, kcal_pg = get_macro_densities_for_food(food)
                else:
                    p_pg, c_pg, f_pg, kcal_pg = 0.0, 0.0, 0.0, 0.0
                
                meal_kcal += grams * kcal_pg
                meal_protein += grams * p_pg
                meal_carbs += grams * c_pg
                meal_fat += grams * f_pg
                
                food_items.append(f"{ingredient.name} ({ingredient.quantity})")
            except FoodItem.DoesNotExist:
                food_items.append(f"{ingredient.name} ({ingredient.quantity})")
        
        day_total_calories += meal_kcal
        
        print(f"\n{meal_name}:")
        print(f"  Calories: {meal_kcal:.1f} kcal")
        print(f"  Protein: {meal_protein:.1f}g | Carbs: {meal_carbs:.1f}g | Fat: {meal_fat:.1f}g")
        print(f"  Food Items:")
        for item in food_items:
            print(f"    • {item}")
    
    print(f"\n  Day {day_num} Total Calories: {day_total_calories:.1f} kcal")

print(f"\n{'='*80}")
print(f"SUMMARY")
print(f"{'='*80}")
print(f"Total Meals Generated: {len(plan_output.plan)}")

# Calculate average daily calories
daily_totals = []
for day_num in sorted(meals_by_day.keys()):
    day_total = 0.0
    for meal in meals_by_day[day_num]:
        for ingredient in meal.ingredients:
            try:
                grams = float(ingredient.quantity.replace('g', '').strip())
            except:
                grams = 0.0
            try:
                food = FoodItem.objects.filter(name=ingredient.name).first()
                if food:
                    _, _, _, kcal_pg = get_macro_densities_for_food(food)
                    day_total += grams * kcal_pg
            except:
                pass
    daily_totals.append(day_total)

if daily_totals:
    avg_daily = sum(daily_totals) / len(daily_totals)
    print(f"Average Daily Calories: {avg_daily:.1f} kcal")
    print(f"Day 1: {daily_totals[0]:.1f} kcal")
    if len(daily_totals) > 1:
        print(f"Day 2: {daily_totals[1]:.1f} kcal")
    if len(daily_totals) > 2:
        print(f"Day 3: {daily_totals[2]:.1f} kcal")

print(f"{'='*80}\n")

