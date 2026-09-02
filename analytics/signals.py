"""Wire server-side analytics to the events that actually happen.

Guarded and deferred, like every other cross-app receiver: analytics is a side effect
and must never be able to fail a user's workout, meal or profile save.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .recorder import record_activity, record_metric

logger = logging.getLogger(__name__)


def _safe(fn):
    def wrapper(sender, instance, **kwargs):
        try:
            transaction.on_commit(lambda: fn(sender, instance, **kwargs))
        except Exception:
            logger.debug("analytics receiver %s failed", fn.__name__, exc_info=True)
    wrapper.__name__ = fn.__name__
    return wrapper


@receiver(post_save, sender="routine.WorkoutSession")
@_safe
def on_workout_completed(sender, instance, **kwargs):
    if instance.status != "completed":
        return
    from analytics.models import UserActivity

    # session_id is a column on UserActivity, not a metadata key — querying
    # metadata__session_id matched nothing and every re-save logged the workout again.
    if UserActivity.objects.filter(
        user_id=instance.user_id, activity_type="routine_completed",
        session_id=str(instance.pk),
    ).exists():
        return
    record_activity(instance.user, "routine_completed",
                    session_id=str(instance.pk), routine_id=instance.routine_id)
    duration = getattr(instance, "duration", None)
    if duration is not None:
        record_metric(instance.user, "workout_duration",
                      round(duration.total_seconds() / 60.0, 1), "min")


@receiver(post_save, sender="routine.ExerciseSetLog")
@_safe
def on_exercise_logged(sender, instance, created, **kwargs):
    if not created:
        return
    progress = instance.user_exercise_progress
    if progress is None:
        return
    record_activity(progress.user, "exercise_completed",
                    exercise_id=progress.exercise_id, set_number=instance.set_number)


@receiver(post_save, sender="diet.DietPlan")
@_safe
def on_diet_plan_generated(sender, instance, created, **kwargs):
    if not created:
        return
    record_activity(instance.user, "diet_plan_generated",
                    plan_id=instance.pk, goal=instance.goal)


@receiver(post_save, sender="diet.MealComponent")
@_safe
def on_meal_completed(sender, instance, **kwargs):
    if not getattr(instance, "is_completed", False):
        return
    from analytics.models import UserActivity

    plan = getattr(getattr(instance, "meal", None), "diet_plan", None)
    if plan is None:
        return
    if UserActivity.objects.filter(
        user_id=plan.user_id, activity_type="meal_completed",
        session_id=f"meal-component-{instance.pk}",
    ).exists():
        return
    record_activity(plan.user, "meal_completed",
                    session_id=f"meal-component-{instance.pk}",
                    component_id=instance.pk, food_id=instance.food_id)


@receiver(post_save, sender="users.CustomUser")
@_safe
def on_profile_weight_change(sender, instance, created, **kwargs):
    """Body weight is the series the weight-loss achievements are computed from."""
    if created or not instance.weight:
        return
    from analytics.models import PerformanceMetric

    latest = (PerformanceMetric.objects
              .filter(user_id=instance.pk, metric_type="weight")
              .order_by("-recorded_at").first())
    if latest and abs(float(latest.value) - float(instance.weight)) < 0.05:
        return
    record_metric(instance, "weight", instance.weight, "kg")
