"""
views.py - API and Web Views for Diet App

This module provides API endpoints and web views for diet plan generation, daily advice,
and plan reporting. Integrates with GPTDietPlanner and background tasks.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .tasks import generate_ai_diet_plan
from .models import DailyAdvice
import json
from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from .services import GPTDietPlanner
from django.core.exceptions import PermissionDenied
from training_platform.utils import get_logger, log_error

logger = get_logger('diet')

class GenerateDietPlanView(APIView):
    """
    API endpoint to trigger asynchronous diet plan generation for the authenticated user.
    """
    def post(self, request):
        user = request.user
        meal_count = request.data.get('meal_count', 3)
        
        # Validate meal count
        if meal_count not in [3,4,5]:
            return Response(
                {"error": "Invalid meal count. Choose 3,4 or 5"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Trigger async generation
        generate_ai_diet_plan.delay(user.id, meal_count)
        logger.info(f"Diet plan generation triggered for user {user.id}.")
        return Response(
            {"status": "Generation started. Check back in 1-2 minutes."},
            status=status.HTTP_202_ACCEPTED
        )

class DailyAdviceView(APIView):
    """
    API endpoint to retrieve the latest daily advice for the authenticated user.
    """
    def get(self, request):
        advice = DailyAdvice.objects.filter(
            user=request.user
        ).order_by('-generated_at').first()
        
        if not advice:
            return Response(
                {"error": "No advice generated yet"},
                status=status.HTTP_404_NOT_FOUND
            )
            
        return Response({
            "text": advice.text,
            "generated_at": advice.generated_at
        })

@login_required
@require_http_methods(["GET"])
def generate_diet_plan(request):
    """
    Web view to generate and display a diet plan for the logged-in user.
    Returns a formatted JSON response with plan details and meals.
    """
    try:
        # Initialize the GPT diet planner
        planner = GPTDietPlanner(request.user)
        
        # Generate the plan and get the report
        report = planner.get_plan_report()
        
        if report["status"] == "error":
            return JsonResponse({
                "status": "error",
                "message": report["message"]
            }, status=400)
            
        # Get the actual plan data for display
        plan = planner.user.dietplan_set.get(id=report["plan_id"])
        meals = plan.meals.all().order_by('date', 'meal_time')
        
        # Format the response
        formatted_plan = {
            "status": "success",
            "plan_details": {
                "id": plan.id,
                "goal": plan.goal,
                "daily_calories": plan.daily_calories,
                "start_date": plan.start_date.isoformat(),
                "end_date": plan.end_date.isoformat(),
                "generation_strategy": plan.generation_strategy
            },
            "meals": []
        }
        
        # Group meals by day
        current_day = None
        day_meals = []
        
        for meal in meals:
            if current_day != meal.date:
                if current_day is not None:
                    formatted_plan["meals"].append({
                        "date": current_day.isoformat(),
                        "meals": day_meals
                    })
                current_day = meal.date
                day_meals = []
            
            # Get meal components
            components = []
            for component in meal.mealcomponent_set.all():
                components.append({
                    "food_name": component.food.name,
                    "quantity_grams": component.quantity,
                    "category": "protein" if component.food.category and component.food.category.is_protein else \
                                "carb" if component.food.category and component.food.category.is_carb else \
                                "fat" if component.food.category and component.food.category.is_fat else None,
                    "calories": component.food.calories * (component.quantity / 100),
                    "protein": component.food.protein * (component.quantity / 100),
                    "carbs": component.food.carbs * (component.quantity / 100),
                    "fat": component.food.fat * (component.quantity / 100)
                })
            
            # Calculate meal totals
            total_calories = sum(c["calories"] for c in components)
            total_protein = sum(c["protein"] for c in components)
            total_carbs = sum(c["carbs"] for c in components)
            total_fat = sum(c["fat"] for c in components)
            
            day_meals.append({
                "meal_time": meal.meal_time,
                "template": meal.template,
                "components": components,
                "totals": {
                    "calories": round(total_calories, 1),
                    "protein": round(total_protein, 1),
                    "carbs": round(total_carbs, 1),
                    "fat": round(total_fat, 1)
                }
            })
        
        # Add the last day's meals
        if current_day is not None:
            formatted_plan["meals"].append({
                "date": current_day.isoformat(),
                "meals": day_meals
            })
        
        return JsonResponse(formatted_plan)
        
    except Exception as e:
        log_error(logger, e, {"user_id": request.user.id, "view": "generate_diet_plan"})
        return JsonResponse({
            "status": "error",
            "message": str(e)
        }, status=500)