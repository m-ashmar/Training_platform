"""
tasks.py - Background Task Management for Diet App

This module provides Celery tasks for generating AI-powered diet plans and daily advice.
Tasks are designed to run asynchronously and interact with GPTDietPlanner and related services.
"""

from celery import shared_task
from django.contrib.auth import get_user_model
from .models import DietPlan, Meal, DailyAdvice
from .services import GPTDietPlanner
from training_platform.utils import get_logger, log_error

logger = get_logger('diet')

@shared_task
def generate_ai_diet_plan(user_id, meal_count=3):
    """
    Asynchronous task to generate a diet plan for a user using GPTDietPlanner.

    Args:
        user_id (int): ID of the user for whom to generate the plan.
        meal_count (int): Number of meals per day (default: 3).
    """
    try:
        user = get_user_model().objects.get(id=user_id)
        planner = GPTDietPlanner(user)
        planner.generate_plan()
        logger.info(f"Diet plan generated for user {user_id} with {meal_count} meals.")
    except Exception as e:
        log_error(logger, e, {"user_id": user_id, "task": "generate_ai_diet_plan"})

@shared_task
def generate_daily_advice(user_id=None):
    """
    Asynchronous task to generate daily dietary advice for a user.

    Args:
        user_id (int, optional): ID of the user. If None, advice is generated for all users.
    """
    try:
        if user_id:
            users = [get_user_model().objects.get(id=user_id)]
        else:
            users = get_user_model().objects.all()
        for user in users:
            # Placeholder for AI advice generation logic
            advice_text = f"Stay hydrated and eat balanced meals, {user.username}!"
            DailyAdvice.objects.create(user=user, text=advice_text, context_data={})
            logger.info(f"Daily advice generated for user {user.id}.")
    except Exception as e:
        log_error(logger, e, {"user_id": user_id, "task": "generate_daily_advice"})