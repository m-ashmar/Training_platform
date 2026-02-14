
import os
import sys
import django
from datetime import timedelta

# Setup Django environment
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from diet.models import DietPlan
from diet.utils.nutrition import get_macro_ratios

def generate_report():
    # Fetch the Diet Plan (ID 390)
    try:
        plan = DietPlan.objects.get(id=393)
    except DietPlan.DoesNotExist:
        print(f"Plan 390 not found.")
        return

    user = plan.user
    print(f"# Diet Plan Report for {user.email}")
    print(f"**Plan ID:** {plan.id}")
    print(f"**Goal:** {plan.goal}")
    print(f"**Dates:** {plan.start_date} to {plan.end_date}")
    print(f"**Daily Calorie Target:** {plan.daily_calories:.0f} kcal")
    
    # Calculate macro targets
    ratios = get_macro_ratios(plan.goal)
    target_protein = (plan.daily_calories * ratios['protein']) / 4.0
    target_carb = (plan.daily_calories * ratios['carb']) / 4.0
    target_fat = (plan.daily_calories * ratios['fat']) / 9.0
    
    print(f"**Macro Targets:** P: {target_protein:.1f}g | C: {target_carb:.1f}g | F: {target_fat:.1f}g")
    print("\n---")

    current_date = plan.start_date
    while current_date <= plan.end_date:
        print(f"\n## Date: {current_date.strftime('%A, %Y-%m-%d')}")
        
        day_meals = plan.meals.filter(date=current_date).order_by('id') # id usually implies order if created sequentially
        # Better to sort by meal type order if possible, but they have 'meal_type' char field.
        # Let's verify order.
        
        # Sort manually
        meal_order = {'Breakfast': 1, 'Lunch': 2, 'Snack': 3, 'Dinner': 4}
        day_meals = sorted(day_meals, key=lambda m: meal_order.get(m.meal_type, 99))

        day_stats = {'calories': 0, 'protein': 0, 'carbs': 0, 'fat': 0}

        if not day_meals:
            print("_No meals generated for this day._")
            current_date += timedelta(days=1)
            continue

        for meal in day_meals:
            print(f"\n### {meal.meal_type}")
            m_stats = meal.calculate_nutrition()
            
            # Accumulate day stats
            day_stats['calories'] += m_stats['calories']
            day_stats['protein'] += m_stats['protein']
            day_stats['carbs'] += m_stats['carbs']
            day_stats['fat'] += m_stats['fat']

            if not meal.components.exists():
                print("  _Empty meal_")
                continue

            for comp in meal.components.all():
                food = comp.food
                print(f"- **{food.name}**: {comp.quantity:.1f}g "
                      f"({(food.calories * comp.quantity/100):.0f} kcal, "
                      f"P:{(food.protein * comp.quantity/100):.1f}g, "
                      f"C:{(food.carbs * comp.quantity/100):.1f}g, "
                      f"F:{(food.fat * comp.quantity/100):.1f}g)")
            
            print(f"> **Meal Totals:** {m_stats['calories']:.0f} kcal | "
                  f"P: {m_stats['protein']:.1f}g | C: {m_stats['carbs']:.1f}g | F: {m_stats['fat']:.1f}g")

        # Day Summary
        print(f"\n### Day Summary vs Targets")
        print(f"| Metric | Actual | Target | % Achieved |")
        print(f"| :--- | :--- | :--- | :--- |")
        
        def safe_pct(val, target):
            return (val / target * 100) if target > 0 else 0

        print(f"| Calories | {day_stats['calories']:.0f} | {plan.daily_calories:.0f} | {safe_pct(day_stats['calories'], plan.daily_calories):.1f}% |")
        print(f"| Protein | {day_stats['protein']:.1f}g | {target_protein:.1f}g | {safe_pct(day_stats['protein'], target_protein):.1f}% |")
        print(f"| Carbs | {day_stats['carbs']:.1f}g | {target_carb:.1f}g | {safe_pct(day_stats['carbs'], target_carb):.1f}% |")
        print(f"| Fat | {day_stats['fat']:.1f}g | {target_fat:.1f}g | {safe_pct(day_stats['fat'], target_fat):.1f}% |")
        
        current_date += timedelta(days=1)

if __name__ == "__main__":
    generate_report()
