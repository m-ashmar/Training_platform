"""
tasks.py - Background Task Management for Diet App

This module provides Celery tasks for generating AI-powered diet plans and daily advice.
Tasks are designed to run asynchronously and interact with DietGenerator and related services.
Enhanced with comprehensive data collection for AI training dataset creation.
Now supports meal count and snack preferences.
"""

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.db import transaction
from .models import DietPlan, Meal, DailyAdvice, MealComponent
from .ai_services import DietGenerator
from .meal_processor import MealProcessor
from .data_collection import TrainingDataCollector
import logging
import json
from .exceptions import (
    HTTPTransientError,
    HTTPPermanentError,
    OpenAIError,
    DietParsingError,
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

def _generate_rule_based_fallback(user_id, meal_count, snack_count, start_date):
    """Produce a plan with the deterministic planner when the AI path fails for good.

    Returns the plan id, or None if the fallback itself fails (in which case the
    caller re-raises the original error).
    """
    try:
        from django.contrib.auth import get_user_model

        from .services.rule_based_planner import RuleBasedPlanner
        from .services.diet_persistence import DietPersistenceService

        user = get_user_model().objects.get(id=user_id)
        daily_kcal = float(getattr(user, 'daily_calorie_target', 0) or 2000)
        planner = RuleBasedPlanner(user)
        output = planner.generate(
            daily_kcal=daily_kcal,
            meal_count=meal_count,
            snack_count=snack_count,
            start_date=start_date,
        )
        plan = DietPersistenceService(user).save_plan(output, meal_count, snack_count, start_date)
        logger.warning(
            "AI generation failed permanently for user %s; served a rule-based plan instead",
            user_id,
        )
        return getattr(plan, 'id', None)
    except Exception:
        logger.exception("Rule-based fallback also failed for user %s", user_id)
        return None


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def generate_ai_diet_plan(self, user_id, meal_count=3, snack_count=0, start_date=None):
    """
    Enhanced asynchronous task to generate a diet plan for a user using DietGenerator.
    Now includes comprehensive data collection for AI training dataset creation.
    Supports meal count and snack preferences.

    Args:
        user_id (int): ID of the user for whom to generate the plan.
        meal_count (int): Number of main meals per day (default: 3).
        snack_count (int): Number of snacks per day (default: 0).
    """
    try:
        user = get_user_model().objects.get(id=user_id)
        
        # Check if user is a trainer (trainers cannot use AI generation)
        if user.is_trainer:
            raise ValueError("Trainers cannot generate AI diet plans")
        
        # Initialize enhanced diet generator
        planner = DietGenerator(user)
        
        # Generate the plan with meal count and snack preferences
        plan_output = planner.generate_plan(meal_count, snack_count)
        
        # Save plan to database with full metadata
        diet_plan = planner.save_plan_to_database(
            plan_output,
            meal_count,
            snack_count,
            start_date=start_date
        )
        
        # Initialize data collector
        data_collector = TrainingDataCollector()
        
        # Collect comprehensive training data
        training_data = data_collector.collect_diet_plan_data(diet_plan, plan_output)
        
        # Store training data for batch processing
        _store_training_data.delay(training_data)
        
        # Log successful generation with comprehensive metrics
        log_json(
            logger,
            "info",
            "Diet plan generated and processed",
            user_id=user_id,
            diet_plan_id=diet_plan.id,
            meals_generated=len(plan_output.plan),
            meal_count=meal_count,
            snack_count=snack_count,
            data_quality_score=training_data["collection_metadata"]["data_quality_score"],
            generation_time=plan_output.generation_metadata["performance_metrics"]["generation_time"],
            task_id=self.request.id,
        )
        
        return {
            "status": "success",
            "diet_plan_id": diet_plan.id,
            "meals_count": len(plan_output.plan),
            "meal_count": meal_count,
            "snack_count": snack_count,
            "data_quality_score": training_data["collection_metadata"]["data_quality_score"],
            "training_data_id": training_data["entry_id"]
        }
        
    except (HTTPTransientError, OpenAIError) as e:
        log_json(
            logger,
            "error",
            "Transient error during plan generation",
            user_id=user_id,
            task="generate_ai_diet_plan",
            task_id=self.request.id,
            retry_count=self.request.retries,
            meal_count=meal_count,
            snack_count=snack_count,
            error=str(e),
        )
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60 * (2 ** self.request.retries))
        log_json(
            logger,
            "error",
            "Final failure after retries for transient error",
            user_id=user_id,
            error=str(e),
        )
        raise
    except (HTTPPermanentError, DietParsingError, ConstraintViolationError, PersistenceError, ValueError) as e:
        # Do not retry permanent or parsing/persistence errors
        log_json(
            logger,
            "error",
            "Permanent error during plan generation (no retry)",
            user_id=user_id,
            task="generate_ai_diet_plan",
            task_id=self.request.id,
            meal_count=meal_count,
            snack_count=snack_count,
            error=str(e),
        )
        # Permanent AI failure is not a reason for the user to get nothing. The
        # rule-based planner is fully capable of producing a plan and needs no LLM;
        # previously the task just re-raised and the request left no visible trace
        # beyond a log line.
        fallback = _generate_rule_based_fallback(user_id, meal_count, snack_count, start_date)
        if fallback is not None:
            return fallback
        raise
    except Exception as e:
        log_json(
            logger,
            "error",
            "Unexpected error during plan generation",
            user_id=user_id,
            task="generate_ai_diet_plan",
            task_id=self.request.id,
            retry_count=self.request.retries,
            meal_count=meal_count,
            snack_count=snack_count,
            error=str(e),
        )
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=60 * (2 ** self.request.retries))
        raise

@shared_task(
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def _store_training_data(training_data):
    """
    Background task to store training data for AI model development.
    
    Args:
        training_data (dict): Comprehensive training data entry.
    """
    try:
        # Store training data in persistent storage
        # This could be a database table, file system, or cloud storage
        training_entry_id = training_data["entry_id"]
        
        # For now, store in DailyAdvice with special context
        DailyAdvice.objects.create(
            user_id=training_data["user_profile"]["user_id"],
            text=f"Training data collected for AI model development - Entry ID: {training_entry_id}",
            context_data={
                "training_data": training_data,
                "data_type": "ai_training_dataset",
                "collection_timestamp": training_data["timestamp"]
            }
        )
        
        log_json(
            logger,
            "info",
            "Training data stored",
            training_entry_id=training_entry_id,
            user_id=training_data["user_profile"]["user_id"],
            data_quality_score=training_data["collection_metadata"]["data_quality_score"],
        )
        
    except Exception as e:
        log_json(
            logger,
            "error",
            "Failed to store training data",
            training_entry_id=training_data.get("entry_id", "unknown"),
            error=str(e),
        )
        raise

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

