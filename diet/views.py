"""
views.py - API and Web Views for Diet App

This module provides API endpoints and web views for diet plan generation, daily advice,
and plan reporting. Integrates with DietGenerator and background tasks.
Enhanced to support both AI-generated and trainer-created diet plans.
"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import api_view, permission_classes
from django.shortcuts import get_object_or_404
from django.db import transaction
from .tasks import generate_ai_diet_plan
from .models import (
    DailyAdvice, FoodItem, UserFoodPreference, FoodCategory, 
    DietPlan, DietPlanTemplate, Meal, MealComponent, UserFoodCategoryPreference
)
from .api import search_food
from .trainer_services import TrainerDietPlanService, ClientProgressService
import json
import logging
from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from .ai_services import DietGenerator
from django.core.exceptions import PermissionDenied
from datetime import date, time, datetime

# Import subscription permissions
from subscription.permissions import HasDietAccess, MealUsageLimit

logger = logging.getLogger(__name__)

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def parse_date(date_str):
    """Parse date string in various formats."""
    if not date_str:
        return None
    
    formats = ['%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%Y-%m-%d']
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None

def parse_time(time_str):
    """Parse time string in various formats."""
    if not time_str:
        return None
    
    formats = ['%H:%M', '%H:%M:%S', '%I:%M %p', '%I:%M:%S %p']
    for fmt in formats:
        try:
            return datetime.strptime(time_str, fmt).time()
        except ValueError:
            continue
    return None

# ============================================================================
# FOOD SEARCH AND IMPORT ENDPOINTS
# ============================================================================

class FoodListView(APIView):
    """
    API endpoint to get all food items from the local database with pagination.
    Returns a paginated list of all available food items.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            # Get query parameters for filtering and pagination
            page = int(request.GET.get('page', 1))
            page_size = min(int(request.GET.get('page_size', 20)), 100)  # Max 100 items per page
            category = request.GET.get('category', None)
            search_query = request.GET.get('search', '').strip()
            
            # Start with all food items
            queryset = FoodItem.objects.select_related('category').all()
            
            # Apply filters
            if category:
                queryset = queryset.filter(category__name__icontains=category)
            
            if search_query:
                queryset = queryset.filter(name__icontains=search_query)
            
            # Apply ordering
            queryset = queryset.order_by('name')
            
            # Calculate pagination
            total_count = queryset.count()
            start_index = (page - 1) * page_size
            end_index = start_index + page_size
            
            # Get paginated results
            food_items = queryset[start_index:end_index]
            
            # Serialize results
            results = []
            for food in food_items:
                results.append({
                    'id': food.id,
                    'name': food.name,
                    'calories': food.calories,
                    'protein': food.protein,
                    'carbs': food.carbs,
                    'fat': food.fat,
                    'image_url': food.image_url,
                    'serving_size': food.serving_size,
                    'serving_size_grams': food.serving_size_grams,
                    'category': food.category.name if food.category else None,
                    'category_id': food.category.id if food.category else None,
                    'api_id': food.api_id,
                    'calories_per_gram': food.calories_per_gram,
                    'protein_per_gram': food.protein_per_gram,
                    'carbs_per_gram': food.carbs_per_gram,
                    'fat_per_gram': food.fat_per_gram
                })
            
            # Calculate pagination info
            total_pages = (total_count + page_size - 1) // page_size
            has_next = page < total_pages
            has_previous = page > 1
            
            return Response({
                'results': results,
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total_count': total_count,
                    'total_pages': total_pages,
                    'has_next': has_next,
                    'has_previous': has_previous,
                    'next_page': page + 1 if has_next else None,
                    'previous_page': page - 1 if has_previous else None
                },
                'filters': {
                    'category': category,
                    'search': search_query
                }
            })
            
        except Exception as e:
            logger.error(f"Food list error: {str(e)}")
            return Response(
                {"error": "Failed to retrieve food items"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class FoodCategoryListView(APIView):
    """
    API endpoint to get all food categories from the database.
    Returns a list of all available food categories for filtering.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            categories = FoodCategory.objects.all().order_by('name')
            
            results = []
            for category in categories:
                results.append({
                    'id': category.id,
                    'name': category.name,
                    'meal_times': category.meal_times,
                    'is_protein': category.is_protein,
                    'is_carb': category.is_carb,
                    'is_fat': category.is_fat,
                    'food_count': category.fooditem_set.count()
                })
            
            return Response({
                'results': results,
                'total_count': len(results)
            })
            
        except Exception as e:
            logger.error(f"Food category list error: {str(e)}")
            return Response(
                {"error": "Failed to retrieve food categories"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class FoodSearchView(APIView):
    """
    API endpoint to search for food items from both local database and Edamam API.
    Returns combined results with indication of source.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        query = request.GET.get('q', '').strip()
        if not query:
            return Response(
                {"error": "Query parameter 'q' is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

class UserFoodCategoryPreferenceView(APIView):
    """Set or list per-user meal categorization for liked foods."""
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request):
        try:
            # Ensure a UserFoodPreference exists
            preferences, _ = UserFoodPreference.objects.get_or_create(user=request.user)
            liked_ids = set(preferences.liked_foods.values_list('id', flat=True))

            # Existing mappings
            mappings = UserFoodCategoryPreference.objects.filter(user=request.user).select_related('food')
            mapping_data = [
                {
                    'food_id': m.food.id,
                    'food_name': m.food.name,
                    'meal': m.meal,
                    'macro': m.macro,
                    'updated_at': m.updated_at,
                }
                for m in mappings
            ]

            # Liked but uncategorized foods
            categorized_ids = set(mappings.values_list('food_id', flat=True))
            uncategorized = FoodItem.objects.filter(id__in=liked_ids - categorized_ids).order_by('name')
            uncategorized_data = [
                {
                    'food_id': f.id,
                    'food_name': f.name,
                }
                for f in uncategorized
            ]

            return Response({
                'mappings': mapping_data,
                'uncategorized_liked_foods': uncategorized_data,
                'choices': {
                    'meals': [c[0] for c in UserFoodCategoryPreference.MEAL_CHOICES],
                    'macros': [c[0] for c in UserFoodCategoryPreference.MACRO_CHOICES],
                }
            })
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference list error: {str(e)}")
            return Response({"error": "Failed to load category preferences"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        try:
            food_id = request.data.get('food_id')
            meal = request.data.get('meal')
            macro = request.data.get('macro')

            if not all([food_id, meal, macro]):
                return Response({"error": "food_id, meal, and macro are required"}, status=status.HTTP_400_BAD_REQUEST)

            # Validate choices
            meal_values = {c[0] for c in UserFoodCategoryPreference.MEAL_CHOICES}
            macro_values = {c[0] for c in UserFoodCategoryPreference.MACRO_CHOICES}
            if meal not in meal_values or macro not in macro_values:
                return Response({"error": "Invalid meal or macro"}, status=status.HTTP_400_BAD_REQUEST)

            food = get_object_or_404(FoodItem, id=food_id)

            # Ensure food is liked by the user before categorization
            preferences, _ = UserFoodPreference.objects.get_or_create(user=request.user)
            if not preferences.liked_foods.filter(id=food.id).exists():
                return Response({"error": "Food must be liked before categorization"}, status=status.HTTP_400_BAD_REQUEST)

            obj, created = UserFoodCategoryPreference.objects.update_or_create(
                user=request.user,
                food=food,
                defaults={"meal": meal, "macro": macro}
            )

            return Response({
                'created': created,
                'food_id': obj.food.id,
                'food_name': obj.food.name,
                'meal': obj.meal,
                'macro': obj.macro,
            }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference create error: {str(e)}")
            return Response({"error": "Failed to set category preference"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class UserFoodCategoryPreferenceDetailView(APIView):
    """Update or delete a specific food's categorization for the user."""
    permission_classes = [IsAuthenticated, HasDietAccess]

    def put(self, request, food_id):
        try:
            meal = request.data.get('meal')
            macro = request.data.get('macro')
            if not any([meal, macro]):
                return Response({"error": "Provide meal and/or macro to update"}, status=status.HTTP_400_BAD_REQUEST)

            meal_values = {c[0] for c in UserFoodCategoryPreference.MEAL_CHOICES}
            macro_values = {c[0] for c in UserFoodCategoryPreference.MACRO_CHOICES}
            if meal and meal not in meal_values:
                return Response({"error": "Invalid meal"}, status=status.HTTP_400_BAD_REQUEST)
            if macro and macro not in macro_values:
                return Response({"error": "Invalid macro"}, status=status.HTTP_400_BAD_REQUEST)

            obj = get_object_or_404(UserFoodCategoryPreference, user=request.user, food_id=food_id)
            if meal:
                obj.meal = meal
            if macro:
                obj.macro = macro
            obj.save()
            return Response({
                'food_id': obj.food.id,
                'food_name': obj.food.name,
                'meal': obj.meal,
                'macro': obj.macro,
            })
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference update error: {str(e)}")
            return Response({"error": "Failed to update category preference"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, food_id):
        try:
            obj = get_object_or_404(UserFoodCategoryPreference, user=request.user, food_id=food_id)
            obj.delete()
            return Response({"message": "Category preference deleted"})
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference delete error: {str(e)}")
            return Response({"error": "Failed to delete category preference"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        try:
            # Search local database first
            local_results = FoodItem.objects.filter(
                name__icontains=query
            )[:10]
            
            local_foods = []
            for food in local_results:
                local_foods.append({
                    'id': food.id,
                    'name': food.name,
                    'calories': food.calories,
                    'protein': food.protein,
                    'carbs': food.carbs,
                    'fat': food.fat,
                    'image_url': food.image_url,
                    'serving_size': food.serving_size,
                    'category': food.category.name if food.category else None,
                    'source': 'local',
                    'api_id': food.api_id
                })
            
            # Search Edamam API
            edamam_results = []
            try:
                edamam_response = search_food(query)
                hints = edamam_response.get('hints', [])
                
                for hint in hints[:5]:  # Limit to 5 results
                    food_data = hint.get('food', {})
                    edamam_results.append({
                        'name': food_data.get('label', ''),
                        'calories': food_data.get('nutrients', {}).get('ENERC_KCAL', 0),
                        'protein': food_data.get('nutrients', {}).get('PROCNT', 0),
                        'carbs': food_data.get('nutrients', {}).get('CHOCDF', 0),
                        'fat': food_data.get('nutrients', {}).get('FAT', 0),
                        'image_url': food_data.get('image', ''),
                        'serving_size': '100g',
                        'source': 'edamam',
                        'api_id': food_data.get('foodId', '')
                    })
            except Exception as e:
                logger.warning(f"Edamam API search failed: {str(e)}")
            
            return Response({
                'local_results': local_foods,
                'edamam_results': edamam_results,
                'total_results': len(local_foods) + len(edamam_results)
            })
            
        except Exception as e:
            logger.error(f"Food search error: {str(e)}")
            return Response(
                {"error": "Failed to search food items"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class FoodImportView(APIView):
    """
    API endpoint to import food items from Edamam API into local database.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def post(self, request):
        try:
            food_data = request.data.get('food_data')
            if not food_data:
                return Response(
                    {"error": "food_data is required"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Check if food already exists
            existing_food = FoodItem.objects.filter(api_id=food_data.get('api_id')).first()
            if existing_food:
                return Response({
                    "message": "Food item already exists",
                    "food_id": existing_food.id
                })
            
            # Create new food item
            food_item = FoodItem.objects.create(
                api_id=food_data.get('api_id', ''),
                name=food_data.get('name', ''),
                image_url=food_data.get('image_url', ''),
                calories=food_data.get('calories', 0),
                protein=food_data.get('protein', 0),
                carbs=food_data.get('carbs', 0),
                fat=food_data.get('fat', 0),
                serving_size=food_data.get('serving_size', '100g'),
                serving_size_grams=self._calculate_serving_size_grams(food_data)
            )
            
            # Auto-assign category
            self._auto_assign_category(food_item)
            
            return Response({
                "message": "Food item imported successfully",
                "food_id": food_item.id,
                "name": food_item.name
            })
            
        except Exception as e:
            logger.error(f"Food import error: {str(e)}")
            return Response(
                {"error": "Failed to import food item"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _calculate_serving_size_grams(self, food_data):
        """Calculate serving size in grams."""
        serving_size = food_data.get('serving_size', '100g')
        try:
            # Extract number from serving size
            import re
            number = re.findall(r'\d+', serving_size)
            if number:
                return int(number[0])
        except:
            pass
        return 100
    
    def _auto_assign_category(self, food_item):
        """Auto-assign food category based on nutritional content."""
        # Simple logic: highest macro determines category
        protein = food_item.protein
        carbs = food_item.carbs
        fat = food_item.fat
        
        if protein > carbs and protein > fat:
            category_name = 'Proteins'
        elif carbs > protein and carbs > fat:
            category_name = 'Carbs'
        else:
            category_name = 'Fats'
        
        category, created = FoodCategory.objects.get_or_create(
            name=category_name,
            defaults={'is_protein': category_name == 'Proteins', 
                     'is_carb': category_name == 'Carbs', 
                     'is_fat': category_name == 'Fats'}
        )
        food_item.category = category
        food_item.save()

class UserPreferencesView(APIView):
    """
    API endpoint to manage user food preferences (liked/disliked foods).
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            preferences, created = UserFoodPreference.objects.get_or_create(user=request.user)
            
            return Response({
                'liked_foods': [
                    {'id': food.id, 'name': food.name, 'image_url': food.image_url}
                    for food in preferences.liked_foods.all()
                ],
                'disliked_foods': [
                    {'id': food.id, 'name': food.name, 'image_url': food.image_url}
                    for food in preferences.disliked_foods.all()
                ],
                'allergies': preferences.allergies,
                'protein_choices': [
                    {'id': food.id, 'name': food.name, 'image_url': food.image_url}
                    for food in preferences.protein_choices.all()
                ],
                'carb_choices': [
                    {'id': food.id, 'name': food.name, 'image_url': food.image_url}
                    for food in preferences.carb_choices.all()
                ],
                'fat_choices': [
                    {'id': food.id, 'name': food.name, 'image_url': food.image_url}
                    for food in preferences.fat_choices.all()
                ]
            })
            
        except Exception as e:
            logger.error(f"User preferences error: {str(e)}")
            return Response(
                {"error": "Failed to retrieve user preferences"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def post(self, request):
        try:
            preferences, created = UserFoodPreference.objects.get_or_create(user=request.user)
            
            # Update preferences based on request data
            if 'liked_foods' in request.data:
                food_ids = request.data['liked_foods']
                foods = FoodItem.objects.filter(id__in=food_ids)
                preferences.liked_foods.set(foods)
            
            if 'disliked_foods' in request.data:
                food_ids = request.data['disliked_foods']
                foods = FoodItem.objects.filter(id__in=food_ids)
                preferences.disliked_foods.set(foods)
            
            if 'allergies' in request.data:
                preferences.allergies = request.data['allergies']
                preferences.save()
            
            return Response({
                "message": "Preferences updated successfully"
            })
            
        except Exception as e:
            logger.error(f"User preferences update error: {str(e)}")
            return Response(
                {"error": "Failed to update preferences"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def delete(self, request):
        try:
            preferences = get_object_or_404(UserFoodPreference, user=request.user)
            
            # Clear specific preferences
            if 'liked_foods' in request.data:
                preferences.liked_foods.clear()
            
            if 'disliked_foods' in request.data:
                preferences.disliked_foods.clear()
            
            return Response({
                "message": "Preferences cleared successfully"
            })
            
        except Exception as e:
            logger.error(f"User preferences clear error: {str(e)}")
            return Response(
                {"error": "Failed to clear preferences"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class GenerateDietPlanView(APIView):
    """
    API endpoint to trigger asynchronous diet plan generation for the authenticated user.
    Requires active subscription with diet access and meal usage limits.
    Only available for clients, not trainers.
    """
    permission_classes = [IsAuthenticated, HasDietAccess, MealUsageLimit]
    
    def post(self, request):
        try:
            # Check if user is a client (trainers cannot use AI generation)
            if request.user.is_trainer:
                return Response(
                    {"error": "Trainers cannot generate AI diet plans. Use trainer diet plan creation instead."},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            meal_count = request.data.get('meal_count', 3)
            snack_count = request.data.get('snack_count', 0)
            
            # Trigger async generation
            task = generate_ai_diet_plan.delay(
                user_id=request.user.id,
                meal_count=meal_count,
                snack_count=snack_count
            )
            
            return Response({
                "message": "Diet plan generation started",
                "task_id": task.id,
                "estimated_time": "2-3 minutes"
            })
            
        except Exception as e:
            logger.error(f"Diet plan generation error: {str(e)}")
            return Response(
                {"error": "Failed to start diet plan generation"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class DailyAdviceView(APIView):
    """
    API endpoint to retrieve the latest daily advice for the authenticated user.
    Requires active subscription with AI advice access.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            advice = DailyAdvice.objects.filter(user=request.user).order_by('-generated_at').first()
            
            if not advice:
                return Response({
                    "message": "No advice available",
                    "advice": None
                })
            
            return Response({
                "advice": {
                    'id': advice.id,
                    'text': advice.text,
                    'generated_at': advice.generated_at,
                    'context_data': advice.context_data
                }
            })
            
        except Exception as e:
            logger.error(f"Daily advice error: {str(e)}")
            return Response(
                {"error": "Failed to retrieve daily advice"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

@login_required
@require_http_methods(["GET"])
def generate_diet_plan(request):
    """
    Web view for diet plan generation.
    """
    try:
        return render(request, 'diet/generate_plan.html', {
            'user': request.user
        })
    except Exception as e:
        logger.error(f"Diet plan generation web view error: {str(e)}")
        return JsonResponse({
            "error": "Failed to load diet plan generation page"
        }, status=500) 

# ============================================================================
# TRAINER DIET PLAN MANAGEMENT ENDPOINTS
# ============================================================================

class TrainerTemplatesView(APIView):
    """
    API endpoint for trainers to get available diet plan templates.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        try:
            # Check if user is a trainer
            if not request.user.is_trainer:
                return Response(
                    {"error": "Only trainers can access diet plan templates"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            trainer_service = TrainerDietPlanService(request.user)
            templates = trainer_service.get_available_templates()
            
            results = []
            for template in templates:
                results.append({
                    'id': template.id,
                    'name': template.name,
                    'description': template.description,
                    'meals_per_day': template.meals_per_day,
                    'snacks_per_day': template.snacks_per_day,
                    'days_variation': template.days_variation,
                    'total_meals_per_cycle': template.total_meals_per_cycle
                })
            
            return Response({
                'results': results,
                'total_count': len(results)
            })
            
        except Exception as e:
            logger.error(f"Trainer templates error: {str(e)}")
            return Response(
                {"error": "Failed to retrieve templates"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class TrainerDietPlanView(APIView):
    """
    API endpoint for trainers to create and manage diet plans for their clients.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def post(self, request):
        """Create a new diet plan for a client."""
        try:
            # Check if user is a trainer
            if not request.user.is_trainer:
                return Response(
                    {"error": "Only trainers can create diet plans"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Validate required fields
            client_id = request.data.get('client_id')
            template_id = request.data.get('template_id')
            start_date_str = request.data.get('start_date')
            
            if not all([client_id, template_id, start_date_str]):
                return Response(
                    {"error": "client_id, template_id, and start_date are required"},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
            # Parse data
            from users.models import CustomUser
            client = get_object_or_404(CustomUser, id=client_id)
            template = get_object_or_404(DietPlanTemplate, id=template_id)
            start_date = parse_date(start_date_str)
            
            if not start_date:
                return Response(
                    {"error": "Invalid start_date format"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Create diet plan
            trainer_service = TrainerDietPlanService(request.user)
            diet_plan = trainer_service.create_diet_plan(
                client=client,
                template=template,
                start_date=start_date,
                duration_weeks=request.data.get('duration_weeks', 4),
                goal=request.data.get('goal', 'Maintain'),
                daily_calories=request.data.get('daily_calories')
            )
            
            return Response({
                "message": "Diet plan created successfully",
                "diet_plan": {
                    'id': diet_plan.id,
                    'client_name': client.username,
                    'template_name': template.name,
                    'start_date': diet_plan.start_date,
                    'end_date': diet_plan.end_date,
                    'goal': diet_plan.goal,
                    'daily_calories': diet_plan.daily_calories
                }
            })
            
        except Exception as e:
            logger.error(f"Trainer diet plan creation error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    def get(self, request):
        """Get diet plans for a specific client."""
        try:
            # Check if user is a trainer
            if not request.user.is_trainer:
                return Response(
                    {"error": "Only trainers can access client diet plans"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            client_id = request.GET.get('client_id')
            if not client_id:
                return Response(
                    {"error": "client_id parameter is required"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            from users.models import CustomUser
            client = get_object_or_404(CustomUser, id=client_id)
            
            trainer_service = TrainerDietPlanService(request.user)
            diet_plans = trainer_service.get_client_diet_plans(client)
            
            results = []
            for plan in diet_plans:
                results.append({
                    'id': plan.id,
                    'goal': plan.goal,
                    'daily_calories': plan.daily_calories,
                    'start_date': plan.start_date,
                    'end_date': plan.end_date,
                    'is_active': plan.is_active,
                    'template_name': plan.template.name if plan.template else None,
                    'meals_count': plan.meals.count()
                })
                
            # Return response outside the loop
                return Response({
                'results': results,
                'total_count': len(results)
                })
                
        except Exception as e:
            logger.error(f"Trainer diet plan retrieval error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class TrainerMealView(APIView):
    """
    API endpoint for trainers to add, update, and delete meals in diet plans.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def post(self, request):
        """Add a meal to a diet plan."""
        try:
            # Check if user is a trainer
            if not request.user.is_trainer:
                return Response(
                    {"error": "Only trainers can manage meals"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Validate required fields
            diet_plan_id = request.data.get('diet_plan_id')
            meal_type = request.data.get('meal_type')
            target_date_str = request.data.get('target_date')
            food_items = request.data.get('food_items', [])
            
            if not all([diet_plan_id, meal_type, target_date_str, food_items]):
                return Response(
                    {"error": "diet_plan_id, meal_type, target_date, and food_items are required"},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
            # Parse data
            diet_plan = get_object_or_404(DietPlan, id=diet_plan_id)
            target_date = parse_date(target_date_str)
            scheduled_time = None
            if request.data.get('scheduled_time'):
                scheduled_time = parse_time(request.data.get('scheduled_time'))
            
            if not target_date:
                return Response(
                    {"error": "Invalid target_date format"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Add meal
            trainer_service = TrainerDietPlanService(request.user)
            meal = trainer_service.add_meal_to_plan(
                diet_plan=diet_plan,
                meal_type=meal_type,
                target_date=target_date,
                food_items=food_items,
                scheduled_time=scheduled_time,
                description=request.data.get('description', '')
            )
            
            return Response({
                "message": "Meal added successfully",
                "meal": {
                    'id': meal.id,
                    'meal_type': meal.meal_type,
                    'date': meal.date,
                    'scheduled_time': meal.scheduled_time,
                    'description': meal.description,
                    'components_count': meal.components.count()
                }
            })
            
        except Exception as e:
            logger.error(f"Trainer meal creation error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    def put(self, request, meal_id):
        """Update a meal."""
        try:
            # Check if user is a trainer
            if not request.user.is_trainer:
                return Response(
                    {"error": "Only trainers can update meals"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            meal = get_object_or_404(Meal, id=meal_id)
            
            # Parse optional fields
            scheduled_time = None
            if request.data.get('scheduled_time'):
                scheduled_time = parse_time(request.data.get('scheduled_time'))
            
            # Update meal
            trainer_service = TrainerDietPlanService(request.user)
            updated_meal = trainer_service.update_meal(
                meal=meal,
                food_items=request.data.get('food_items'),
                scheduled_time=scheduled_time,
                description=request.data.get('description')
            )
            
            return Response({
                "message": "Meal updated successfully",
                "meal": {
                    'id': updated_meal.id,
                    'meal_type': updated_meal.meal_type,
                    'date': updated_meal.date,
                    'scheduled_time': updated_meal.scheduled_time,
                    'description': updated_meal.description,
                    'components_count': updated_meal.components.count()
                }
            })
            
        except Exception as e:
            logger.error(f"Trainer meal update error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    def delete(self, request, meal_id):
        """Delete a meal."""
        try:
            # Check if user is a trainer
            if not request.user.is_trainer:
                return Response(
                    {"error": "Only trainers can delete meals"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            meal = get_object_or_404(Meal, id=meal_id)
            
            # Delete meal
            trainer_service = TrainerDietPlanService(request.user)
            trainer_service.delete_meal(meal)
            
            return Response({
                "message": "Meal deleted successfully"
            })
            
        except Exception as e:
            logger.error(f"Trainer meal deletion error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

# ============================================================================
# ENHANCED NUTRITIONAL TRACKING ENDPOINTS
# ============================================================================

class DietPlanNutritionView(APIView):
    """
    Enhanced API endpoint to get detailed nutritional information for a diet plan.
    Shows total proteins, carbs, fats for the whole plan and each meal.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request, plan_id):
        """Get detailed nutritional breakdown for a diet plan."""
        try:
            diet_plan = get_object_or_404(DietPlan, id=plan_id)
            
            # Check permissions
            if request.user.is_trainer:
                if diet_plan.created_by != request.user:
                    return Response(
                        {"error": "You can only view nutrition for plans you created"},
                        status=status.HTTP_403_FORBIDDEN
                    )
            elif request.user.is_client:
                if diet_plan.user != request.user:
                    return Response(
                        {"error": "You can only view your own diet plan nutrition"},
                        status=status.HTTP_403_FORBIDDEN
                    )
            
            # Get target date from query params
            target_date_str = request.GET.get('date')
            target_date = parse_date(target_date_str) if target_date_str else date.today()
            
            # Calculate plan nutrition
            plan_nutrition = diet_plan.calculate_daily_nutrition(target_date)
            
            # Get meals for the date with detailed nutrition
            meals = diet_plan.meals.filter(date=target_date).order_by('scheduled_time')
            meals_data = []
            
            for meal in meals:
                meal_nutrition = meal.calculate_nutrition()
                meals_data.append({
                    'id': meal.id,
                    'meal_type': meal.meal_type,
                    'scheduled_time': meal.scheduled_time,
                    'description': meal.description,
                    'is_completed': meal.is_completed,
                    'completion_percentage': meal.completion_percentage,
                    'nutrition': meal_nutrition,
                    'components_count': meal.components.count(),
                    'completed_components': meal.components.filter(is_completed=True).count()
                })
            
            # Calculate totals
            total_meals = meals.count()
            completed_meals = sum(1 for meal in meals if meal.is_completed)
            
            # Calculate nutritional targets (based on standard ratios)
            target_protein = (diet_plan.daily_calories * 0.30) / 4  # 30% of calories from protein
            target_carbs = (diet_plan.daily_calories * 0.50) / 4    # 50% of calories from carbs
            target_fat = (diet_plan.daily_calories * 0.20) / 9      # 20% of calories from fat
            
            # Calculate nutritional percentages
            protein_percentage = round((plan_nutrition['protein'] / target_protein * 100), 1) if target_protein > 0 else 0
            carbs_percentage = round((plan_nutrition['carbs'] / target_carbs * 100), 1) if target_carbs > 0 else 0
            fat_percentage = round((plan_nutrition['fat'] / target_fat * 100), 1) if target_fat > 0 else 0
            
            return Response({
                'diet_plan': {
                    'id': diet_plan.id,
                    'goal': diet_plan.goal,
                    'daily_calories': diet_plan.daily_calories,
                    'start_date': diet_plan.start_date,
                    'end_date': diet_plan.end_date
                },
                'date': target_date,
                'plan_nutrition': {
                    'calories': plan_nutrition['calories'],
                    'protein': plan_nutrition['protein'],
                    'carbs': plan_nutrition['carbs'],
                    'fat': plan_nutrition['fat'],
                    'targets': {
                        'calories': diet_plan.daily_calories,
                        'protein': target_protein,
                        'carbs': target_carbs,
                        'fat': target_fat
                    },
                    'percentages': {
                        'calories': round((plan_nutrition['calories'] / diet_plan.daily_calories * 100), 1) if diet_plan.daily_calories > 0 else 0,
                        'protein': protein_percentage,
                        'carbs': carbs_percentage,
                        'fat': fat_percentage
                    }
                },
                'meals': meals_data,
                'nutritional_summary': {
                    'total_calories': plan_nutrition['calories'],
                    'total_protein': plan_nutrition['protein'],
                    'total_carbs': plan_nutrition['carbs'],
                    'total_fat': plan_nutrition['fat'],
                    'calories_target': diet_plan.daily_calories,
                    'protein_target': target_protein,
                    'carbs_target': target_carbs,
                    'fat_target': target_fat,
                    'calories_percentage': round((plan_nutrition['calories'] / diet_plan.daily_calories * 100), 1) if diet_plan.daily_calories > 0 else 0,
                    'protein_percentage': protein_percentage,
                    'carbs_percentage': carbs_percentage,
                    'fat_percentage': fat_percentage
                },
                'summary': {
                    'total_meals': total_meals,
                    'completed_meals': completed_meals,
                    'completion_percentage': round((completed_meals / total_meals * 100), 1) if total_meals > 0 else 0,
                    'calories_target': diet_plan.daily_calories,
                    'calories_consumed': plan_nutrition['calories'],
                    'calories_percentage': round((plan_nutrition['calories'] / diet_plan.daily_calories * 100), 1) if diet_plan.daily_calories > 0 else 0
                }
            })
            
        except Exception as e:
            logger.error(f"Diet plan nutrition error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        
class MealComponentsView(APIView):
    """
    Enhanced API endpoint to get detailed components of any meal in a diet plan.
    Shows all food items, quantities, and nutritional breakdown.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request, meal_id):
        """Get detailed components of a specific meal."""
        try:
            meal = get_object_or_404(Meal, id=meal_id)
            
            # Check permissions
            if request.user.is_trainer:
                if meal.diet_plan.created_by != request.user:
                    return Response(
                        {"error": "You can only view meals in plans you created"},
                        status=status.HTTP_403_FORBIDDEN
                    )
            elif request.user.is_client:
                if meal.diet_plan.user != request.user:
                    return Response(
                        {"error": "You can only view your own meals"},
                        status=status.HTTP_403_FORBIDDEN
                    )
            
            # Get meal components with detailed information
            components = meal.components.all()
            components_data = []
            
            for component in components:
                component_nutrition = component.calculate_nutrition()
                components_data.append({
                    'id': component.id,
                    'food': {
                        'id': component.food.id,
                        'name': component.food.name,
                        'calories': component.food.calories,
                        'protein': component.food.protein,
                        'carbs': component.food.carbs,
                        'fat': component.food.fat,
                        'serving_size': component.food.serving_size,
                        'image_url': component.food.image_url,
                        'category': component.food.category.name if component.food.category else None
                    },
                    'quantity': component.quantity,
                    'is_completed': component.is_completed,
                    'completed_at': component.completed_at,
                    'actual_quantity_consumed': component.actual_quantity_consumed,
                    'nutrition': component_nutrition
                })
            
            # Calculate meal totals
            meal_nutrition = meal.calculate_nutrition()
            total_components = components.count()
            completed_components = components.filter(is_completed=True).count()
            
            # Calculate meal nutritional targets (based on meal type and plan calories)
            meal_calorie_target = meal.diet_plan.daily_calories / 3  # Assuming 3 meals per day
            meal_protein_target = (meal_calorie_target * 0.30) / 4
            meal_carbs_target = (meal_calorie_target * 0.50) / 4
            meal_fat_target = (meal_calorie_target * 0.20) / 9
            
            # Calculate meal nutritional percentages
            meal_protein_percentage = round((meal_nutrition['protein'] / meal_protein_target * 100), 1) if meal_protein_target > 0 else 0
            meal_carbs_percentage = round((meal_nutrition['carbs'] / meal_carbs_target * 100), 1) if meal_carbs_target > 0 else 0
            meal_fat_percentage = round((meal_nutrition['fat'] / meal_fat_target * 100), 1) if meal_fat_target > 0 else 0
            
            return Response({
                'meal': {
                    'id': meal.id,
                    'meal_type': meal.meal_type,
                    'date': meal.date,
                    'scheduled_time': meal.scheduled_time,
                    'description': meal.description,
                    'is_completed': meal.is_completed,
                    'completion_percentage': meal.completion_percentage,
                    'diet_plan_id': meal.diet_plan.id
                },
                'components': components_data,
                'nutrition': {
                    'calories': meal_nutrition['calories'],
                    'protein': meal_nutrition['protein'],
                    'carbs': meal_nutrition['carbs'],
                    'fat': meal_nutrition['fat'],
                    'targets': {
                        'calories': meal_calorie_target,
                        'protein': meal_protein_target,
                        'carbs': meal_carbs_target,
                        'fat': meal_fat_target
                    },
                    'percentages': {
                        'calories': round((meal_nutrition['calories'] / meal_calorie_target * 100), 1) if meal_calorie_target > 0 else 0,
                        'protein': meal_protein_percentage,
                        'carbs': meal_carbs_percentage,
                        'fat': meal_fat_percentage
                    }
                },
                'meal_nutritional_summary': {
                    'total_calories': meal_nutrition['calories'],
                    'total_protein': meal_nutrition['protein'],
                    'total_carbs': meal_nutrition['carbs'],
                    'total_fat': meal_nutrition['fat'],
                    'calories_target': meal_calorie_target,
                    'protein_target': meal_protein_target,
                    'carbs_target': meal_carbs_target,
                    'fat_target': meal_fat_target,
                    'calories_percentage': round((meal_nutrition['calories'] / meal_calorie_target * 100), 1) if meal_calorie_target > 0 else 0,
                    'protein_percentage': meal_protein_percentage,
                    'carbs_percentage': meal_carbs_percentage,
                    'fat_percentage': meal_fat_percentage
                },
                'summary': {
                    'total_components': total_components,
                    'completed_components': completed_components,
                    'completion_percentage': round((completed_components / total_components * 100), 1) if total_components > 0 else 0
                }
            })
            
        except Exception as e:
            logger.error(f"Meal components error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class EnhancedClientProgressView(APIView):
    """
    Enhanced API endpoint for clients to track detailed daily progress.
    Shows meal completion status, nutritional progress, and component details.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        """Get enhanced daily progress with detailed meal and nutritional information."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                return Response(
                    {"error": "Only clients can access progress tracking"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            target_date_str = request.GET.get('date')
            target_date = parse_date(target_date_str) if target_date_str else date.today()
            
            # Get progress using enhanced service
            client_service = ClientProgressService(request.user)
            progress = client_service.get_enhanced_daily_progress(target_date)
            
            return Response(progress)
            
        except Exception as e:
            logger.error(f"Enhanced client progress error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class MealCompletionView(APIView):
    """
    API endpoint for clients to complete entire meals or individual components.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def post(self, request, meal_id):
        """Complete a meal or its components."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                return Response(
                    {"error": "Only clients can complete meals"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            meal = get_object_or_404(Meal, id=meal_id)
            
            # Validate client owns this meal
            if meal.diet_plan.user != request.user:
                return Response(
                    {"error": "You can only complete your own meals"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            action = request.data.get('action', 'complete_meal')
            
            if action == 'complete_meal':
                # Complete entire meal
                client_service = ClientProgressService(request.user)
                result = client_service.complete_entire_meal(meal)
                
                return Response({
                    "message": "Meal completed successfully",
                    "meal_id": meal.id,
                    "completion_percentage": 100.0,
                    "completed_at": result['completed_at']
                })
            
            elif action == 'complete_component':
                # Complete specific component
                component_id = request.data.get('component_id')
                actual_quantity = request.data.get('actual_quantity')
                
                if not component_id:
                    return Response(
                        {"error": "component_id is required"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                component = get_object_or_404(MealComponent, id=component_id, meal=meal)
                client_service = ClientProgressService(request.user)
                updated_component = client_service.complete_meal_component(component, actual_quantity)
                
                # Get updated meal completion status
                meal.refresh_from_db()
                
                return Response({
                    "message": "Component completed successfully",
                    "component_id": updated_component.id,
                    "meal_completion_percentage": meal.completion_percentage,
                    "meal_is_completed": meal.is_completed,
                    "completed_at": updated_component.completed_at
                })
            
            else:
                return Response(
                    {"error": "Invalid action. Use 'complete_meal' or 'complete_component'"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
        except Exception as e:
            logger.error(f"Meal completion error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

# ============================================================================
# CLIENT PROGRESS TRACKING ENDPOINTS
# ============================================================================

class ClientProgressView(APIView):
    """
    API endpoint for clients to track their daily progress.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        """Get daily progress for the authenticated client."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                return Response(
                    {"error": "Only clients can access progress tracking"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            target_date_str = request.GET.get('date')
            target_date = None
            if target_date_str:
                target_date = parse_date(target_date_str)
            
            # Get progress
            client_service = ClientProgressService(request.user)
            progress = client_service.get_daily_progress(target_date)
            
            return Response(progress)
            
        except Exception as e:
            logger.error(f"Client progress error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class ClientWeeklyProgressView(APIView):
    """
    API endpoint for clients to get weekly progress.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        """Get weekly progress for the authenticated client."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                return Response(
                    {"error": "Only clients can access progress tracking"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            start_date_str = request.GET.get('start_date')
            start_date = None
            if start_date_str:
                start_date = parse_date(start_date_str)
            
            # Get weekly progress
            client_service = ClientProgressService(request.user)
            week_progress = client_service.get_weekly_progress(start_date)
            
            return Response({
                'week_progress': week_progress,
                'total_days': len(week_progress)
            })
            
        except Exception as e:
            logger.error(f"Client weekly progress error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class ClientMealInteractionView(APIView):
    """
    API endpoint for clients to interact with meals (complete components, rate meals).
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def post(self, request):
        """Complete a meal component or rate a meal."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                return Response(
                    {"error": "Only clients can interact with meals"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            action = request.data.get('action')
            
            if action == 'complete_component':
                component_id = request.data.get('component_id')
                actual_quantity = request.data.get('actual_quantity')
                
                if not component_id:
                    return Response(
                        {"error": "component_id is required"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                component = get_object_or_404(MealComponent, id=component_id)
                client_service = ClientProgressService(request.user)
                updated_component = client_service.complete_meal_component(component, actual_quantity)
                
                return Response({
                    "message": "Component completed successfully",
                    "component": {
                        'id': updated_component.id,
                        'is_completed': updated_component.is_completed,
                        'completed_at': updated_component.completed_at
                    }
                })
            
            elif action == 'rate_meal':
                meal_id = request.data.get('meal_id')
                is_liked = request.data.get('is_liked')
                notes = request.data.get('notes', '')
                
                if not all([meal_id, is_liked is not None]):
                    return Response(
                        {"error": "meal_id and is_liked are required"},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                meal = get_object_or_404(Meal, id=meal_id)
                client_service = ClientProgressService(request.user)
                updated_meal = client_service.rate_meal(meal, is_liked, notes)
                
                return Response({
                    "message": "Meal rated successfully",
                    "meal": {
                        'id': updated_meal.id,
                        'is_liked': updated_meal.is_liked,
                        'notes': updated_meal.notes
                    }
                })
            
            else:
                return Response(
                    {"error": "Invalid action. Use 'complete_component' or 'rate_meal'"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
        except Exception as e:
            logger.error(f"Client meal interaction error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

class ClientMealDetailsView(APIView):
    """
    API endpoint for clients to get detailed meal information.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request, meal_id):
        """Get detailed information about a specific meal."""
        try:
            # Check if user is a client
            if not request.user.is_client:
                return Response(
                    {"error": "Only clients can access meal details"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            meal = get_object_or_404(Meal, id=meal_id)
            
            # Get meal details
            client_service = ClientProgressService(request.user)
            meal_details = client_service.get_meal_details(meal)
            
            return Response(meal_details)
            
        except Exception as e:
            logger.error(f"Client meal details error: {str(e)}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )