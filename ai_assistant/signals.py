"""
signals.py — User Behavior Event Signal Handlers

Listens to post_save signals across the platform and creates
UserBehaviorEvent records for the training data pipeline.
"""

import logging
from functools import wraps
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

def _track(fn):
    """Run a behaviour-tracking receiver without ever breaking the caller's save.

    These are post_save receivers on WorkoutSession, ExerciseSetLog, RoutineProgress,
    DietPlan and MealComponent — the core write paths. An exception here propagates out
    of the user's own save(), so a analytics outage was enough to stop someone logging
    their workout. `achievements/signals.py` already guards every receiver this way.
    """
    @wraps(fn)
    def wrapper(sender, instance, **kwargs):
        try:
            return fn(sender, instance, **kwargs)
        except Exception:
            logger.exception("Behaviour tracking failed in %s; user write unaffected", fn.__name__)
    return wrapper



@receiver(post_save, sender='routine.WorkoutSession')
@_track
def on_workout_session_save(sender, instance, **kwargs):
    """Track workout completion or abandonment."""
    if instance.status in ('completed', 'abandoned'):
        from ai_assistant.models import UserBehaviorEvent
        event_type = (
            'workout_completed' if instance.status == 'completed'
            else 'workout_abandoned'
        )
        # This receiver fires on EVERY save, not just the transition, so editing a note
        # on a finished session used to log a second 'workout_completed'. Every metric
        # built on these counts inflated by however often the row happened to be saved.
        if UserBehaviorEvent.objects.filter(
            user=instance.user,
            event_type=event_type,
            event_data__session_id=instance.pk,
        ).exists():
            return
        UserBehaviorEvent.objects.create(
            user=instance.user,
            event_type=event_type,
            event_data={
                'session_id': instance.pk,
                'routine_id': instance.routine_id,
                'routine_name': instance.routine.name if instance.routine_id else None,
                'duration_seconds': (
                    int(instance.duration.total_seconds())
                    if instance.duration else None
                ),
            },
        )


@receiver(post_save, sender='routine.ExerciseSetLog')
@_track
def on_set_logged(sender, instance, created, **kwargs):
    """Track each set logged."""
    if not created:
        return
    from ai_assistant.models import UserBehaviorEvent
    progress = instance.user_exercise_progress
    if not progress:
        return
    # `str(progress.exercise)` lazily fetched the Exercise row for EVERY set logged —
    # 5 queries per set on the hot workout-logging path. The ids are already in hand.
    UserBehaviorEvent.objects.create(
        user_id=progress.user_id,
        event_type='set_logged',
        event_data={
            'exercise_id': progress.exercise_id,
            'set_number': instance.set_number,
            'weight': instance.weight,
            'reps': instance.reps,
            'rpe': instance.rpe,
        },
    )


@receiver(post_save, sender='routine.RoutineProgress')
@_track
def on_routine_day_completed(sender, instance, **kwargs):
    """Track routine day completion."""
    if instance.status != 'completed':
        return
    from ai_assistant.models import UserBehaviorEvent
    # Avoid duplicate events: check if one already exists today
    if UserBehaviorEvent.objects.filter(
        user=instance.user,
        event_type='routine_day_completed',
        event_data__routine_id=instance.routine_id,
        event_data__day=instance.day,
    ).exists():
        return
    UserBehaviorEvent.objects.create(
        user_id=instance.user_id,
        event_type='routine_day_completed',
        event_data={
            'routine_id': instance.routine_id,
            'day': instance.day,
            'exercises_completed': instance.exercises_completed,
            'total_exercises': instance.total_exercises,
        },
    )


@receiver(post_save, sender='diet.DietPlan')
@_track
def on_diet_plan_created(sender, instance, created, **kwargs):
    """Track diet plan generation."""
    if not created:
        return
    from ai_assistant.models import UserBehaviorEvent
    UserBehaviorEvent.objects.create(
        user=instance.user,
        event_type='plan_generated',
        event_data={
            'plan_id': instance.id,
            'goal': instance.goal,
            'daily_calories': instance.daily_calories,
            'strategy': instance.generation_strategy,
        },
    )


@receiver(post_save, sender='diet.MealComponent')
@_track
def on_meal_completed(sender, instance, **kwargs):
    """Track meal component completion."""
    if not instance.is_completed:
        return
    from ai_assistant.models import UserBehaviorEvent
    meal = instance.meal
    diet_plan = meal.diet_plan
    # Fires on every save of a completed component, so ticking anything else on the row
    # re-logged the same meal. Keyed on the component id to log it exactly once.
    if UserBehaviorEvent.objects.filter(
        user_id=diet_plan.user_id,
        event_type='meal_completed',
        event_data__component_id=instance.pk,
    ).exists():
        return
    UserBehaviorEvent.objects.create(
        user_id=diet_plan.user_id,
        event_type='meal_completed',
        event_data={
            'component_id': instance.pk,
            'meal_type': meal.meal_type,
            'food_id': instance.food_id,
            'quantity': instance.quantity,
            'actual_quantity': instance.actual_quantity_consumed,
        },
    )
