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

logger = logging.getLogger(__name__)

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

@shared_task
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

@shared_task
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
                        "fitness_goal": getattr(user, 'fitness_goal', 'Maintain')
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

@shared_task
def export_training_dataset(start_date=None, end_date=None, output_format='json'):
    """
    Export training dataset for AI model development.
    
    Args:
        start_date (str, optional): Start date in ISO format.
        end_date (str, optional): End date in ISO format.
        output_format (str): Output format ('json' or 'csv').
    """
    try:
        # Collect training data from DailyAdvice entries
        queryset = DailyAdvice.objects.filter(
            context_data__data_type='ai_training_dataset'
        )
        
        if start_date:
            queryset = queryset.filter(generated_at__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(generated_at__date__lte=end_date)
        
        # Extract training data
        training_dataset = []
        for advice in queryset:
            training_data = advice.context_data.get('training_data', {})
            if training_data:
                training_dataset.append(training_data)
        
        # Calculate dataset quality summary
        quality_summary = _calculate_dataset_quality_summary(training_dataset)
        
        # Convert to requested format
        if output_format == 'csv':
            output_data = _convert_to_csv(training_dataset)
        else:
            output_data = training_dataset
        
        # Store exported dataset
        _store_exported_dataset.delay(output_data, output_format, start_date, end_date)
        
        log_json(
            logger,
            "info",
            "Training dataset export completed",
            dataset_size=len(training_dataset),
            output_format=output_format,
            quality_summary=quality_summary,
        )
        
        return {
            "status": "success",
            "dataset_size": len(training_dataset),
            "quality_summary": quality_summary
        }
        
    except Exception as e:
        log_json(logger, "error", "Error exporting training dataset", task="export_training_dataset", error=str(e))
        raise

@shared_task
def _store_exported_dataset(output_data, output_format, start_date, end_date):
    """
    Background task to store exported training dataset.
    
    Args:
        output_data: The exported dataset
        output_format (str): Output format
        start_date (str): Start date
        end_date (str): End date
    """
    try:
        # Store the exported dataset
        # This could be saved to a file, cloud storage, or database
        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        filename = f"training_dataset_{start_date}_{end_date}_{timestamp}.{output_format}"
        
        # For now, just log the export
        log_json(logger, "info", "Exported training dataset stored", filename=filename, format=output_format, data_size=len(str(output_data)))
        
    except Exception as e:
        log_json(logger, "error", "Failed to store exported dataset", error=str(e))
        raise

@shared_task
def analyze_diet_plan_effectiveness(diet_plan_id):
    """
    Analyze the effectiveness of a diet plan based on user progress and feedback.
    
    Args:
        diet_plan_id (int): ID of the diet plan to analyze.
    """
    try:
        diet_plan = DietPlan.objects.get(id=diet_plan_id)
        
        # Analyze plan metrics
        plan_metrics = _analyze_plan_metrics(diet_plan)
        
        # Analyze nutritional balance
        nutrition_analysis = _analyze_nutritional_balance(diet_plan)
        
        # Analyze ingredients
        ingredient_analysis = _analyze_ingredients(diet_plan)
        
        # Analyze user compatibility
        user_compatibility = _analyze_user_compatibility(diet_plan)
        
        # Calculate overall effectiveness score
        effectiveness_score = _calculate_effectiveness_score({
            'plan_metrics': plan_metrics,
            'nutrition_analysis': nutrition_analysis,
            'ingredient_analysis': ingredient_analysis,
            'user_compatibility': user_compatibility
        })
        
        # Store analysis results
        DailyAdvice.objects.create(
            user=diet_plan.user,
            text=f"Diet plan effectiveness analysis completed. Score: {effectiveness_score}/100",
            context_data={
                "analysis_type": "diet_plan_effectiveness",
                "diet_plan_id": diet_plan_id,
                "effectiveness_score": effectiveness_score,
                "plan_metrics": plan_metrics,
                "nutrition_analysis": nutrition_analysis,
                "ingredient_analysis": ingredient_analysis,
                "user_compatibility": user_compatibility,
                "analysis_timestamp": timezone.now().isoformat()
            }
        )
        
        log_json(logger, "info", "Diet plan effectiveness analysis completed", diet_plan_id=diet_plan_id, user_id=diet_plan.user.id, effectiveness_score=effectiveness_score)
        
        return {
            "status": "success",
            "effectiveness_score": effectiveness_score,
            "analysis_summary": {
                "plan_metrics": plan_metrics,
                "nutrition_analysis": nutrition_analysis,
                "ingredient_analysis": ingredient_analysis,
                "user_compatibility": user_compatibility
            }
        }
        
    except Exception as e:
        log_json(logger, "error", "Error analyzing diet plan effectiveness", diet_plan_id=diet_plan_id, task="analyze_diet_plan_effectiveness", error=str(e))
        raise

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
        goal = getattr(user, 'fitness_goal', 'Maintain')
        
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

def _convert_to_csv(training_dataset):
    """
    Convert training dataset to CSV format.
    
    Args:
        training_dataset (list): List of training data entries
        
    Returns:
        str: CSV formatted data
    """
    if not training_dataset:
        return ""
    
    # Extract headers from first entry
    headers = list(training_dataset[0].keys())
    
    # Create CSV content
    csv_lines = [','.join(headers)]
    
    for entry in training_dataset:
        row = []
        for header in headers:
            value = entry.get(header, '')
            # Handle nested dictionaries and lists
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            row.append(str(value))
        csv_lines.append(','.join(row))
    
    return '\n'.join(csv_lines)

def _calculate_dataset_quality_summary(training_dataset):
    """
    Calculate quality summary for training dataset.
    
    Args:
        training_dataset (list): List of training data entries
        
    Returns:
        dict: Quality summary
    """
    if not training_dataset:
        return {"total_entries": 0, "quality_score": 0}
    
    total_entries = len(training_dataset)
    complete_entries = 0
    
    for entry in training_dataset:
        # Check if entry has required fields
        required_fields = ['user_profile', 'generation_parameters', 'performance_metrics']
        if all(field in entry for field in required_fields):
            complete_entries += 1
    
    quality_score = (complete_entries / total_entries) * 100 if total_entries > 0 else 0
    
    return {
        "total_entries": total_entries,
        "complete_entries": complete_entries,
        "quality_score": round(quality_score, 2)
    }

def _analyze_plan_metrics(diet_plan):
    """
    Analyze basic plan metrics.
    
    Args:
        diet_plan: DietPlan instance
        
    Returns:
        dict: Plan metrics analysis
    """
    meals = diet_plan.meals.all()
    total_meals = meals.count()
    
    # Calculate completion rates
    completed_meals = sum(1 for meal in meals if meal.is_completed)
    completion_rate = (completed_meals / total_meals * 100) if total_meals > 0 else 0
    
    return {
        "total_meals": total_meals,
        "completed_meals": completed_meals,
        "completion_rate": round(completion_rate, 2),
        "plan_duration_days": (diet_plan.end_date - diet_plan.start_date).days
    }

def _analyze_nutritional_balance(diet_plan):
    """
    Analyze nutritional balance of the diet plan.
    
    Args:
        diet_plan: DietPlan instance
        
    Returns:
        dict: Nutritional analysis
    """
    meals = diet_plan.meals.all()
    total_calories = 0
    total_protein = 0
    total_carbs = 0
    total_fat = 0
    
    for meal in meals:
        nutrition = meal.calculate_nutrition()
        total_calories += nutrition['calories']
        total_protein += nutrition['protein']
        total_carbs += nutrition['carbs']
        total_fat += nutrition['fat']
    
    # Calculate macronutrient ratios
    if total_calories > 0:
        protein_ratio = (total_protein * 4 / total_calories) * 100
        carb_ratio = (total_carbs * 4 / total_calories) * 100
        fat_ratio = (total_fat * 9 / total_calories) * 100
    else:
        protein_ratio = carb_ratio = fat_ratio = 0
    
    return {
        "total_calories": round(total_calories, 1),
        "total_protein": round(total_protein, 1),
        "total_carbs": round(total_carbs, 1),
        "total_fat": round(total_fat, 1),
        "protein_ratio": round(protein_ratio, 1),
        "carb_ratio": round(carb_ratio, 1),
        "fat_ratio": round(fat_ratio, 1),
        "target_calories": diet_plan.daily_calories,
        "calorie_accuracy": round((total_calories / diet_plan.daily_calories) * 100, 1) if diet_plan.daily_calories > 0 else 0
    }

def _analyze_ingredients(diet_plan):
    """
    Analyze ingredients used in the diet plan.
    
    Args:
        diet_plan: DietPlan instance
        
    Returns:
        dict: Ingredient analysis
    """
    meals = diet_plan.meals.all()
    all_components = MealComponent.objects.filter(meal__in=meals)
    
    # Count unique foods
    unique_foods = all_components.values('food').distinct().count()
    total_components = all_components.count()
    
    # Analyze food categories
    category_counts = {}
    for component in all_components.select_related('food__category'):
        category = component.food.category.name if component.food.category else 'Uncategorized'
        category_counts[category] = category_counts.get(category, 0) + 1
    
    return {
        "unique_foods": unique_foods,
        "total_components": total_components,
        "variety_score": round((unique_foods / total_components) * 100, 2) if total_components > 0 else 0,
        "category_distribution": category_counts
    }

def _analyze_user_compatibility(diet_plan):
    """
    Analyze compatibility between diet plan and user preferences.
    
    Args:
        diet_plan: DietPlan instance
        
    Returns:
        dict: User compatibility analysis
    """
    try:
        preferences = diet_plan.user.userfoodpreference_set.first()
        if not preferences:
            return {"compatibility_score": 0, "reason": "No user preferences found"}
        
        meals = diet_plan.meals.all()
        all_components = MealComponent.objects.filter(meal__in=meals)
        
        # Check liked foods usage
        liked_foods_used = 0
        disliked_foods_used = 0
        
        for component in all_components:
            if component.food in preferences.liked_foods.all():
                liked_foods_used += 1
            elif component.food in preferences.disliked_foods.all():
                disliked_foods_used += 1
        
        total_components = all_components.count()
        
        if total_components > 0:
            liked_ratio = (liked_foods_used / total_components) * 100
            disliked_ratio = (disliked_foods_used / total_components) * 100
            compatibility_score = max(0, 100 - disliked_ratio + liked_ratio)
        else:
            compatibility_score = 0
        
        return {
            "compatibility_score": round(compatibility_score, 2),
            "liked_foods_used": liked_foods_used,
            "disliked_foods_used": disliked_foods_used,
            "total_components": total_components,
            "liked_ratio": round((liked_foods_used / total_components) * 100, 2) if total_components > 0 else 0,
            "disliked_ratio": round((disliked_foods_used / total_components) * 100, 2) if total_components > 0 else 0
        }
        
    except Exception as e:
        logger.error(f"Error analyzing user compatibility: {str(e)}")
        return {"compatibility_score": 0, "reason": f"Analysis error: {str(e)}"}

def _calculate_effectiveness_score(analysis):
    """
    Calculate overall effectiveness score from analysis components.
    
    Args:
        analysis (dict): Analysis results
        
    Returns:
        float: Effectiveness score (0-100)
    """
    try:
        # Weight different components
        weights = {
            'completion_rate': 0.3,
            'calorie_accuracy': 0.25,
            'variety_score': 0.2,
            'compatibility_score': 0.25
        }
        
        plan_metrics = analysis.get('plan_metrics', {})
        nutrition_analysis = analysis.get('nutrition_analysis', {})
        ingredient_analysis = analysis.get('ingredient_analysis', {})
        user_compatibility = analysis.get('user_compatibility', {})
        
        # Calculate weighted score
        score = (
            plan_metrics.get('completion_rate', 0) * weights['completion_rate'] +
            nutrition_analysis.get('calorie_accuracy', 0) * weights['calorie_accuracy'] +
            ingredient_analysis.get('variety_score', 0) * weights['variety_score'] +
            user_compatibility.get('compatibility_score', 0) * weights['compatibility_score']
        )
        
        return round(score, 2)
        
    except Exception as e:
        logger.error(f"Error calculating effectiveness score: {str(e)}")
        return 0