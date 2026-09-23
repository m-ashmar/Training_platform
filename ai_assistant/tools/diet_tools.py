"""
diet_tools.py — Diet plan, meal details, and adherence tools.

Queries DietPlan, Meal, MealComponent, DailyProgress to give the AI
visibility into the user's nutrition and adherence patterns.
"""

from datetime import timedelta
from django.utils import timezone
from django.utils.translation import gettext as _


def plan_day_adherence(user, plan, since, until=None):
    """Calorie adherence of `plan` from `since` to `until` (inclusive; today when
    omitted), over the plan's DAYS rather than over the days that happen to have a row.

    Since S-023 a `DailyProgress` row exists only for a day on which something was
    ticked, so averaging the rows averaged the good days: a client who logged 2 of 7
    days was told the adherence of those 2 (SIDE_BUGS S-091). Here every day the plan
    has meals on counts, and a day with no row scores 0. Today counts only once
    something is ticked — it is not over yet.

    Returns `{rows, days, tracked, scores, average}`: the rows by date, every day
    counted, the days that carry a tracked row, each day's score (100 = exactly on
    target), and their mean — None when nothing was tracked at all, because no data is
    not a score.
    """
    from diet.models import DailyProgress, Meal

    today = timezone.localdate()
    until = min(until or today, today)
    rows = {dp.date: dp for dp in DailyProgress.objects.filter(
        user=user, diet_plan=plan, date__gte=since, date__lte=until).order_by('date')}
    tracked = sorted(day for day, dp in rows.items() if dp.target_calories > 0)
    plan_days = set(Meal.objects.filter(
        diet_plan=plan, date__gte=since, date__lte=min(until, today - timedelta(days=1)),
    ).order_by().values_list('date', flat=True).distinct())
    days = sorted(plan_days | set(tracked))
    scores = {day: (max(0, 100 - abs(rows[day].calories_percentage - 100))
                    if day in rows and rows[day].target_calories > 0 else 0)
              for day in days}
    average = round(sum(scores.values()) / len(days), 1) if tracked else None
    return {"rows": rows, "days": days, "tracked": tracked, "scores": scores,
            "average": average}


def get_active_diet_plan(user, **kwargs):
    """Get the user's currently active diet plan summary: the one covering today."""
    from diet.models import DietPlan

    plan = DietPlan.governing(user).select_related('template', 'created_by').first()

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

    # The plan covering THAT day. The newest active plan is next week's as soon as a
    # trainer schedules it, and the client was told they had no meals today.
    plan = DietPlan.governing(user, target_date).first()
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
    from diet.models import DietPlan

    plan = DietPlan.governing(user).first()
    if not plan:
        return {"adherence": None, "message": str(_("No active diet plan."))}

    try:
        days = max(1, min(365, int(days)))
    except (TypeError, ValueError):
        return {"adherence": None,
                "message": str(_("I need the number of days as a whole number."))}
    since = timezone.localdate() - timedelta(days=days)
    adherence = plan_day_adherence(user, plan, since)

    daily_data = []
    for dp in adherence["rows"].values():
        daily_data.append({
            "date": dp.date.isoformat(),
            "calories_consumed": dp.calories_consumed,
            "calories_target": dp.target_calories,
            "calories_pct": dp.calories_percentage,
            "protein_pct": dp.protein_percentage,
            "carbs_pct": dp.carbs_percentage,
            "fat_pct": dp.fat_percentage,
            "meals_completed": dp.meals_completed,
            "total_meals": dp.total_meals,
        })

    count = len(adherence["tracked"])
    plan_days = len(adherence["days"])

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
        # Over every day of the plan in the period, an untracked one counting 0 — see
        # `plan_day_adherence`. `None`, never 0, for a client who has tracked nothing:
        # no data is not a score.
        "average_adherence_pct": adherence["average"],
        "days_tracked": count,
        "plan_days": plan_days,
        "daily_data": daily_data,
        "best_day": best_day,
        "worst_day": worst_day,
        "period_days": days,
    }
    if not count:
        # Said in words as well as by a null, because the model reads the message.
        payload["message"] = str(_("This client has not tracked any days yet."))
    elif count < plan_days:
        payload["message"] = str(_(
            "Only %(tracked)s of the %(days)s plan days were tracked; a day with "
            "nothing ticked counts as zero.") % {"tracked": count, "days": plan_days})
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
