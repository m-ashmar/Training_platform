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

    
    # These arguments come from the MODEL's function call, not from a validated
    # serializer, so a hallucinated "next tuesday" or a `days` of "seven" reached
    # `fromisoformat` and `timedelta` as-is and raised out of the tool. A tool answers
    # the model; it does not crash the conversation.
    target_date = timezone.localdate()
    if date_str:
        from datetime import date as dt_date
        try:
            target_date = dt_date.fromisoformat(str(date_str))
        except (TypeError, ValueError):
            return {"meals": [],
                    "message": str(_("I need the date as YYYY-MM-DD."))}

    plan = DietPlan.objects.filter(user=user, is_active=True).first()
    if not plan:
        return {"meals": [], "message": str(_("No active diet plan."))}

    meals = Meal.objects.filter(
        diet_plan=plan, date=target_date,
    ).prefetch_related('components__food').order_by('scheduled_time')
    

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


    plan = DietPlan.objects.filter(user=user, is_active=True).first()
    if not plan:
        return {"adherence": None, "message": str(_("No active diet plan."))}

    try:
        days = max(1, min(365, int(days)))
    except (TypeError, ValueError):
        return {"adherence": None,
                "message": str(_("I need the number of days as a whole number."))}
    since = timezone.localdate() - timedelta(days=days)
    progress_qs = DailyProgress.objects.filter(
        user=user, diet_plan=plan, date__gte=since,
    ).order_by('date')
    

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

    # `0` here read as "this client scored zero on adherence" for a client who has
    # simply never tracked a day, and the assistant answers from the first field it
    # finds. No data is not a score.
    avg_adherence = round(total_adherence / count, 1) if count else None

    # Find best and worst days
    # Closest to target, not the largest. Ranking by `calories_pct` reported a 200%
    # binge as the client's best adherence day and a fasting day as their worst — the
    # opposite of the adherence score computed a few lines above, which this tool then
    # hands to the model as fact.
    def _distance_from_target(day):
        return abs(day["calories_pct"] - 100)

    best_day = min(daily_data, key=_distance_from_target) if daily_data else None
    worst_day = max(daily_data, key=_distance_from_target) if daily_data else None

    payload = {
        "average_adherence_pct": avg_adherence,
        "days_tracked": count,
        "daily_data": daily_data,
        "best_day": best_day,
        "worst_day": worst_day,
        "period_days": days,
    }
    if not count:
        # Said in words as well as by a null, because the model reads the message.
        payload["message"] = str(_("This client has not tracked any days yet."))
    return payload


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
