import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from diet.models import UserFoodCategoryPreference, FoodItem
from diet.services.rule_based_planner import RuleBasedPlanner
from diet.utils.nutrition import get_macro_densities_for_food

email = "oo@gmail.com"
user = CustomUser.objects.get(email=email)
print(f"\n{'='*80}")
print(f"3-DAY DIET PLAN - CALORIES & QUANTITIES BY MEAL")
print(f"{'='*80}")
print(f"User: {user.email}")
print(f"Name: {getattr(user, 'full_name', None) or getattr(user, 'username', 'N/A')}")

# Calculate daily calories
try:
    daily_kcal = float(user.calculate_daily_calories() or 2000.0)
except:
    daily_kcal = 2000.0

# Generate plan
planner = RuleBasedPlanner(user)
goal = planner._resolve_goal()
print(f"Goal: {goal}")
print(f"Daily Calorie Target: {daily_kcal:.1f} kcal\n")

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
    
    if meal_name == "Breakfast":
        meal_count += 1
        current_day = meal_count
        meals_by_day[current_day] = []
    
    if current_day:
        meals_by_day[current_day].append(meal)

# Print plan by day
for day_num in sorted(meals_by_day.keys()):
    print(f"{'='*80}")
    print(f"DAY {day_num}")
    print(f"{'='*80}\n")
    
    day_meals = meals_by_day[day_num]
    day_total = 0.0
    
    for meal in day_meals:
        meal_name = meal.meal_name
        
        # Skip snacks, focus on Breakfast, Lunch, Dinner
        if meal_name == "Snack":
            continue
        
        # Calculate nutrition
        meal_kcal = 0.0
        meal_protein = 0.0
        meal_carbs = 0.0
        meal_fat = 0.0
        
        print(f"{meal_name.upper()}")
        print(f"{'-'*80}")
        print(f"Food Items & Quantities:")
        print(f"{'-'*80}")
        
        items_detail = []
        for ingredient in meal.ingredients:
            try:
                grams = float(ingredient.quantity.replace('g', '').strip())
            except:
                grams = 0.0
            
            try:
                food = FoodItem.objects.filter(name=ingredient.name).first()
                if food:
                    p_pg, c_pg, f_pg, kcal_pg = get_macro_densities_for_food(food)
                    
                    item_kcal = grams * kcal_pg
                    item_protein = grams * p_pg
                    item_carbs = grams * c_pg
                    item_fat = grams * f_pg
                    
                    meal_kcal += item_kcal
                    meal_protein += item_protein
                    meal_carbs += item_carbs
                    meal_fat += item_fat
                    
                    items_detail.append({
                        'name': ingredient.name,
                        'quantity': ingredient.quantity,
                        'grams': grams,
                        'kcal': item_kcal,
                        'protein': item_protein,
                        'carbs': item_carbs,
                        'fat': item_fat
                    })
                else:
                    items_detail.append({
                        'name': ingredient.name,
                        'quantity': ingredient.quantity,
                        'grams': grams,
                        'kcal': 0.0,
                        'protein': 0.0,
                        'carbs': 0.0,
                        'fat': 0.0
                    })
            except Exception as e:
                items_detail.append({
                    'name': ingredient.name,
                    'quantity': ingredient.quantity,
                    'grams': grams,
                    'kcal': 0.0,
                    'protein': 0.0,
                    'carbs': 0.0,
                    'fat': 0.0
                })
        
        # Print items with calories
        for item in items_detail:
            print(f"  • {item['name']:40s} | Quantity: {item['quantity']:8s} | Calories: {item['kcal']:7.1f} kcal")
        
        day_total += meal_kcal
        
        # Print meal summary
        print(f"\n{'='*80}")
        print(f"{meal_name} Total: {meal_kcal:.1f} kcal")
        print(f"  Protein: {meal_protein:.1f}g | Carbs: {meal_carbs:.1f}g | Fat: {meal_fat:.1f}g")
        print(f"{'='*80}\n")
    
    print(f"Day {day_num} Total (Breakfast + Lunch + Dinner): {day_total:.1f} kcal\n")

print(f"{'='*80}")
print(f"SUMMARY")
print(f"{'='*80}")
print(f"Total Meals Generated: {len([m for m in plan_output.plan if m.meal_name != 'Snack'])}")
print(f"{'='*80}\n")


