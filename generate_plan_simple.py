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
print(f"GENERATING DIET PLAN FOR USER: {user.email}")
print(f"{'='*80}")
print(f"Name: {getattr(user, 'full_name', None) or getattr(user, 'username', 'N/A')}")

# Print user preferences
print("\n" + "="*80)
print("USER FOOD PREFERENCES")
print("="*80)
preferences = UserFoodCategoryPreference.objects.filter(user=user).select_related('food')
if not preferences.exists():
    print("No food preferences found for this user.")
else:
    preferences_by_meal = {}
    for pref in preferences:
        meal = pref.meal
        macro = pref.macro
        if meal not in preferences_by_meal:
            preferences_by_meal[meal] = {}
        if macro not in preferences_by_meal[meal]:
            preferences_by_meal[meal][macro] = []
        preferences_by_meal[meal][macro].append(pref.food.name)
    
    for meal in ['Breakfast', 'Lunch', 'Dinner', 'Snack']:
        if meal in preferences_by_meal:
            print(f"\n{meal}:")
            for macro in ['protein', 'carb', 'fat', 'vegetable', 'fruit']:
                if macro in preferences_by_meal[meal]:
                    foods = preferences_by_meal[meal][macro]
                    print(f"  {macro.capitalize()}: {', '.join(foods)}")

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

plan_output = planner.generate(
    daily_kcal=daily_kcal,
    meal_count=3,
    snack_count=1,
    duration_days=1,
    no_repeat_days=3,
)

# Calculate nutrition helper
def calc_nutrition(meal):
    total_kcal = 0.0
    total_protein = 0.0
    total_carbs = 0.0
    total_fat = 0.0
    for ingredient in meal.ingredients:
        try:
            grams = float(ingredient.quantity.replace('g', '').strip())
        except:
            grams = 0.0
        try:
            food = FoodItem.objects.get(name=ingredient.name)
            p_pg, c_pg, f_pg, kcal_pg = get_macro_densities_for_food(food)
            total_kcal += grams * kcal_pg
            total_protein += grams * p_pg
            total_carbs += grams * c_pg
            total_fat += grams * f_pg
        except:
            pass
    return {'calories': total_kcal, 'protein': total_protein, 'carbs': total_carbs, 'fat': total_fat}

# Print plan details
print("\n" + "="*80)
print("DIET PLAN DETAILS")
print("="*80)

if "lose" in goal.lower():
    protein_pct, carb_pct, fat_pct = 0.35, 0.40, 0.25
elif "gain" in goal.lower():
    protein_pct, carb_pct, fat_pct = 0.25, 0.55, 0.20
else:
    protein_pct, carb_pct, fat_pct = 0.30, 0.50, 0.20

daily_protein_target = daily_kcal * protein_pct / 4.0
daily_carb_target = daily_kcal * carb_pct / 4.0
daily_fat_target = daily_kcal * fat_pct / 9.0

print(f"\nDaily Macro Targets:")
print(f"  Protein: {daily_protein_target:.1f}g ({protein_pct*100:.0f}%)")
print(f"  Carbs: {daily_carb_target:.1f}g ({carb_pct*100:.0f}%)")
print(f"  Fat: {daily_fat_target:.1f}g ({fat_pct*100:.0f}%)")

snack_kcal = 200.0
meal_kcal_budget = daily_kcal - snack_kcal
if goal.lower() == "gain":
    meal_dist = {'Breakfast': 0.40, 'Lunch': 0.40, 'Dinner': 0.20}
elif goal.lower() == "lose":
    meal_dist = {'Breakfast': 0.30, 'Lunch': 0.40, 'Dinner': 0.30}
else:
    meal_dist = {'Breakfast': 0.35, 'Lunch': 0.35, 'Dinner': 0.30}

meal_targets = {}
for meal_name in ['Breakfast', 'Lunch', 'Dinner']:
    meal_targets[meal_name] = {
        'kcal': meal_kcal_budget * meal_dist[meal_name],
        'protein': daily_protein_target * meal_dist[meal_name],
        'carbs': daily_carb_target * meal_dist[meal_name],
        'fat': daily_fat_target * meal_dist[meal_name],
    }
meal_targets['Snack'] = {'kcal': snack_kcal, 'protein': 0.0, 'carbs': 0.0, 'fat': 0.0}

print("\n" + "-"*80)
print("MEAL BREAKDOWN")
print("-"*80)

total_daily = {'kcal': 0.0, 'protein': 0.0, 'carbs': 0.0, 'fat': 0.0}

for meal in plan_output.plan:
    meal_name = meal.meal_name
    nutrition = calc_nutrition(meal)
    target = meal_targets.get(meal_name, {'kcal': 0.0, 'protein': 0.0, 'carbs': 0.0, 'fat': 0.0})
    
    total_daily['kcal'] += nutrition['calories']
    total_daily['protein'] += nutrition['protein']
    total_daily['carbs'] += nutrition['carbs']
    total_daily['fat'] += nutrition['fat']
    
    print(f"\n{meal_name}:")
    print(f"  Target Calories: {target['kcal']:.1f} kcal")
    print(f"  Actual Calories: {nutrition['calories']:.1f} kcal")
    diff = nutrition['calories'] - target['kcal']
    pct_diff = (diff / target['kcal'] * 100) if target['kcal'] > 0 else 0
    print(f"  Difference: {diff:+.1f} kcal ({pct_diff:+.1f}%)")
    
    print(f"\n  Ingredients:")
    for ing in meal.ingredients:
        print(f"    - {ing.name}: {ing.quantity}")
    
    print(f"\n  Nutrition:")
    print(f"    Protein: {nutrition['protein']:.1f}g (target: {target['protein']:.1f}g)")
    print(f"    Carbs: {nutrition['carbs']:.1f}g (target: {target['carbs']:.1f}g)")
    print(f"    Fat: {nutrition['fat']:.1f}g (target: {target['fat']:.1f}g)")
    
    if daily_kcal > 0:
        meal_pct = (nutrition['calories'] / daily_kcal) * 100
        print(f"    Percentage of Daily Calories: {meal_pct:.1f}%")

print("\n" + "-"*80)
print("DAILY TOTALS")
print("-"*80)
print(f"Total Calories: {total_daily['kcal']:.1f} / {daily_kcal:.1f} kcal ({total_daily['kcal']/daily_kcal*100:.1f}%)")
print(f"Total Protein: {total_daily['protein']:.1f}g / {daily_protein_target:.1f}g ({total_daily['protein']/daily_protein_target*100:.1f}%)")
print(f"Total Carbs: {total_daily['carbs']:.1f}g / {daily_carb_target:.1f}g ({total_daily['carbs']/daily_carb_target*100:.1f}%)")
print(f"Total Fat: {total_daily['fat']:.1f}g / {daily_fat_target:.1f}g ({total_daily['fat']/daily_fat_target*100:.1f}%)")

if total_daily['kcal'] > 0:
    protein_kcal = total_daily['protein'] * 4
    carb_kcal = total_daily['carbs'] * 4
    fat_kcal = total_daily['fat'] * 9
    print(f"\nMacro Percentage Breakdown:")
    print(f"  Protein: {protein_kcal:.1f} kcal ({protein_kcal/total_daily['kcal']*100:.1f}%)")
    print(f"  Carbs: {carb_kcal:.1f} kcal ({carb_kcal/total_daily['kcal']*100:.1f}%)")
    print(f"  Fat: {fat_kcal:.1f} kcal ({fat_kcal/total_daily['kcal']*100:.1f}%)")

print(f"\n{'='*80}\n")

