"""
training_tools.py — Exercise progress, workout history, and volume tools.

Queries UserExerciseProgress, ExerciseSetLog, WorkoutSession to give the AI
visibility into the user's training performance and trends.
"""

from datetime import timedelta
from django.db.models import Sum, Avg, Max, Count, F
from django.utils import timezone


def get_exercise_progress(user, exercise_name=None, days=30, **kwargs):
    """
    Get progress data for a specific exercise or all exercises.
    Returns weight/rep/volume trends.
    """
    from routine.models import UserExerciseProgress, ExerciseSetLog

    since = timezone.localdate() - timedelta(days=days)
    qs = UserExerciseProgress.objects.filter(
        user=user, date__gte=since, skipped=False,
    ).select_related('exercise')

    if exercise_name:
        qs = qs.filter(exercise__name__icontains=exercise_name)

    results = []
    for prog in qs.order_by('-date')[:20]:
        set_logs = prog.set_logs.all().order_by('set_number')
        sets_data = [
            {
                "set": log.set_number,
                "weight_kg": log.weight,
                "reps": log.reps,
                "rpe": log.rpe,
            }
            for log in set_logs
        ]
        results.append({
            "exercise": prog.exercise.name,
            "date": prog.date.isoformat(),
            "completed_sets": prog.completed_sets,
            "target_sets": prog.target_sets,
            "volume": prog.calculate_training_volume(),
            "sets": sets_data,
        })

    return {"progress": results, "period_days": days}


def get_workout_history(user, days=14, **kwargs):
    """
    Get recent workout sessions with exercises and durations.
    """
    from routine.models import WorkoutSession

    since = timezone.now() - timedelta(days=days)
    sessions = WorkoutSession.objects.filter(
        user=user, start_time__gte=since,
    ).select_related('routine').prefetch_related(
        'set_logs__user_exercise_progress__exercise',
    ).order_by('-start_time')[:10]

    results = []
    for session in sessions:
        duration_min = None
        if session.duration:
            duration_min = round(session.duration.total_seconds() / 60)

        # Group sets by exercise
        exercises_done = {}
        for log in session.set_logs.all():
            progress = log.user_exercise_progress
            if not progress:
                continue
            ex_name = progress.exercise.name
            if ex_name not in exercises_done:
                exercises_done[ex_name] = {"sets": 0, "best_weight": 0, "total_reps": 0}
            exercises_done[ex_name]["sets"] += 1
            exercises_done[ex_name]["best_weight"] = max(
                exercises_done[ex_name]["best_weight"], log.weight or 0,
            )
            exercises_done[ex_name]["total_reps"] += log.reps or 0

        results.append({
            "date": session.start_time.strftime("%Y-%m-%d"),
            "routine": str(session.routine),
            "status": session.status,
            "duration_minutes": duration_min,
            "exercises": exercises_done,
        })

    return {"sessions": results, "period_days": days}


def get_training_volume(user, days=28, **kwargs):
    """
    Aggregate weekly training volume by muscle group.
    """
    from routine.models import ExerciseSetLog, UserExerciseProgress

    print(f"[AI Tool] get_training_volume called for {user.username}, days={days}")
    
    since = timezone.localdate() - timedelta(days=days)

    # FIXED: user_exercise_progress__exercise__muscle_group -> target_muscle
    volume_by_muscle = (
        ExerciseSetLog.objects.filter(
            user_exercise_progress__user=user,
            date__gte=since,
        )
        .values(muscle=F('user_exercise_progress__exercise__target_muscle'))
        .annotate(
            total_volume=Sum(F('weight') * F('reps')),
            total_sets=Count('id'),
            avg_weight=Avg('weight'),
        )
        .order_by('-total_volume')
    )
    
    print(f"[AI Tool] Volume query retrieved {len(volume_by_muscle)} muscle groups")

    return {
        "volume_by_muscle": [
            {
                "muscle_group": row["muscle"],
                "total_volume_kg": round(row["total_volume"] or 0),
                "total_sets": row["total_sets"],
                "avg_weight_kg": round(row["avg_weight"] or 0, 1),
            }
            for row in volume_by_muscle
        ],
        "period_days": days,
    }


# --- OpenAI Function Schemas ---

TRAINING_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_exercise_progress",
            "description": (
                "Get weight, rep, and volume progression for a specific exercise "
                "or all exercises over a time period. Shows set-by-set details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "exercise_name": {
                        "type": "string",
                        "description": "Name of the exercise (partial match). Omit for all exercises.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back. Default 30.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_workout_history",
            "description": (
                "Get recent workout sessions including exercises performed, "
                "weights used, reps, and session duration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back. Default 14.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_training_volume",
            "description": (
                "Get total training volume (weight × reps) aggregated by muscle "
                "group over a time period. Useful for identifying imbalances."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back. Default 28.",
                    },
                },
                "required": [],
            },
        },
    },
]
