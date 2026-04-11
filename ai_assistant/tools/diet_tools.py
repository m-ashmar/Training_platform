"""
diet_tools.py — Diet plan, meal details, and adherence tools.

Queries DietPlan, Meal, MealComponent, DailyProgress to give the AI
visibility into the user's nutrition and adherence patterns.
"""

from datetime import timedelta
from django.utils import timezone
from django.utils.translation import gettext as _


def get_active_diet_plan(user, **kwargs):
    """Get the user's currently active diet plan summary."""
    from diet.models import DietPlan

    plan = DietPlan.objects.filter(
        user=user, is_active=True,
    ).select_related('template', 'created_by').first()

    if not plan:
        return {"active_plan": None, "message": str(_("No active diet plan found."))}
    
    print(f"[AI Tool] Found plan ID: {plan.id}, Goal: {plan.goal}, Calories: {plan.daily_calories}")

    return {
        "active_plan": {
            "id": plan.id,
            "goal": plan.goal,
            "daily_calories": plan.daily_calories,
            "start_date": plan.start_date.isoformat(),
            "end_date": plan.end_date.isoformat(),
            "duration_weeks": plan.duration_weeks,
            "strategy": plan.get_generation_strategy_display(),
            "created_by": (
                plan.created_by.full_name if plan.created_by else "AI Generated"
            ),
        },
    }


def get_meal_details(user, date_str=None, **kwargs):
    """
    Get detailed meal breakdown for a specific date.
    Returns food items, portions, and macros per meal.
    """
    from diet.models import DietPlan, Meal

    print(f"[AI Tool] get_meal_details called for {user.username}, date={date_str}")
    
    if date_str:
        from datetime import date as dt_date
        target_date = dt_date.fromisoformat(date_str)
    else:
        target_date = timezone.localdate()

    plan = DietPlan.objects.filter(user=user, is_active=True).first()
    if not plan:
        print("[AI Tool] No active plan.")
        return {"meals": [], "message": str(_("No active diet plan."))}

    meals = Meal.objects.filter(
        diet_plan=plan, date=target_date,
    ).prefetch_related('components__food').order_by('scheduled_time')
    
    print(f"[AI Tool] Found {meals.count()} meals for {target_date}")

    results = []
    for meal in meals:
        components = []
        for comp in meal.components.all():
            nutrition = comp.calculate_nutrition()
            components.append({
                "food": comp.food.name,
                "quantity_g": comp.quantity,
                "calories": nutrition["calories"],
                "protein": nutrition["protein"],
                "carbs": nutrition["carbs"],
                "fat": nutrition["fat"],
                "completed": comp.is_completed,
            })

        meal_nutrition = meal.calculate_nutrition()
        results.append({
            "meal_type": meal.meal_type,
            "time": meal.scheduled_time.isoformat() if meal.scheduled_time else None,
            "is_completed": meal.is_completed,
            "completion_pct": meal.completion_percentage,
            "components": components,
            "totals": meal_nutrition,
        })

    return {"date": target_date.isoformat(), "meals": results}


def get_diet_adherence(user, days=7, **kwargs):
    """
    Get diet adherence trends — calories consumed vs target,
    macro gaps, and best/worst days.
    """
    from diet.models import DailyProgress, DietPlan

    print(f"[AI Tool] get_diet_adherence called for {user.username}, days={days}")

    plan = DietPlan.objects.filter(user=user, is_active=True).first()
    if not plan:
        print("[AI Tool] No active plan.")
        return {"adherence": None, "message": str(_("No active diet plan."))}

    since = timezone.localdate() - timedelta(days=days)
    progress_qs = DailyProgress.objects.filter(
        user=user, diet_plan=plan, date__gte=since,
    ).order_by('date')
    
    print(f"[AI Tool] Found {progress_qs.count()} DailyProgress records since {since}")

    daily_data = []
    total_adherence = 0
    count = 0

    for dp in progress_qs:
        cal_pct = dp.calories_percentage
        daily_data.append({
            "date": dp.date.isoformat(),
            "calories_consumed": dp.calories_consumed,
            "calories_target": dp.target_calories,
            "calories_pct": cal_pct,
            "protein_pct": dp.protein_percentage,
            "carbs_pct": dp.carbs_percentage,
            "fat_pct": dp.fat_percentage,
            "meals_completed": dp.meals_completed,
            "total_meals": dp.total_meals,
        })
        if dp.target_calories > 0:
            # Adherence score: 100 when exactly on target
            adherence = max(0, 100 - abs(cal_pct - 100))
            total_adherence += adherence
            count += 1

    avg_adherence = round(total_adherence / count, 1) if count else 0

    # Find best and worst days
    best_day = max(daily_data, key=lambda d: d["calories_pct"]) if daily_data else None
    worst_day = min(daily_data, key=lambda d: d["calories_pct"]) if daily_data else None

    return {
        "average_adherence_pct": avg_adherence,
        "days_tracked": count,
        "daily_data": daily_data,
        "best_day": best_day,
        "worst_day": worst_day,
        "period_days": days,
    }


# --- OpenAI Function Schemas ---

DIET_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_active_diet_plan",
            "description": (
                "Get the user's currently active diet plan including goal, "
                "daily calorie target, duration, and who created it."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meal_details",
            "description": (
                "Get detailed meal breakdown for a specific date showing each "
                "food item, portion size, macros, and completion status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_str": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format. Defaults to today.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diet_adherence",
            "description": (
                "Get diet adherence trends: actual vs target calories, macro gaps, "
                "best/worst days, and average adherence percentage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to analyze. Default 7.",
                    },
                },
                "required": [],
            },
        },
    },
]
