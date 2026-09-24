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
    """Run `fn` after commit, and never let it reach the user's request.

    The try/except used to wrap only `on_commit(...)`, which merely REGISTERS the
    callback — it cannot raise anything the callback will raise. The body ran later,
    unguarded, and any failure inside it (a deleted parent row, a dropped connection,
    a schema change mid-deploy) propagated out of the atomic block and turned a meal
    completion the database had already committed into a 500 for the client. The
    guard has to be inside the callback, which is where the work happens.
    """
    def wrapper(sender, instance, **kwargs):
        def run():
            try:
                fn(sender, instance, **kwargs)
            except Exception:
                logger.warning("analytics receiver %s failed", fn.__name__, exc_info=True)

        try:
            transaction.on_commit(run)
        except Exception:
            logger.warning("could not schedule analytics receiver %s", fn.__name__,
                           exc_info=True)
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


# No receiver on `routine.ExerciseSetLog` (BACKEND_BUGS B-071). It wrote one
# `exercise_completed` row per logged SET, so a twenty-set workout was twenty-one rows of
# the activity log, and the achievement engine, which counts `activity_types.WORKOUT`
# rows as workouts, counted it as twenty-one workouts. The workout is the row: the
# `routine_completed` above, one per finished session, which the log shows with its day,
# duration and volume (`analytics/feed.py`). Rows already written stay as they are.


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
    """One `meal_completed` row per MEAL, written when its last food is ticked.

    It wrote one row per food, with no meal id, name or calories, so a lunch of four
    foods was four identical untitled rows in the activity log (BACKEND_BUGS B-071).
    Now the row is written once every component of the meal is completed — by ticking
    them one at a time or by `complete_entire_meal`, which saves each component — and
    carries the meal. A meal eaten only in part writes nothing.

    `session_id` stays `meal-component-<pk>` (the component whose tick finished the
    meal): `achievements/engine.py` `meal_count` reads a meal back out of that key, and
    counts distinct meals behind it, so old and new rows count the same way there.

    Once per meal, ever: no row is written while the meal already has one — a new one
    (`metadata.meal_id`) or an old per-food one (`meal-component-<pk>` of any of its
    foods). A completed food is saved again for other reasons (a rating), and without
    the second check a meal finished before this change would be logged again, dated
    the day it was rated.
    """
    if not getattr(instance, "is_completed", False):
        return
    from django.db.models import F, FloatField, Q, Sum
    from django.db.models.functions import Coalesce, NullIf

    from analytics.models import UserActivity
    from diet.models import MealComponent

    meal = getattr(instance, "meal", None)
    plan = getattr(meal, "diet_plan", None)
    if plan is None:
        return
    from django.db import transaction

    from diet.models import Meal

    # The check and the write under the MEAL's row lock — the one
    # `update_completion_status` takes. Two foods ticked together each run this on
    # commit; unlocked, both saw no row yet and both wrote one (the review, M2).
    with transaction.atomic():
        Meal.objects.select_for_update().filter(pk=meal.pk).first()
        components = MealComponent.objects.filter(meal_id=meal.pk)
        if components.filter(is_completed=False).exists():
            return
        keys = [f"meal-component-{pk}" for pk in components.values_list("pk", flat=True)]
        if UserActivity.objects.filter(
            Q(metadata__meal_id=meal.pk) | Q(session_id__in=keys),
            user_id=plan.user_id, activity_type="meal_completed",
        ).exists():
            return
        # What was EATEN, by the rule the day's totals use (`DailyProgress.compute_figures`):
        # the amount the client said, else the planned amount, per 100 g of the food.
        eaten = Coalesce(F("actual_quantity_consumed"), F("quantity"), output_field=FloatField())
        calories = components.aggregate(
            kcal=Sum(eaten / NullIf(F("food__serving_size_grams"), 0)
                     * F("food__calories")))["kcal"]
        record_activity(plan.user, "meal_completed",
                        session_id=f"meal-component-{instance.pk}",
                        meal_id=meal.pk, name=meal.name or "",
                        name_ar=meal.name_ar or meal.name or "",
                        meal_type=meal.meal_type,
                        date=meal.date.isoformat() if meal.date else None,
                        calories=round(float(calories or 0), 1))


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
