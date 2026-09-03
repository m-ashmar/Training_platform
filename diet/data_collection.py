"""
data_collection.py - AI Training Data Collection and Management

This module provides comprehensive data collection services for building
training datasets from AI-generated diet plans and user interactions.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from django.utils import timezone
from django.db import transaction
from django.conf import settings
from django.core.cache import cache
from .models import DietPlan, Meal, MealComponent, DailyAdvice, UserFoodPreference
from .ai_services import DietGenerator, DietPlanOutput
from .meal_processor import MealProcessor

logger = logging.getLogger(__name__)

class TrainingDataCollector:
    """
    Comprehensive data collector for AI training dataset creation.
    """
    
    def __init__(self):
        self.logger = logger
        self.collection_metadata = {}
    
    def collect_diet_plan_data(self, diet_plan: DietPlan, plan_output: DietPlanOutput) -> Dict[str, Any]:
        """
        Collect comprehensive data from a generated diet plan.
        
        Args:
            diet_plan (DietPlan): The saved diet plan.
            plan_output (DietPlanOutput): The AI-generated plan output.
            
        Returns:
            Dict[str, Any]: Collected training data.
        """
        try:
            collection_start = timezone.now()
            
            # Collect user profile data
            user_data = self._collect_user_profile_data(diet_plan.user)
            
            # Collect plan generation data
            generation_data = self._collect_generation_data(plan_output)
            
            # Collect meal data
            meal_data = self._collect_meal_data(diet_plan)
            
            # Collect nutritional analysis
            nutrition_data = self._collect_nutrition_data(diet_plan)
            
            # Collect user feedback data (if available)
            feedback_data = self._collect_feedback_data(diet_plan)
            
            # Create comprehensive training entry
            training_entry = {
                "entry_id": f"diet_plan_{diet_plan.id}_{int(timezone.now().timestamp())}",
                "timestamp": collection_start.isoformat(),
                "collection_metadata": {
                    "collector_version": "2.0.0",
                    "collection_time": (timezone.now() - collection_start).total_seconds(),
                    "data_quality_score": self._calculate_data_quality_score(diet_plan, plan_output)
                },
                "user_profile": user_data,
                "generation_context": generation_data,
                "meal_data": meal_data,
                "nutrition_analysis": nutrition_data,
                "user_feedback": feedback_data,
                "validation_metrics": self._collect_validation_metrics(diet_plan, plan_output)
            }
            
            # Store in cache for batch processing
            self._store_training_entry(training_entry)
            
            # Log collection
            self.logger.info(
                f"Successfully collected training data for diet plan {diet_plan.id}",
                extra={
                    "diet_plan_id": diet_plan.id,
                    "user_id": diet_plan.user.id,
                    "meals_count": len(meal_data),
                    "data_quality_score": training_entry["collection_metadata"]["data_quality_score"]
                }
            )
            
            return training_entry
            
        except Exception as e:
            self.logger.error(
                f"Failed to collect diet plan data: {str(e)}",
                extra={
                    "diet_plan_id": diet_plan.id if diet_plan else None,
                    "user_id": diet_plan.user.id if diet_plan else None
                }
            )
            raise
    
    def _collect_user_profile_data(self, user) -> Dict[str, Any]:
        """
        Collect comprehensive user profile data.
        """
        try:
            preferences = UserFoodPreference.objects.get(user=user)
            
            return {
                "user_id": user.id,
                "demographics": {
                    "age": getattr(user, 'age', None),
                    "gender": getattr(user, 'gender', None),
                    "height": getattr(user, 'height', None),
                    "weight": getattr(user, 'weight', None),
                    "activity_level": getattr(user, 'activity_level', None),
                    "fitness_goal": user.resolve_fitness_goal(),
                    "dietary_restrictions": getattr(user, 'dietary_restrictions', None)
                },
                "health_metrics": {
                    "bmi": user.calculate_bmi() if hasattr(user, 'calculate_bmi') else None,
                    "bmr": user.calculate_bmr() if hasattr(user, 'calculate_bmr') else None,
                    "daily_calories": user.calculate_daily_calories() if hasattr(user, 'calculate_daily_calories') else None
                },
                "preferences": {
                    "liked_foods": [f.name for f in preferences.liked_foods.all()],
                    "disliked_foods": [f.name for f in preferences.disliked_foods.all()],
                    "allergies": preferences.allergies,
                    "cuisine_preferences": getattr(preferences, 'cuisine_preferences', []),
                    "cooking_skill_level": getattr(preferences, 'cooking_skill_level', 'Intermediate')
                },
                "historical_data": {
                    "previous_plans_count": DietPlan.objects.filter(user=user).count(),
                    "account_age_days": (timezone.now() - user.date_joined).days,
                    "last_plan_date": self._get_last_plan_date(user)
                }
            }
        except UserFoodPreference.DoesNotExist:
            return {
                "user_id": user.id,
                "demographics": {},
                "health_metrics": {},
                "preferences": {},
                "historical_data": {}
            }
    
    def _collect_generation_data(self, plan_output: DietPlanOutput) -> Dict[str, Any]:
        """
        Collect AI generation context and metadata.
        """
        return {
            "generation_metadata": plan_output.generation_metadata,
            "plan_metadata": plan_output.plan_metadata,
            "ai_model_info": {
                "model": "gpt-3.5-turbo-1106",
                "temperature": 0.3,
                "max_tokens": 4000,
                "generation_strategy": "nutritionist_chef"
            },
            "plan_characteristics": {
                "total_meals": len(plan_output.plan),
                "meal_types": list(set(meal.meal_type for meal in plan_output.plan if meal.meal_type)),
                "total_calories": sum(meal.total_nutrition.get('calories', 0) for meal in plan_output.plan),
                "avg_preparation_time": self._calculate_avg_preparation_time(plan_output.plan),
                "difficulty_distribution": self._calculate_difficulty_distribution(plan_output.plan)
            }
        }
    
    def _collect_meal_data(self, diet_plan: DietPlan) -> List[Dict[str, Any]]:
        """
        Collect detailed meal data with ingredients and processing information.
        """
        meals_data = []
        
        for meal in diet_plan.meals.prefetch_related('mealcomponent_set__food').all():
            # Process meal with enhanced processor
            meal_processor = MealProcessor()
            
            # Get meal components
            components = meal.mealcomponent_set.all()
            
            meal_data = {
                "meal_id": meal.id,
                "meal_name": meal.description,
                "meal_type": getattr(meal, 'meal_type', 'Lunch'),
                "template": meal.template,
                "date": meal.date.isoformat() if meal.date else None,
                "is_ai_generated": meal.is_ai_generated,
                "ingredients": [
                    {
                        "food_id": comp.food.id,
                        "food_name": comp.food.name,
                        "quantity": comp.quantity,
                        "meal_time": comp.meal_time,
                        "nutrition": {
                            "calories": comp.food.calories,
                            "protein": comp.food.protein,
                            "carbs": comp.food.carbs,
                            "fat": comp.food.fat
                        },
                        "is_ai_generated": comp.food.is_ai_generated,
                        "serving_size": comp.food.serving_size,
                        "image_url": comp.food.image_url
                    }
                    for comp in components
                ],
                "total_nutrition": self._calculate_meal_total_nutrition(components),
                "ingredient_count": components.count(),
                "processing_metadata": {
                    "processing_timestamp": timezone.now().isoformat(),
                    "ingredient_resolution_method": "enhanced_fuzzy_matching"
                }
            }
            
            meals_data.append(meal_data)
        
        return meals_data
    
    def _collect_nutrition_data(self, diet_plan: DietPlan) -> Dict[str, Any]:
        """
        Collect comprehensive nutritional analysis.
        """
        all_meals = diet_plan.meals.prefetch_related('mealcomponent_set__food').all()
        total_calories = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        
        meal_nutrition = []
        
        for meal in all_meals:
            components = meal.mealcomponent_set.all()
            meal_calories = sum(comp.food.calories for comp in components)
            meal_protein = sum(comp.food.protein for comp in components)
            meal_carbs = sum(comp.food.carbs for comp in components)
            meal_fat = sum(comp.food.fat for comp in components)
            
            total_calories += meal_calories
            total_protein += meal_protein
            total_carbs += meal_carbs
            total_fat += meal_fat
            
            meal_nutrition.append({
                "meal_id": meal.id,
                "calories": meal_calories,
                "protein": meal_protein,
                "carbs": meal_carbs,
                "fat": meal_fat,
                "protein_ratio": (meal_protein * 4 / meal_calories * 100) if meal_calories > 0 else 0,
                "carb_ratio": (meal_carbs * 4 / meal_calories * 100) if meal_calories > 0 else 0,
                "fat_ratio": (meal_fat * 9 / meal_calories * 100) if meal_calories > 0 else 0
            })
        
        return {
            "daily_totals": {
                "calories": total_calories,
                "protein": total_protein,
                "carbs": total_carbs,
                "fat": total_fat,
                "protein_ratio": (total_protein * 4 / total_calories * 100) if total_calories > 0 else 0,
                "carb_ratio": (total_carbs * 4 / total_calories * 100) if total_calories > 0 else 0,
                "fat_ratio": (total_fat * 9 / total_calories * 100) if total_calories > 0 else 0
            },
            "meal_breakdown": meal_nutrition,
            "nutritional_balance_score": self._calculate_nutritional_balance_score(total_protein, total_carbs, total_fat),
            "calorie_distribution": self._calculate_calorie_distribution(meal_nutrition)
        }
    
    def _collect_feedback_data(self, diet_plan: DietPlan) -> Dict[str, Any]:
        """
        Collect user feedback and interaction data.
        """
        # This would integrate with user feedback systems
        # For now, return placeholder data
        return {
            "user_rating": None,
            "completion_rate": None,
            "modifications_made": [],
            "feedback_comments": [],
            "interaction_metrics": {
                "views_count": 0,
                "shares_count": 0,
                "favorites_count": 0
            }
        }
    
    def _collect_validation_metrics(self, diet_plan: DietPlan, plan_output: DietPlanOutput) -> Dict[str, Any]:
        """
        Collect validation and quality metrics.
        """
        return {
            "nutrition_accuracy": self._validate_nutrition_accuracy(diet_plan, plan_output),
            "ingredient_availability": self._validate_ingredient_availability(diet_plan),
            "meal_variety_score": self._calculate_meal_variety_score(diet_plan),
            "preparation_feasibility": self._validate_preparation_feasibility(diet_plan),
            "dietary_compliance": self._validate_dietary_compliance(diet_plan),
            "overall_quality_score": self._calculate_overall_quality_score(diet_plan, plan_output)
        }
    
    def _calculate_data_quality_score(self, diet_plan: DietPlan, plan_output: DietPlanOutput) -> float:
        """
        Calculate overall data quality score for the training entry.
        """
        score = 0.0
        
        # User profile completeness
        if hasattr(diet_plan.user, 'age') and diet_plan.user.age:
            score += 0.2
        if hasattr(diet_plan.user, 'weight') and diet_plan.user.weight:
            score += 0.2
        
        # Plan completeness
        if plan_output.plan:
            score += 0.2
        
        # Nutritional data availability
        meals = diet_plan.meal_set.all()
        if meals.exists():
            components = meals[0].mealcomponent_set.all()
            if components.exists() and components[0].food.calories > 0:
                score += 0.2
        
        # Generation metadata
        if plan_output.generation_metadata:
            score += 0.2
        
        return min(score, 1.0)
    
    def _store_training_entry(self, training_entry: Dict[str, Any]):
        """
        Stage training entry for batch processing.

        Uses edamam_cache (DB4) as a temporary staging store, which is
        isolated from the session store (DB0) and the app response cache (DB2/3).
        TTL: 24 hours — enough for a nightly export job.

        NOTE for production: replace with a proper pipeline store (S3 / Celery task)
        so large JSON blobs don't consume Redis memory.
        """
        from training_platform.cache import edamam_cache
        cache_key = f"training_data_{training_entry['entry_id']}"
        edamam_cache().set(cache_key, training_entry, timeout=86400)  # 24 hours
    
    def export_training_dataset(self, start_date: Optional[datetime] = None, 
                               end_date: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        Export training dataset for AI model development.
        
        Args:
            start_date: Start date for data collection.
            end_date: End date for data collection.
            
        Returns:
            List[Dict[str, Any]]: Training dataset.
        """
        if not start_date:
            start_date = timezone.now() - timedelta(days=30)
        if not end_date:
            end_date = timezone.now()
        
        # Get diet plans in date range
        diet_plans = DietPlan.objects.filter(
            created_at__range=(start_date, end_date),
            generation_strategy='GPT'
        )
        
        training_dataset = []
        
        for diet_plan in diet_plans:
            try:
                # Reconstruct plan output from stored data
                plan_output = self._reconstruct_plan_output(diet_plan)
                
                # Collect training data
                training_entry = self.collect_diet_plan_data(diet_plan, plan_output)
                training_dataset.append(training_entry)
                
            except Exception as e:
                self.logger.error(f"Failed to process diet plan {diet_plan.id}: {str(e)}")
                continue
        
        return training_dataset
    
    def _reconstruct_plan_output(self, diet_plan: DietPlan) -> DietPlanOutput:
        """
        Reconstruct DietPlanOutput from stored diet plan data.
        """
        # This is a simplified reconstruction
        # In practice, you'd want to store the full plan_output separately
        from .ai_services import AIMeal, AIIngredient, DietPlanOutput
        
        meals = []
        for meal in diet_plan.meal_set.all():
            components = meal.mealcomponent_set.all()
            
            ingredients = []
            for comp in components:
                ingredient = AIIngredient(
                    name=comp.food.name,
                    quantity=comp.quantity,
                    estimated_calories=comp.food.calories,
                    estimated_protein=comp.food.protein,
                    estimated_carbs=comp.food.carbs,
                    estimated_fat=comp.food.fat
                )
                ingredients.append(ingredient)
            
            total_nutrition = self._calculate_meal_total_nutrition(components)
            
            ai_meal = AIMeal(
                meal_name=meal.description,
                description=meal.description,
                ingredients=ingredients,
                total_nutrition=total_nutrition,
                meal_type=getattr(meal, 'meal_type', 'Lunch')
            )
            meals.append(ai_meal)
        
        return DietPlanOutput(
            plan=meals,
            plan_metadata=diet_plan.generated_plan.get('plan_metadata', {}),
            generation_metadata=diet_plan.generated_plan.get('generation_metadata', {})
        )
    
    # Helper methods for data collection
    def _get_last_plan_date(self, user) -> Optional[str]:
        """Get the date of the user's last diet plan."""
        last_plan = DietPlan.objects.filter(user=user).order_by('-created_at').first()
        return last_plan.created_at.isoformat() if last_plan else None
    
    def _calculate_avg_preparation_time(self, meals) -> Optional[float]:
        """Calculate average preparation time for meals."""
        times = [meal.preparation_time for meal in meals if meal.preparation_time]
        return sum(times) / len(times) if times else None
    
    def _calculate_difficulty_distribution(self, meals) -> Dict[str, int]:
        """Calculate difficulty level distribution."""
        distribution = {'Easy': 0, 'Medium': 0, 'Hard': 0}
        for meal in meals:
            if meal.difficulty_level:
                distribution[meal.difficulty_level] += 1
        return distribution
    
    def _calculate_meal_total_nutrition(self, components) -> Dict[str, float]:
        """Calculate total nutrition for a meal."""
        return {
            'calories': sum(comp.food.calories for comp in components),
            'protein': sum(comp.food.protein for comp in components),
            'carbs': sum(comp.food.carbs for comp in components),
            'fat': sum(comp.food.fat for comp in components)
        }
    
    def _calculate_nutritional_balance_score(self, protein: float, carbs: float, fat: float) -> float:
        """Calculate nutritional balance score."""
        total_calories = protein * 4 + carbs * 4 + fat * 9
        if total_calories == 0:
            return 0.0
        
        protein_ratio = (protein * 4 / total_calories) * 100
        carb_ratio = (carbs * 4 / total_calories) * 100
        fat_ratio = (fat * 9 / total_calories) * 100
        
        # Ideal ratios: 30% protein, 50% carbs, 20% fat
        ideal_protein = 30
        ideal_carbs = 50
        ideal_fat = 20
        
        score = 1.0 - (
            abs(protein_ratio - ideal_protein) + 
            abs(carb_ratio - ideal_carbs) + 
            abs(fat_ratio - ideal_fat)
        ) / 100
        
        return max(0.0, score)
    
    def _calculate_calorie_distribution(self, meal_nutrition: List[Dict[str, Any]]) -> Dict[str, float]:
        """Calculate calorie distribution across meals."""
        total_calories = sum(meal['calories'] for meal in meal_nutrition)
        if total_calories == 0:
            return {}
        
        return {
            f"meal_{i+1}": (meal['calories'] / total_calories) * 100
            for i, meal in enumerate(meal_nutrition)
        }
    
    # Validation methods
    def _validate_nutrition_accuracy(self, diet_plan: DietPlan, plan_output: DietPlanOutput) -> float:
        """Validate nutrition accuracy between AI and calculated values."""
        # Implementation would compare AI-generated vs calculated nutrition
        return 0.85  # Placeholder
    
    def _validate_ingredient_availability(self, diet_plan: DietPlan) -> float:
        """Validate ingredient availability and accessibility."""
        # Implementation would check ingredient availability
        return 0.90  # Placeholder
    
    def _calculate_meal_variety_score(self, diet_plan: DietPlan) -> float:
        """Calculate meal variety score."""
        meals = diet_plan.meal_set.all()
        unique_ingredients = set()
        
        for meal in meals:
            for comp in meal.mealcomponent_set.all():
                unique_ingredients.add(comp.food.name)
        
        return min(len(unique_ingredients) / 20, 1.0)  # Normalize to 0-1
    
    def _validate_preparation_feasibility(self, diet_plan: DietPlan) -> float:
        """Validate meal preparation feasibility."""
        # Implementation would check preparation complexity
        return 0.88  # Placeholder
    
    def _validate_dietary_compliance(self, diet_plan: DietPlan) -> float:
        """Validate dietary compliance with user restrictions."""
        # Implementation would check against user dietary restrictions
        return 0.92  # Placeholder
    
    def _calculate_overall_quality_score(self, diet_plan: DietPlan, plan_output: DietPlanOutput) -> float:
        """Calculate overall quality score."""
        scores = [
            self._validate_nutrition_accuracy(diet_plan, plan_output),
            self._validate_ingredient_availability(diet_plan),
            self._calculate_meal_variety_score(diet_plan),
            self._validate_preparation_feasibility(diet_plan),
            self._validate_dietary_compliance(diet_plan)
        ]
        
        return sum(scores) / len(scores) 