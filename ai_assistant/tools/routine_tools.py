"""
routine_tools.py — Routine schedule and progress tools.

Queries Routine, RoutineExercise, RoutineProgress to show the AI
what the user's current training plan looks like and how far along they are.
"""

from django.utils.translation import gettext as _


def get_routine_schedule(user, **kwargs):
    """Get the user's active routines with day-by-day exercise breakdown."""
    from routine.models import Routine, RoutineExercise

    print(f"[AI Tool] get_routine_schedule called for user: {user.username}")
    
    routines = Routine.objects.filter(
        assigned_to=user, is_active=True,
    ).prefetch_related(
        'routine_exercises__exercise',
    ).order_by('-created_at')[:3]

    if not routines:
        print("[AI Tool] No active routines found.")
        return {"routines": [], "message": str(_("No active routines found."))}

    results = []
    for routine in routines:
        print(f"[AI Tool] Processing routine: {routine.name}")
        days = {}
        for re in routine.routine_exercises.all().order_by('day', 'order'):
            day_key = f"Day {re.day}"
            if day_key not in days:
                days[day_key] = []
            
            # FIXED: muscle_group -> target_muscle
            # FIXED: rest_seconds -> rest_time
            # FIXED: weight_kg -> weight
            days[day_key].append({
                "exercise": re.exercise.name,
                "muscle_group": re.exercise.target_muscle, 
                "sets": re.sets,
                "reps": re.reps,
                "rest_seconds": re.rest_time,
                "weight_kg": re.weight,
                "order": re.order,
            })

        results.append({
            "name": routine.name,
            "days_in_plan": routine.days,
            "difficulty": routine.difficulty_level,
            "duration_minutes": routine.estimated_duration,
            "start_date": routine.start_date.isoformat() if routine.start_date else None,
            "end_date": routine.end_date.isoformat() if routine.end_date else None,
            "schedule": days,
        })

    return {"routines": results}


def get_routine_progress(user, **kwargs):
    """Get completion status for each day across active routines."""
    from routine.models import Routine, RoutineProgress

    routines = Routine.objects.filter(
        assigned_to=user, is_active=True,
    )

    results = []
    for routine in routines:
        progress_qs = RoutineProgress.objects.filter(
            user=user, routine=routine,
        ).order_by('day')

        days_progress = []
        completed_days = 0
        total_days = 0

        for rp in progress_qs:
            total_days += 1
            if rp.status == 'Completed':
                completed_days += 1
            days_progress.append({
                "day": rp.day,
                "status": rp.status,
                "exercises_completed": rp.exercises_completed,
                "total_exercises": rp.total_exercises,
                "completion_pct": rp.completion_percentage,
            })

        overall_pct = round((completed_days / total_days * 100)) if total_days else 0

        results.append({
            "routine": routine.name,
            "overall_completion_pct": overall_pct,
            "completed_days": completed_days,
            "total_days": total_days,
            "days": days_progress,
        })

    return {"routine_progress": results}


# --- OpenAI Function Schemas ---

ROUTINE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_routine_schedule",
            "description": (
                "Get the user's active training routines with the full day-by-day "
                "exercise schedule including sets, reps, rest times, and weights."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_routine_progress",
            "description": (
                "Get the user's completion status for each day in their active "
                "routines. Shows overall completion percentage and per-day details."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
