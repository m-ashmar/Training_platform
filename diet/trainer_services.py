"""
trainer_services.py - Trainer Diet Plan Management Services

This module provides services for trainers to create and manage diet plans for their clients.
Includes template management, meal scheduling, and nutritional calculations.
"""

from typing import List, Dict, Any, Optional
from datetime import date, timedelta, time
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import (
    DietPlan, DietPlanTemplate, Meal, MealComponent, 
    FoodItem, DailyProgress
)
from users.models import CustomUser

class TrainerDietPlanService:
    """
    Service for trainers to create and manage diet plans for their clients.
    """
    
    def __init__(self, trainer: CustomUser):
        """
        Initialize the service for a specific trainer.
        
        Args:
            trainer: The trainer user instance
        """
        if not trainer.is_trainer:
            raise ValidationError("User must be a trainer to use this service")
        self.trainer = trainer
    
    def get_available_templates(self) -> List[DietPlanTemplate]:
        """
        Get all available diet plan templates.
        
        Returns:
            List of active diet plan templates
        """
        return DietPlanTemplate.objects.filter(is_active=True).order_by('meals_per_day', 'snacks_per_day')
    
    def get_client_food_items(self, search_query: str = None, category: str = None) -> List[FoodItem]:
        """
        Get food items available for diet plan creation with optional filtering.
        
        Args:
            search_query: Optional search term for food names
            category: Optional category filter
            
        Returns:
            List of matching food items
        """
        queryset = FoodItem.objects.select_related('category').all()
        
        if search_query:
            queryset = queryset.filter(name__icontains=search_query)
        
        if category:
            queryset = queryset.filter(category__name__icontains=category)
        
        return queryset.order_by('name')[:100]  # Limit results
    
    def create_diet_plan(
        self,
        client: CustomUser,
        template: DietPlanTemplate,
        start_date: date,
        duration_weeks: int = 4,
        goal: str = 'Maintain',
        daily_calories: Optional[float] = None
    ) -> DietPlan:
        """
        Create a new diet plan for a client using a template.
        
        Args:
            client: The client user
            template: The diet plan template to use
            start_date: Start date for the plan
            duration_weeks: Duration in weeks
            goal: Fitness goal (Lose, Maintain, Gain)
            daily_calories: Daily calorie target (auto-calculated if None)
            
        Returns:
            Created DietPlan instance
        """
        # Validate trainer-client relationship
        if not self._can_manage_client(client):
            raise ValidationError("You can only create diet plans for your assigned clients")
        
        # Calculate daily calories if not provided
        if daily_calories is None:
            daily_calories = client.calculate_daily_calories(goal)
        
        # Calculate end date
        end_date = start_date + timedelta(weeks=duration_weeks)
        
        # Create the diet plan
        diet_plan = DietPlan.objects.create(
            user=client,
            goal=goal,
            daily_calories=daily_calories,
            start_date=start_date,
            end_date=end_date,
            duration_weeks=duration_weeks,
            generation_strategy='TRAINER',
            template=template,
            created_by=self.trainer
        )
        
        return diet_plan
    
    def add_meal_to_plan(
        self,
        diet_plan: DietPlan,
        meal_type: str,
        target_date: date,
        food_items: List[Dict[str, Any]],
        scheduled_time: Optional[time] = None,
        description: str = ""
    ) -> Meal:
        """
        Add a meal to a diet plan.
        
        Args:
            diet_plan: The diet plan to add the meal to
            meal_type: Type of meal (Breakfast, Lunch, Dinner, Snack)
            target_date: Date for the meal
            food_items: List of dicts with 'food_id' and 'quantity' keys
            scheduled_time: Optional scheduled time for the meal
            description: Optional meal description
            
        Returns:
            Created Meal instance
        """
        # Validate trainer can manage this plan
        if diet_plan.created_by != self.trainer:
            raise ValidationError("You can only modify diet plans you created")
        
        # Create the meal
        meal = Meal.objects.create(
            diet_plan=diet_plan,
            meal_type=meal_type,
            date=target_date,
            scheduled_time=scheduled_time,
            description=description,
            is_ai_generated=False
        )
        
        # Add meal components
        for food_data in food_items:
            food_item = FoodItem.objects.get(id=food_data['food_id'])
            MealComponent.objects.create(
                meal=meal,
                food=food_item,
                quantity=food_data['quantity'],
                meal_time=meal_type
            )
        
        return meal
    
    def update_meal(
        self,
        meal: Meal,
        food_items: List[Dict[str, Any]] = None,
        scheduled_time: Optional[time] = None,
        description: str = None
    ) -> Meal:
        """
        Update an existing meal.
        
        Args:
            meal: The meal to update
            food_items: New food items (optional)
            scheduled_time: New scheduled time (optional)
            description: New description (optional)
            
        Returns:
            Updated Meal instance
        """
        # Validate trainer can manage this meal
        if meal.diet_plan.created_by != self.trainer:
            raise ValidationError("You can only modify meals in plans you created")
        
        # Update meal fields
        if scheduled_time is not None:
            meal.scheduled_time = scheduled_time
        if description is not None:
            meal.description = description
        meal.save()
        
        # Update food items if provided
        if food_items is not None:
            # Remove existing components
            meal.components.all().delete()
            
            # Add new components
            for food_data in food_items:
                food_item = FoodItem.objects.get(id=food_data['food_id'])
                MealComponent.objects.create(
                    meal=meal,
                    food=food_item,
                    quantity=food_data['quantity'],
                    meal_time=meal.meal_type
                )
        
        return meal
    
    def delete_meal(self, meal: Meal) -> bool:
        """
        Delete a meal from a diet plan.
        
        Args:
            meal: The meal to delete
            
        Returns:
            True if deleted successfully
        """
        # Validate trainer can manage this meal
        if meal.diet_plan.created_by != self.trainer:
            raise ValidationError("You can only delete meals in plans you created")
        
        meal.delete()
        return True
    
    def get_client_diet_plans(self, client: CustomUser) -> List[DietPlan]:
        """
        Get all diet plans for a specific client.
        
        Args:
            client: The client user
            
        Returns:
            List of diet plans for the client
        """
        if not self._can_manage_client(client):
            raise ValidationError("You can only view diet plans for your assigned clients")
        
        return DietPlan.objects.filter(
            user=client,
            created_by=self.trainer
        ).order_by('-start_date')
    
    def get_plan_nutrition_summary(self, diet_plan: DietPlan, target_date: date = None) -> Dict[str, Any]:
        """
        Get nutritional summary for a diet plan on a specific date.
        
        Args:
            diet_plan: The diet plan
            target_date: Target date (defaults to today)
            
        Returns:
            Dictionary with nutritional summary
        """
        if target_date is None:
            target_date = date.today()
        
        meals = diet_plan.meals.filter(date=target_date)
        total_calories = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        meal_count = meals.count()
        
        for meal in meals:
            nutrition = meal.calculate_nutrition()
            total_calories += nutrition['calories']
            total_protein += nutrition['protein']
            total_carbs += nutrition['carbs']
            total_fat += nutrition['fat']
        
        return {
            'date': target_date,
            'total_calories': round(total_calories, 1),
            'total_protein': round(total_protein, 1),
            'total_carbs': round(total_carbs, 1),
            'total_fat': round(total_fat, 1),
            'meal_count': meal_count,
            'target_calories': diet_plan.daily_calories,
            'calories_percentage': round((total_calories / diet_plan.daily_calories) * 100, 1) if diet_plan.daily_calories > 0 else 0
        }
    
    def _can_manage_client(self, client: CustomUser) -> bool:
        """
        Check if trainer can manage this client.
        
        Args:
            client: The client to check
            
        Returns:
            True if trainer can manage this client
        """
        # Check if client is assigned to this trainer
        return client.assigned_trainer == self.trainer

class ClientProgressService:
    """
    Service for tracking client progress and meal completion.
    """
    
    def __init__(self, client: CustomUser):
        """
        Initialize the service for a specific client.
        
        Args:
            client: The client user instance
        """
        if not client.is_client:
            raise ValidationError("User must be a client to use this service")
        self.client = client
    
    def complete_meal_component(
        self,
        component: MealComponent,
        actual_quantity: Optional[float] = None
    ) -> MealComponent:
        """
        Mark a meal component as completed.
        
        Args:
            component: The meal component to complete
            actual_quantity: Actual quantity consumed (optional)
            
        Returns:
            Updated MealComponent instance
        """
        # Validate client owns this component
        if component.meal.diet_plan.user != self.client:
            raise ValidationError("You can only complete your own meal components")
        
        component.complete(actual_quantity)
        return component
    
    def rate_meal(self, meal: Meal, is_liked: bool, notes: str = "") -> Meal:
        """
        Rate a meal (like/dislike) and add notes.
        
        Args:
            meal: The meal to rate
            is_liked: Whether the meal was liked
            notes: Optional notes about the meal
            
        Returns:
            Updated Meal instance
        """
        # Validate client owns this meal
        if meal.diet_plan.user != self.client:
            raise ValidationError("You can only rate your own meals")
        
        meal.is_liked = is_liked
        meal.notes = notes
        meal.save()
        return meal
    
    def get_daily_progress(self, target_date: date = None) -> Dict[str, Any]:
        """
        Get daily progress for the client.
        
        Args:
            target_date: Target date (defaults to today)
            
        Returns:
            Dictionary with daily progress information
        """
        if target_date is None:
            target_date = date.today()
        
        # Get active diet plan
        active_plan = self._get_active_diet_plan()
        if not active_plan:
            return {
                'date': target_date,
                'has_active_plan': False,
                'meals_completed': 0,
                'total_meals': 0,
                'completion_percentage': 0
            }
        
        # Get or create daily progress
        daily_progress, created = DailyProgress.objects.get_or_create(
            user=self.client,
            diet_plan=active_plan,
            date=target_date,
            defaults={
                'target_calories': active_plan.daily_calories,
                'target_protein': (active_plan.daily_calories * 0.30) / 4,
                'target_carbs': (active_plan.daily_calories * 0.50) / 4,
                'target_fat': (active_plan.daily_calories * 0.20) / 9
            }
        )
        
        # Update progress
        daily_progress.update_progress()
        
        return {
            'date': target_date,
            'has_active_plan': True,
            'diet_plan_id': active_plan.id,
            'meals_completed': daily_progress.meals_completed,
            'total_meals': daily_progress.total_meals,
            'completion_percentage': daily_progress.completion_percentage,
            'calories_consumed': daily_progress.calories_consumed,
            'target_calories': daily_progress.target_calories,
            'calories_percentage': daily_progress.calories_percentage,
            'is_day_completed': daily_progress.is_day_completed
        }
    
    def get_weekly_progress(self, start_date: date = None) -> List[Dict[str, Any]]:
        """
        Get weekly progress for the client.
        
        Args:
            start_date: Start date for the week (defaults to start of current week)
            
        Returns:
            List of daily progress dictionaries
        """
        if start_date is None:
            # Start of current week (Monday)
            today = date.today()
            start_date = today - timedelta(days=today.weekday())
        
        week_progress = []
        for i in range(7):
            day_date = start_date + timedelta(days=i)
            day_progress = self.get_daily_progress(day_date)
            week_progress.append(day_progress)
        
        return week_progress
    
    def get_meal_details(self, meal: Meal) -> Dict[str, Any]:
        """
        Get detailed information about a meal.
        
        Args:
            meal: The meal to get details for
            
        Returns:
            Dictionary with meal details
        """
        # Validate client owns this meal
        if meal.diet_plan.user != self.client:
            raise ValidationError("You can only view your own meals")
        
        components = []
        for component in meal.components.all():
            components.append({
                'id': component.id,
                'food_name': component.food.name,
                'food_image': component.food.image_url,
                'quantity': component.quantity,
                'is_completed': component.is_completed,
                'completed_at': component.completed_at,
                'nutrition': component.calculate_nutrition()
            })
        
        return {
            'id': meal.id,
            'meal_type': meal.meal_type,
            'date': meal.date,
            'scheduled_time': meal.scheduled_time,
            'description': meal.description,
            'is_completed': meal.is_completed,
            'completion_percentage': meal.completion_percentage,
            'is_liked': meal.is_liked,
            'notes': meal.notes,
            'nutrition': meal.calculate_nutrition(),
            'components': components
        }
    
    def get_enhanced_daily_progress(self, target_date: date = None) -> Dict[str, Any]:
        """
        Get enhanced daily progress with detailed meal and nutritional information.
        
        Args:
            target_date: Target date (defaults to today)
            
        Returns:
            Dictionary with enhanced daily progress information
        """
        if target_date is None:
            target_date = date.today()
        
        # Get active diet plan
        active_plan = self._get_active_diet_plan()
        if not active_plan:
            return {
                'date': target_date,
                'has_active_plan': False,
                'meals_completed': 0,
                'total_meals': 0,
                'completion_percentage': 0
            }
        
        # Get or create daily progress
        daily_progress, created = DailyProgress.objects.get_or_create(
            user=self.client,
            diet_plan=active_plan,
            date=target_date,
            defaults={
                'target_calories': active_plan.daily_calories,
                'target_protein': (active_plan.daily_calories * 0.30) / 4,
                'target_carbs': (active_plan.daily_calories * 0.50) / 4,
                'target_fat': (active_plan.daily_calories * 0.20) / 9
            }
        )
        
        # Update progress
        daily_progress.update_progress()
        
        # Get meals for the date with detailed information
        meals = active_plan.meals.filter(date=target_date).order_by('scheduled_time')
        meals_data = []
        
        for meal in meals:
            meal_nutrition = meal.calculate_nutrition()
            components = meal.components.all()
            
            meal_data = {
                'id': meal.id,
                'meal_type': meal.meal_type,
                'scheduled_time': meal.scheduled_time,
                'description': meal.description,
                'is_completed': meal.is_completed,
                'completion_percentage': meal.completion_percentage,
                'nutrition': meal_nutrition,
                'components': []
            }
            
            # Add component details
            for component in components:
                component_nutrition = component.calculate_nutrition()
                meal_data['components'].append({
                    'id': component.id,
                    'food_name': component.food.name,
                    'quantity': component.quantity,
                    'is_completed': component.is_completed,
                    'completed_at': component.completed_at,
                    'nutrition': component_nutrition
                })
            
            meals_data.append(meal_data)
        
        # Calculate plan nutrition
        plan_nutrition = active_plan.calculate_daily_nutrition(target_date)
        
        return {
            'date': target_date,
            'has_active_plan': True,
            'diet_plan': {
                'id': active_plan.id,
                'goal': active_plan.goal,
                'daily_calories': active_plan.daily_calories
            },
            'meals': meals_data,
            'plan_nutrition': plan_nutrition,
            'progress': {
                'meals_completed': daily_progress.meals_completed,
                'total_meals': daily_progress.total_meals,
                'completion_percentage': daily_progress.completion_percentage,
                'calories_consumed': daily_progress.calories_consumed,
                'calories_target': daily_progress.target_calories,
                'calories_percentage': daily_progress.calories_percentage,
                'protein_consumed': daily_progress.protein_consumed,
                'protein_target': daily_progress.target_protein,
                'protein_percentage': daily_progress.protein_percentage,
                'carbs_consumed': daily_progress.carbs_consumed,
                'carbs_target': daily_progress.target_carbs,
                'carbs_percentage': daily_progress.carbs_percentage,
                'fat_consumed': daily_progress.fat_consumed,
                'fat_target': daily_progress.target_fat,
                'fat_percentage': daily_progress.fat_percentage
            },
            'summary': {
                'total_meals': len(meals_data),
                'completed_meals': sum(1 for meal in meals_data if meal['is_completed']),
                'total_components': sum(len(meal['components']) for meal in meals_data),
                'completed_components': sum(
                    sum(1 for comp in meal['components'] if comp['is_completed']) 
                    for meal in meals_data
                ),
                'day_completed': daily_progress.is_day_completed
            }
        }
    
    def complete_entire_meal(self, meal: Meal) -> Dict[str, Any]:
        """
        Complete an entire meal by marking all components as completed.
        
        Args:
            meal: The meal to complete
            
        Returns:
            Dictionary with completion details
        """
        # Validate client owns this meal
        if meal.diet_plan.user != self.client:
            raise ValidationError("You can only complete your own meals")
        
        # Complete all components
        components = meal.components.all()
        completed_count = 0
        
        for component in components:
            if not component.is_completed:
                component.complete()
                completed_count += 1
        
        # Update daily progress
        daily_progress, created = DailyProgress.objects.get_or_create(
            user=self.client,
            diet_plan=meal.diet_plan,
            date=meal.date,
            defaults={
                'target_calories': meal.diet_plan.daily_calories,
                'target_protein': (meal.diet_plan.daily_calories * 0.30) / 4,
                'target_carbs': (meal.diet_plan.daily_calories * 0.50) / 4,
                'target_fat': (meal.diet_plan.daily_calories * 0.20) / 9
            }
        )
        daily_progress.update_progress()
        
        return {
            'meal_id': meal.id,
            'components_completed': completed_count,
            'total_components': components.count(),
            'completed_at': timezone.now(),
            'meal_completion_percentage': 100.0
        }
    
    def _get_active_diet_plan(self) -> Optional[DietPlan]:
        """
        Get the client's active diet plan.
        
        Returns:
            Active DietPlan instance or None
        """
        today = date.today()
        return DietPlan.objects.filter(
            user=self.client,
            is_active=True,
            start_date__lte=today,
            end_date__gte=today
        ).first() 