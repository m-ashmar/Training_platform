"""
tasks.py - Background Task Management for Diet App

This module provides Celery tasks for generating AI-powered diet plans and daily advice.
Tasks are designed to run asynchronously and interact with the deterministic planner.
Enhanced with comprehensive data collection for AI training dataset creation.
Now supports meal count and snack preferences.
"""

from celery import shared_task
from django.contrib.auth import get_user_model
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _
from django.db import transaction
from .models import DietPlan, Meal, DailyAdvice, MealComponent
import logging
import json
from .exceptions import (
    HTTPTransientError,
    HTTPPermanentError,
    PersistenceError,
    ConstraintViolationError,
)
from .utils.logging_utils import log_json
from django.db import OperationalError, InterfaceError

# Transient failures worth retrying. These tasks previously used a bare @shared_task:
# no bind, no autoretry, no self.retry() — so ANY exception (a DB blip, an FCM 503, a
# broker hiccup) lost the job permanently and silently. `autoretry_for` gives them a
# retry policy without changing any signature; permanent errors still fail fast.
TRANSIENT_ERRORS = (
    OperationalError,
    InterfaceError,
    ConnectionError,
    TimeoutError,
)


logger = logging.getLogger(__name__)

@shared_task(
    bind=True,          # so the task can tell a retry from its last attempt
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def generate_rule_based_plan(self, user_id, meal_count=3, snack_count=1, start_date=None, duration_days=7):
    """The deterministic planner, off the request thread, for plans longer than the
    synchronous endpoint allows. Same planner, same persistence, same refusals.

    Returns a dict the caller can show: `{"plan_id": ...}` or `{"error": ..., "code": ...}`.
    A refusal (infeasible calories, an unservable slot, an incomplete profile) is the
    client's to act on and is not retried.
    """
    from django.contrib.auth import get_user_model

    from .services.plan_generation import (
        MAX_TASK_DAYS, InfeasibleCaloriesError, generate_and_save)
    from .services.rule_based_planner import NoServableMealError

    user = get_user_model().objects.get(id=user_id)

    def _refund(client):
        """Give the allowance back. It was claimed when the request was accepted, so
        every path that ends without a plan owes it."""
        from subscription import quota
        quota.release(client, "daily_meals")

    try:
        _output, plan = generate_and_save(
            user, meal_count=meal_count, snack_count=snack_count, start_date=start_date,
            duration_days=duration_days, max_days=MAX_TASK_DAYS)
    except InfeasibleCaloriesError as e:
        _refund(user)
        return {"error": str(e), "code": "infeasible_calories", "user_id": user_id,
                "max_feasible_kcal": round(e.ceiling), "suggestion": e.suggestion}
    except NoServableMealError as e:
        _refund(user)
        return {"error": str(e), "code": "no_servable_meal", "user_id": user_id}
    except ConstraintViolationError as e:
        # Two generations for one client raced, or the client already has a plan
        # covering those dates. Not a fault: the plan they wanted exists.
        _refund(user)
        return {"error": str(e), "code": "plan_already_exists", "user_id": user_id}
    except PersistenceError as e:
        if str(e).startswith("preferences_changed_during_generation"):
            # Their preferences changed mid-build; generating again is the answer, and
            # they should not be charged for a plan that was never served.
            _refund(user)
            return {"error": str(e), "code": "preferences_changed", "user_id": user_id}
        _refund(user)
        raise
    except (ValueError, PermissionError) as e:
        _refund(user)
        return {"error": str(e), "code": "invalid_request", "user_id": user_id}
    except TRANSIENT_ERRORS:
        # A retry re-enters this function from the top, where there is no
        # `quota.reserve` — the allowance was claimed once, by the view. Refunding on
        # every transient failure and letting a later attempt succeed means a plan built
        # with `usage_count` unchanged: a client on one plan a day gets unlimited plans
        # for as long as the database is flapping, which is exactly when the extra
        # optimiser runs hurt most. So the reservation stands through the retries and is
        # given back only when the task has given up for good.
        if self.request.retries >= (self.max_retries or 0):
            _refund(user)
        raise
    except Exception:
        # No retry is coming for this one. Nothing was built, so nothing is owed.
        _refund(user)
        raise
    return {"plan_id": plan.id, "user_id": user_id,
            "superseded_plan_ids": getattr(plan, "superseded_plan_ids", []),
            "replaced_a_plan_written_for_you": bool(
                getattr(plan, "superseded_authored_plan_ids", []))}


@shared_task(
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def generate_daily_advice(user_id=None):
    """
    Enhanced asynchronous task to generate daily dietary advice for a user.
    Now includes data collection for advice effectiveness tracking.

    Args:
        user_id (int, optional): ID of the user. If None, advice is generated for all users.
    """
    try:
        if user_id:
            users = [get_user_model().objects.get(id=user_id)]
        else:
            # Clients with an active plan, in batches. This was `objects.all()` —
            # every account on the platform, trainers and admins included, held in
            # memory, and one user whose profile raised aborted every remaining one.
            users = (get_user_model().objects
                     .filter(user_type='client', is_active=True,
                             diet_plans__is_active=True)
                     .distinct()
                     .iterator(chunk_size=200))
            
        for user in users:
            try:
                # Generate personalized advice based on user data
                advice_text = _generate_personalized_advice(user)
            
                # One row per client per day, created or refreshed. A bare `create()`
                # meant a redelivered sweep — `acks_late` plus `reject_on_worker_lost`
                # are both on — gave every client a second piece of advice, and a third.
                from training_platform.user_time import today_for

                today = today_for(user)
                advice, _created = DailyAdvice.objects.update_or_create(
                    user=user,
                    for_date=today,
                    defaults=dict(
                    text=advice_text,
                    context_data={
                        "advice_type": "daily_nutrition",
                        "generation_timestamp": timezone.now().isoformat(),
                        "user_metrics": {
                            "bmi": user.calculate_bmi() if hasattr(user, 'calculate_bmi') else None,
                            "daily_calories": user.calculate_daily_calories() if hasattr(user, 'calculate_daily_calories') else None,
                            "fitness_goal": user.resolve_fitness_goal()
                        },
                        "advice_effectiveness_tracking": {
                            "views_count": 0,
                            "engagement_score": 0,
                            "user_feedback": None
                        }
                    }),
                )
            
                logger.info(
                    f"Daily advice generated for user {user.id}",
                    extra={
                        "user_id": user.id,
                        "advice_id": advice.id,
                        "advice_type": "daily_nutrition"
                    }
                )
            except TRANSIENT_ERRORS:
                # The database, not this client: caught here, an outage was logged once
                # per client and the retry below never saw it.
                raise
            except Exception:
                # One user's bad profile must not cancel everyone else's
                # advice. This exception reached the task-level handler,
                # which logged once against user_id=None and stopped.
                logger.exception("daily advice failed for user %s", user.id)
                continue
            
    except TRANSIENT_ERRORS:
        # Let Celery retry. The blanket `except Exception` below swallowed these too, so
        # `autoretry_for=TRANSIENT_ERRORS` on the decorator could never fire and a
        # database blip silently cost every client that day's advice.
        raise
    except Exception as e:
        logger.error(
            f"Error generating daily advice for user {user_id}: {str(e)}",
            extra={"user_id": user_id, "task": "generate_daily_advice"}
        )

def _generate_personalized_advice(user):
    """
    Generate personalized dietary advice for a user.
    
    Args:
        user: User instance
        
    Returns:
        str: Personalized advice text
    """
    from django.utils import translation

    # In the CLIENT'S language, not the worker's. This was hardcoded English and
    # then persisted into `DailyAdvice.text`, so an Arabic client got English prose
    # and activating Arabic later could not fix the stored rows. Render it under
    # the client's own language so the text is right when it is written.
    language = (getattr(user, 'preferred_language', None)
                or getattr(settings, 'LANGUAGE_CODE', 'en'))
    try:
        # Get user metrics
        bmi = user.calculate_bmi() if hasattr(user, 'calculate_bmi') else None
        # The number the client's PLAN is built to. The profile formula is only a
        # fallback: a trainer's plan, or one generated at an earlier weight, has its own
        # target, and the advice named a different figure from the one on the plan
        # screen beside it.
        active_plan = (DietPlan.objects.filter(user=user, is_active=True)
                       .order_by('-created_at', '-id').only('daily_calories').first())
        if active_plan is not None and active_plan.daily_calories:
            daily_calories = round(float(active_plan.daily_calories))
        else:
            daily_calories = user.calculate_daily_calories() if hasattr(user, 'calculate_daily_calories') else None
        goal = user.resolve_fitness_goal()

        with translation.override(language):
            if goal == 'Lose':
                advice = _("Aim for a moderate deficit of 300 to 500 calories a day. "
                           "Your target is %(kcal)s calories.") % {"kcal": daily_calories}
            elif goal == 'Gain':
                advice = _("Eat %(kcal)s calories a day, with enough protein to build "
                           "muscle.") % {"kcal": daily_calories}
            else:
                advice = _("Keep your intake at %(kcal)s calories a day to hold your "
                           "weight.") % {"kcal": daily_calories}

            if bmi and bmi > 25:
                advice += " " + str(_("More vegetables and lean protein would help."))

        return str(advice)
        
    except TRANSIENT_ERRORS:
        raise
    except Exception as e:
        log_json(logger, "error", "Error generating personalized advice", error=str(e))
        with translation.override(language):
            return str(_("Stay hydrated and maintain a balanced diet with regular meals."))

