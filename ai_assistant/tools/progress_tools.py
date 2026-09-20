"""
progress_tools.py — Combined progress overview tool.

Aggregates data across training, diet, and routines to give the AI
a holistic view of the user's fitness journey.
"""

from datetime import timedelta
from django.db.models import Count, Q
from django.utils import timezone


def get_overall_progress(user, days=30, **kwargs):
    """
    Get a combined fitness summary: training consistency, volume trends,
    diet adherence, and routine completion.
    """
    from routine.models import (
        WorkoutSession, UserExerciseProgress, RoutineProgress, Routine,
    )
    from diet.models import DailyProgress, DietPlan

    
    since_date = timezone.localdate() - timedelta(days=days)
    since_dt = timezone.now() - timedelta(days=days)

    # --- Training ---
    workout_count = WorkoutSession.objects.filter(
        user=user, status='completed', start_time__gte=since_dt,
    ).count()

    exercise_days = UserExerciseProgress.objects.filter(
        user=user, date__gte=since_date, skipped=False,
    ).values('date').distinct().count()

    # Training consistency: days worked out / total days
    consistency_pct = round((exercise_days / max(days, 1)) * 100, 1)

    # --- Diet ---
    plan = DietPlan.objects.filter(user=user, is_active=True).first()
    diet_summary = None
    if plan:
        diet_progress = DailyProgress.objects.filter(
            user=user, diet_plan=plan, date__gte=since_date,
        )
        diet_days = diet_progress.count()
        completed_diet_days = diet_progress.filter(is_day_completed=True).count()
        
        # Calculate caloric adherence
        total_cal_pct = 0
        cal_days_count = 0
        for dp in diet_progress:
            if dp.target_calories > 0:
                # Cap at 100% for "adherence" (overeating isn't adherence)
                # Actually, strictly adherence means closeness to 100%. 
                # Let's use the same logic as diet_tools: 100 - abs(pct - 100)
                cal_pct = dp.calories_percentage
                score = max(0, 100 - abs(cal_pct - 100))
                total_cal_pct += score
                cal_days_count += 1
                
        avg_cal_adherence = round(total_cal_pct / cal_days_count, 1) if cal_days_count else 0
        
        diet_summary = {
            "days_tracked": diet_days,
            "days_completed": completed_diet_days,
            "meal_completion_pct": (
                round((completed_diet_days / diet_days * 100), 1)
                if diet_days else 0
            ),
            "caloric_adherence_pct": avg_cal_adherence,
            "adherence_pct": avg_cal_adherence, # For compatibility/clarity
        }

    # --- Routines ---
    active_routines = Routine.objects.filter(
        assigned_to=user, is_active=True,
    ).count()

    routine_completion = RoutineProgress.objects.filter(
        user=user,
        routine__is_active=True,
    ).aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status='completed')),
    )
    routine_pct = (
        round((routine_completion['completed'] / routine_completion['total'] * 100), 1)
        if routine_completion['total'] else 0
    )

    return {
        "period_days": days,
        "training": {
            "workouts_completed": workout_count,
            "days_trained": exercise_days,
            "consistency_pct": consistency_pct,
        },
        "diet": diet_summary,
        "routines": {
            "active_count": active_routines,
            "overall_completion_pct": routine_pct,
            "completed_days": routine_completion['completed'],
            "total_days": routine_completion['total'],
        },
        # How much of the coach's plan was actually done over the same period, so the
        # assistant answers "did I finish my sets" with numbers rather than "you trained
        # 4 days" (the owner, 2026-09-20; routine/outcomes.py).
        "plan_adherence": _plan_adherence(user, days),
        # And which way the lifting is going, so "am I getting stronger?" is answered
        # from the sets themselves (routine/strength.py). A projection is a RANGE and
        # only appears when there is enough training behind it; never a promise, and
        # never a claim about the body.
        "strength": _strength(user),
    }


def _strength(user, limit=5):
    from routine import strength as strength_module

    overview = strength_module.overview(user, limit=limit)
    return {
        "tracked_exercises": overview['tracked_count'],
        "rising_exercises": overview['rising_count'],
        "top_movers": [
            {"exercise": row.get('exercise_name'),
             "change_percent": row.get('change_percent'),
             "direction": row.get('direction'),
             "best_kg": (row.get('best') or {}).get('weight'),
             "projection": {k: v for k, v in (row.get('projection') or {}).items()
                            if k in ('available', 'low', 'high', 'horizon_days',
                                     'confidence', 'reason')}}
            for row in overview['top_movers']
        ],
        "plateaus": [row.get('exercise_name') for row in overview['plateaus']],
    }


def _plan_adherence(user, days):
    from datetime import timedelta

    from django.utils import timezone

    from routine.outcomes import adherence

    today = timezone.localdate()
    week = adherence(user, today - timedelta(days=days - 1), today, today=today)
    return {
        "days_closed": week['days_closed'],
        "perfect_days": week['perfect_days'],
        "short_days": week['short_days'],
        "sets_planned": week['sets']['planned'],
        "sets_logged": week['sets']['logged'],
        "sets_missed": week['sets']['missed'],
        "sets_done_pct": week['sets']['done_percent'],
        "reps_missed": week['reps']['missed'],
        "perfect_day_streak": week['perfect_streak']['current'],
        "approximate": week['approximate'],
    }


# --- OpenAI Function Schemas ---

PROGRESS_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_overall_progress",
            "description": (
                "Get a holistic fitness summary combining training consistency, "
                "workout count, diet adherence, and routine completion percentage "
                "over a specified period."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to analyze. Default 30.",
                    },
                },
                "required": [],
            },
        },
    },
]
