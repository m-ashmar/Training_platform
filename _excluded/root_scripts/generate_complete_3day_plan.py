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
print(f"COMPLETE 3-DAY DIET PLAN WITH ALL INFORMATION")
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

# Calculate macro targets
if "lose" in goal.lower():
    protein_pct, carb_pct, fat_pct = 0.35, 0.40, 0.25
elif "gain" in goal.lower():
    protein_pct, carb_pct, fat_pct = 0.25, 0.55, 0.20
else:
    protein_pct, carb_pct, fat_pct = 0.30, 0.50, 0.20

daily_protein_target = daily_kcal * protein_pct / 4.0
daily_carb_target = daily_kcal * carb_pct / 4.0
daily_fat_target = daily_kcal * fat_pct / 9.0

print(f"{'='*80}")
print(f"DAILY MACRO TARGETS")
print(f"{'='*80}")
print(f"Protein: {daily_protein_target:.1f}g ({protein_pct*100:.0f}%)")
print(f"Carbs: {daily_carb_target:.1f}g ({carb_pct*100:.0f}%)")
print(f"Fat: {daily_fat_target:.1f}g ({fat_pct*100:.0f}%)")
print(f"{'='*80}\n")

# Print plan by day
daily_totals = []
for day_num in sorted(meals_by_day.keys()):
    print(f"{'='*80}")
    print(f"DAY {day_num}")
    print(f"{'='*80}\n")
    
    day_meals = meals_by_day[day_num]
    day_total_kcal = 0.0
    day_total_protein = 0.0
    day_total_carbs = 0.0
    day_total_fat = 0.0
    
    for meal in day_meals:
        meal_name = meal.meal_name
        
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
            print(f"  • {item['name']:45s} | {item['quantity']:8s} | {item['kcal']:7.1f} kcal")
        
        day_total_kcal += meal_kcal
        day_total_protein += meal_protein
        day_total_carbs += meal_carbs
        day_total_fat += meal_fat
        
        # Calculate meal percentage of daily target
        meal_pct = (meal_kcal / daily_kcal * 100) if daily_kcal > 0 else 0
        
        # Print meal summary
        print(f"\n{meal_name} Summary:")
        print(f"  Total Calories: {meal_kcal:.1f} kcal ({meal_pct:.1f}% of daily)")
        print(f"  Protein: {meal_protein:.1f}g")
        print(f"  Carbs: {meal_carbs:.1f}g")
        print(f"  Fat: {meal_fat:.1f}g")
        print(f"{'='*80}\n")
    
    daily_totals.append({
        'kcal': day_total_kcal,
        'protein': day_total_protein,
        'carbs': day_total_carbs,
        'fat': day_total_fat
    })
    
    # Day summary
    day_pct = (day_total_kcal / daily_kcal * 100) if daily_kcal > 0 else 0
    print(f"Day {day_num} Total:")
    print(f"  Calories: {day_total_kcal:.1f} kcal / {daily_kcal:.1f} kcal ({day_pct:.1f}%)")
    print(f"  Protein: {day_total_protein:.1f}g / {daily_protein_target:.1f}g ({day_total_protein/daily_protein_target*100:.1f}%)")
    print(f"  Carbs: {day_total_carbs:.1f}g / {daily_carb_target:.1f}g ({day_total_carbs/daily_carb_target*100:.1f}%)")
    print(f"  Fat: {day_total_fat:.1f}g / {daily_fat_target:.1f}g ({day_total_fat/daily_fat_target*100:.1f}%)")
    
    # Macro breakdown
    if day_total_kcal > 0:
        protein_kcal = day_total_protein * 4
        carb_kcal = day_total_carbs * 4
        fat_kcal = day_total_fat * 9
        print(f"\n  Macro Percentage Breakdown:")
        print(f"    Protein: {protein_kcal:.1f} kcal ({protein_kcal/day_total_kcal*100:.1f}%)")
        print(f"    Carbs: {carb_kcal:.1f} kcal ({carb_kcal/day_total_kcal*100:.1f}%)")
        print(f"    Fat: {fat_kcal:.1f} kcal ({fat_kcal/day_total_kcal*100:.1f}%)")
    
    print(f"\n{'='*80}\n")

# Overall summary
print(f"{'='*80}")
print(f"OVERALL SUMMARY")
print(f"{'='*80}")
print(f"Total Meals Generated: {len([m for m in plan_output.plan if m.meal_name != 'Snack'])}")

if daily_totals:
    avg_kcal = sum(d['kcal'] for d in daily_totals) / len(daily_totals)
    avg_protein = sum(d['protein'] for d in daily_totals) / len(daily_totals)
    avg_carbs = sum(d['carbs'] for d in daily_totals) / len(daily_totals)
    avg_fat = sum(d['fat'] for d in daily_totals) / len(daily_totals)
    
    print(f"\nAverage Daily Values:")
    print(f"  Calories: {avg_kcal:.1f} kcal")
    print(f"  Protein: {avg_protein:.1f}g")
    print(f"  Carbs: {avg_carbs:.1f}g")
    print(f"  Fat: {avg_fat:.1f}g")
    
    print(f"\nDaily Breakdown:")
    for i, day_total in enumerate(daily_totals, 1):
        print(f"  Day {i}: {day_total['kcal']:.1f} kcal | P: {day_total['protein']:.1f}g | C: {day_total['carbs']:.1f}g | F: {day_total['fat']:.1f}g")

print(f"{'='*80}\n")


