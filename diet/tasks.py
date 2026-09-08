"""
tasks.py - Background Task Management for Diet App

This module provides Celery tasks for generating AI-powered diet plans and daily advice.
Tasks are designed to run asynchronously and interact with the deterministic planner.
Enhanced with comprehensive data collection for AI training dataset creation.
Now supports meal count and snack preferences.
"""

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.db import transaction
from .models import DietPlan, Meal, DailyAdvice, MealComponent
from .meal_processor import MealProcessor
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
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def generate_rule_based_plan(user_id, meal_count=3, snack_count=1, start_date=None, duration_days=7):
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
    try:
        _output, plan = generate_and_save(
            user, meal_count=meal_count, snack_count=snack_count, start_date=start_date,
            duration_days=duration_days, max_days=MAX_TASK_DAYS)
    except InfeasibleCaloriesError as e:
        return {"error": str(e), "code": "infeasible_calories", "user_id": user_id,
                "max_feasible_kcal": round(e.ceiling), "suggestion": e.suggestion}
    except NoServableMealError as e:
        return {"error": str(e), "code": "no_servable_meal", "user_id": user_id}
    except (ValueError, PermissionError) as e:
        return {"error": str(e), "code": "invalid_request", "user_id": user_id}
    # The quota is spent when a plan exists, not when the request was accepted.
    from subscription import quota
    quota.record_on_commit(user, "daily_meals")
    return {"plan_id": plan.id, "user_id": user_id}


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
            users = get_user_model().objects.all()
            
        for user in users:
            # Generate personalized advice based on user data
            advice_text = _generate_personalized_advice(user)
            
            # Create advice with comprehensive context data
            advice = DailyAdvice.objects.create(
                user=user,
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
                }
            )
            
            logger.info(
                f"Daily advice generated for user {user.id}",
                extra={
                    "user_id": user.id,
                    "advice_id": advice.id,
                    "advice_type": "daily_nutrition"
                }
            )
            
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
    try:
        # Get user metrics
        bmi = user.calculate_bmi() if hasattr(user, 'calculate_bmi') else None
        daily_calories = user.calculate_daily_calories() if hasattr(user, 'calculate_daily_calories') else None
        goal = user.resolve_fitness_goal()
        
        # Generate advice based on user profile
        if goal == 'Lose':
            advice = f"Focus on creating a moderate calorie deficit of 300-500 calories per day. Your target is {daily_calories} calories."
        elif goal == 'Gain':
            advice = f"Ensure you're consuming {daily_calories} calories daily with adequate protein for muscle growth."
        else:
            advice = f"Maintain your current calorie intake of {daily_calories} calories to sustain your weight."
        
        if bmi and bmi > 25:
            advice += " Consider incorporating more vegetables and lean proteins into your meals."
        
        return advice
        
    except Exception as e:
        log_json(logger, "error", "Error generating personalized advice", error=str(e))
        return "Stay hydrated and maintain a balanced diet with regular meals."

