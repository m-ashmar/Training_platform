"""
signals.py — User Behavior Event Signal Handlers

Listens to post_save signals across the platform and creates
UserBehaviorEvent records for the training data pipeline.
"""

import logging
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='routine.WorkoutSession')
def on_workout_session_save(sender, instance, created, **kwargs):
    """Track workout completion or abandonment."""
    if instance.status in ('completed', 'abandoned'):
        from ai_assistant.models import UserBehaviorEvent
        event_type = (
            'workout_completed' if instance.status == 'completed'
            else 'workout_abandoned'
        )
        UserBehaviorEvent.objects.create(
            user=instance.user,
            event_type=event_type,
            event_data={
                'routine_id': instance.routine_id,
                'routine_name': str(instance.routine),
                'duration_seconds': (
                    int(instance.duration.total_seconds())
                    if instance.duration else None
                ),
            },
        )


@receiver(post_save, sender='routine.ExerciseSetLog')
def on_set_logged(sender, instance, created, **kwargs):
    """Track each set logged."""
    if not created:
        return
    from ai_assistant.models import UserBehaviorEvent
    progress = instance.user_exercise_progress
    if not progress:
        return
    UserBehaviorEvent.objects.create(
        user=progress.user,
        event_type='set_logged',
        event_data={
            'exercise': str(progress.exercise),
            'set_number': instance.set_number,
            'weight': instance.weight,
            'reps': instance.reps,
            'rpe': instance.rpe,
        },
    )


@receiver(post_save, sender='routine.RoutineProgress')
def on_routine_day_completed(sender, instance, **kwargs):
    """Track routine day completion."""
    if instance.status != 'Completed':
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
        user=instance.user,
        event_type='routine_day_completed',
        event_data={
            'routine_id': instance.routine_id,
            'routine_name': str(instance.routine),
            'day': instance.day,
            'exercises_completed': instance.exercises_completed,
            'total_exercises': instance.total_exercises,
        },
    )


@receiver(post_save, sender='diet.DietPlan')
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
def on_meal_completed(sender, instance, **kwargs):
    """Track meal component completion."""
    if not instance.is_completed:
        return
    from ai_assistant.models import UserBehaviorEvent
    meal = instance.meal
    diet_plan = meal.diet_plan
    UserBehaviorEvent.objects.create(
        user=diet_plan.user,
        event_type='meal_completed',
        event_data={
            'meal_type': meal.meal_type,
            'food': str(instance.food),
            'quantity': instance.quantity,
            'actual_quantity': instance.actual_quantity_consumed,
        },
    )
