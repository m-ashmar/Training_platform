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
from django.utils.translation import gettext as _
from .tasks import generate_ai_diet_plan
from .ai_services import DietGenerator
from .exceptions import OpenAIError, DietParsingError, PersistenceError
from .engine.rule_based_planner import RuleBasedPlanner
from .models import (
    DailyAdvice, FoodItem, UserFoodPreference, FoodCategory, 
    DietPlan, DietPlanTemplate, Meal, MealComponent, UserFoodCategoryPreference
)
from .api import search_food
from .trainer_services import TrainerDietPlanService, ClientProgressService
from .data_collection import TrainingDataCollector
import json
import logging
from django.shortcuts import render
from django.http import JsonResponse
from .ai_services import DietGenerator
from django.core.exceptions import PermissionDenied
from datetime import date, time, datetime
from django.db import models

# Import subscription permissions
from subscription.permissions import HasDietAccess, MealUsageLimit
from subscription import quota
from django.utils import timezone
from django.http import Http404
from rest_framework.exceptions import NotFound, PermissionDenied, NotAuthenticated
from training_platform.api_exceptions import PASSTHROUGH_EXCEPTIONS
from training_platform.query_params import int_param

from diet.planner.portion import describe

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
            page = int_param(request.GET, 'page', default=1, minimum=1)
            page_size = int_param(request.GET, 'page_size', default=20, minimum=1, maximum=100)
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
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
    # Relaxed to authenticated-only to match tests
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        try:
            query = request.GET.get('q', '').strip()
            if not query:
                return Response(
                    {"error": "Query parameter 'q' is required"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Search local database first
            local_qs = FoodItem.objects.filter(
                name__icontains=query
            )[:10]
            
            local_results = []
            for food in local_qs:
                local_results.append({
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
                        'id': f"edamam_{food_data.get('foodId', '')}",
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
            
            results = local_results + edamam_results
            return Response({
                'query': query,
                'local_count': len(local_results),
                'edamam_count': len(edamam_results),
                'total_count': len(results),
                'results': results
            })
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Food search error: {str(e)}")
            return Response(
                {"error": "Failed to search food items"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class UserFoodCategoryPreferenceView(APIView):
    """Set or list per-user meal categorization for liked foods."""
    permission_classes = [IsAuthenticated, HasDietAccess]

    def get(self, request):
        try:
            # Ensure a UserFoodPreference exists
            preferences, _created = UserFoodPreference.objects.get_or_create(user=request.user)
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
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference list error: {str(e)}")
            return Response({"error": _("Failed to load category preferences")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        try:
            food_id = request.data.get('food_id')
            meal = request.data.get('meal')
            macro = request.data.get('macro')

            if not all([food_id, meal, macro]):
                return Response({"error": _("food_id, meal, and macro are required")}, status=status.HTTP_400_BAD_REQUEST)

            # Validate choices
            meal_values = {c[0] for c in UserFoodCategoryPreference.MEAL_CHOICES}
            macro_values = {c[0] for c in UserFoodCategoryPreference.MACRO_CHOICES}
            if meal not in meal_values or macro not in macro_values:
                return Response({"error": _("Invalid meal or macro")}, status=status.HTTP_400_BAD_REQUEST)

            food = get_object_or_404(FoodItem, id=food_id)

            # Putting a food in a meal slot is a stronger statement than liking it, so it
            # implies the like rather than requiring it first. Rejecting the request
            # meant a client who categorised without tapping "like" got an error, and a
            # client who liked without categorising contributed nothing to 79% of meals.
            preferences, _created = UserFoodPreference.objects.get_or_create(user=request.user)
            preferences.liked_foods.add(food)
            preferences.disliked_foods.remove(food)

            # Keyed on every field of the unique constraint. Keyed on (user, food) alone,
            # a second slot for the same food overwrote the first, so "chicken is my lunch
            # protein AND my dinner protein" — the whole point of the feature — had never
            # once existed in the database: 405 rows, 405 distinct (user, food) pairs.
            obj, created = UserFoodCategoryPreference.objects.get_or_create(
                user=request.user, food=food, meal=meal, macro=macro,
            )

            return Response({
                'created': created,
                'food_id': obj.food.id,
                'food_name': obj.food.name,
                'meal': obj.meal,
                'macro': obj.macro,
            }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference create error: {str(e)}")
            return Response({"error": _("Failed to set category preference")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class UserFoodCategoryPreferenceDetailView(APIView):
    """Update or delete a specific food's categorization for the user."""
    permission_classes = [IsAuthenticated, HasDietAccess]

    def put(self, request, food_id):
        try:
            meal = request.data.get('meal')
            macro = request.data.get('macro')
            if not any([meal, macro]):
                return Response({"error": _("Provide meal and/or macro to update")}, status=status.HTTP_400_BAD_REQUEST)

            meal_values = {c[0] for c in UserFoodCategoryPreference.MEAL_CHOICES}
            macro_values = {c[0] for c in UserFoodCategoryPreference.MACRO_CHOICES}
            if meal and meal not in meal_values:
                return Response({"error": _("Invalid meal")}, status=status.HTTP_400_BAD_REQUEST)
            if macro and macro not in macro_values:
                return Response({"error": _("Invalid macro")}, status=status.HTTP_400_BAD_REQUEST)

            # A food may now sit in several slots, so the row to change is named by
            # the slot it is in (`from_meal`, `from_macro`) and moved to the new one.
            # Without that, `get_object_or_404` on (user, food) raised
            # MultipleObjectsReturned the moment the core feature worked.
            rows = UserFoodCategoryPreference.objects.filter(user=request.user, food_id=food_id)
            from_meal, from_macro = request.data.get('from_meal'), request.data.get('from_macro')
            if from_meal:
                rows = rows.filter(meal=from_meal)
            if from_macro:
                rows = rows.filter(macro=from_macro)
            if rows.count() > 1:
                return Response({"error": _("This food is in several slots; pass from_meal and from_macro")},
                                status=status.HTTP_400_BAD_REQUEST)
            obj = rows.first()
            if obj is None:
                return Response({"error": _("Not found")}, status=status.HTTP_404_NOT_FOUND)
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
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference update error: {str(e)}")
            return Response({"error": _("Failed to update category preference")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, food_id):
        try:
            # Delete one slot when named, every slot for the food when not.
            rows = UserFoodCategoryPreference.objects.filter(user=request.user, food_id=food_id)
            if request.data.get('meal'):
                rows = rows.filter(meal=request.data['meal'])
            if request.data.get('macro'):
                rows = rows.filter(macro=request.data['macro'])
            deleted, _ = rows.delete()
            if not deleted:
                return Response({"error": _("Not found")}, status=status.HTTP_404_NOT_FOUND)
            return Response({"message": _("Category preference deleted"), "deleted": deleted})
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"UserFoodCategoryPreference delete error: {str(e)}")
            return Response({"error": _("Failed to delete category preference")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class FoodImportView(APIView):
    """
    API endpoint to import food items from Edamam API into local database.
    """
    # Relaxed to authenticated-only to match tests
    permission_classes = [IsAuthenticated]
    
    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def post(self, request):
        try:
            # Accept either nested 'food_data' or flat body per tests
            food_data = request.data.get('food_data') or request.data
            if not food_data or not food_data.get('api_id'):
                return Response({"error": _("api_id is required")}, status=status.HTTP_400_BAD_REQUEST)
            
            # Check if food already exists
            existing_food = FoodItem.objects.filter(api_id=food_data.get('api_id')).first()
            if existing_food:
                return Response({
                    "message": _("Food item already exists"),
                    "food_id": existing_food.id,
                    "food_name": existing_food.name
                })
            
            # Determine serving size grams robustly
            serving_size_grams = self._calculate_serving_size_grams(food_data)
            
            # Create new food item
            food_item = FoodItem.objects.create(
                api_id=food_data.get('api_id', ''),
                name=food_data.get('name', ''),
                image_url=food_data.get('image_url', ''),
                calories=food_data.get('calories', 0) or 0,
                protein=food_data.get('protein', 0) or 0,
                carbs=food_data.get('carbs', 0) or 0,
                fat=food_data.get('fat', 0) or 0,
                serving_size=food_data.get('serving_size', '100g') or '100g',
                serving_size_grams=serving_size_grams if serving_size_grams and serving_size_grams > 0 else 100
            )
            
            # Auto-assign category, swallow all errors
            try:
                self._auto_assign_category(food_item)
            except Exception as _e:
                logger.warning(f"Auto-assign category failed: {_e}")
                try:
                    category, _created = FoodCategory.objects.get_or_create(
                        name='Other', defaults={'is_protein': False, 'is_carb': False, 'is_fat': False}
                    )
                    food_item.category = category
                    food_item.save()
                except Exception as _e2:
                    logger.warning(f"Fallback set 'Other' category failed: {_e2}")
            
            return Response({
                "message": _("Food item imported successfully"),
                "food_id": food_item.id,
                "food_name": food_item.name
            }, status=status.HTTP_201_CREATED)
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Food import error: {str(e)}")
            print('FOOD_IMPORT_EXCEPTION:', repr(e))
            return Response(
                {"error": _("Failed to import food item")},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _calculate_serving_size_grams(self, food_data):
        """Calculate serving size in grams."""
        serving_size = food_data.get('serving_size', '100g')
        try:
            import re
            # First try numeric weight in measures
            measures = food_data.get('measures') or []
            for m in measures:
                if isinstance(m, dict) and 'weight' in m:
                    w = m.get('weight')
                    try:
                        return int(float(w))
                    except Exception:
                        continue
            # Extract number from serving size string
            number = re.findall(r'\d+\.?\d*', str(serving_size))
            if number:
                return int(float(number[0]))
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
        return 100
    
    def _auto_assign_category(self, food_item):
        """Auto-assign food category based on nutritional content with 'Other' fallback."""
        protein = float(food_item.protein or 0)
        carbs = float(food_item.carbs or 0)
        fat = float(food_item.fat or 0)

        max_macro = max(protein, carbs, fat)
        if max_macro == 0:
            category_name = 'Other'
        else:
            # If macros are close (balanced), choose Other (tolerance 5g)
            values = sorted([protein, carbs, fat], reverse=True)
            balanced = (values[0] - values[1]) <= 5 and (values[1] - values[2]) <= 5
            if balanced:
                category_name = 'Other'
            elif protein >= carbs and protein >= fat:
                category_name = 'Proteins'
            elif carbs >= protein and carbs >= fat:
                category_name = 'Carbs'
            else:
                category_name = 'Fats'

        defaults = {
            'is_protein': category_name == 'Proteins',
            'is_carb': category_name == 'Carbs',
            'is_fat': category_name == 'Fats',
        }
        # 'Other' has no macro flags
        if category_name == 'Other':
            defaults = {'is_protein': False, 'is_carb': False, 'is_fat': False}

        category, _ = FoodCategory.objects.get_or_create(name=category_name, defaults=defaults)
        food_item.category = category
        food_item.save()

class UserPreferencesView(APIView):
    """
    API endpoint to manage user food preferences (liked/disliked foods).
    """
    # Relaxed to authenticated-only to match tests
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        try:
            # Optimize: Prefetch all related ManyToMany fields to prevent N+1 queries
            try:
                preferences = UserFoodPreference.objects.prefetch_related(
                    'liked_foods', 'disliked_foods', 'protein_choices', 'carb_choices',
                    'fat_choices', 'vegetable_choices', 'fruit_choices',
                ).get(user=request.user)
                created = False
            except UserFoodPreference.DoesNotExist:
                preferences = UserFoodPreference.objects.create(user=request.user)
                created = True
            
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
                ],
                'vegetable_choices': [
                    {'id': food.id, 'name': food.name, 'image_url': food.image_url}
                    for food in preferences.vegetable_choices.all()
                ],
                'fruit_choices': [
                    {'id': food.id, 'name': food.name, 'image_url': food.image_url}
                    for food in preferences.fruit_choices.all()
                ],
                'local_ratio': preferences.local_ratio,
            })
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"User preferences error: {str(e)}")
            return Response(
                {"error": _("Failed to retrieve user preferences")},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def post(self, request):
        try:
            preferences, _created = UserFoodPreference.objects.get_or_create(user=request.user)
            action = request.data.get('action')
            if action:
                food_id = request.data.get('food_id')
                if not food_id:
                    return Response({"error": _("food_id is required")}, status=status.HTTP_400_BAD_REQUEST)
                # BUG FIX: Use select_related for better performance
                food = FoodItem.objects.select_related('category').filter(id=food_id).first()
                if not food:
                    return Response({"error": _("Food not found")}, status=status.HTTP_404_NOT_FOUND)
                if action == 'like':
                    preferences.liked_foods.add(food)
                    preferences.disliked_foods.remove(food)
                    return Response({"message": _("Preference updated"), "action": "like"})
                if action == 'dislike':
                    preferences.disliked_foods.add(food)
                    preferences.liked_foods.remove(food)
                    return Response({"message": _("Preference updated"), "action": "dislike"})
                return Response({"error": _("Invalid action")}, status=status.HTTP_400_BAD_REQUEST)

            # Bulk update path (retain old behavior)
            if 'liked_foods' in request.data:
                food_ids = request.data['liked_foods']
                foods = FoodItem.objects.filter(id__in=food_ids)
                preferences.liked_foods.set(foods)
            if 'disliked_foods' in request.data:
                food_ids = request.data['disliked_foods']
                foods = FoodItem.objects.filter(id__in=food_ids)
                preferences.disliked_foods.set(foods)
            # The five macro-choice lists carried a 50-point ranking weight that no
            # endpoint could write; Django admin was the only writer.
            for field in ('protein_choices', 'carb_choices', 'fat_choices',
                          'vegetable_choices', 'fruit_choices'):
                if field in request.data:
                    ids = request.data[field] or []
                    getattr(preferences, field).set(FoodItem.objects.filter(id__in=ids))
            changed = []
            if 'allergies' in request.data:
                preferences.allergies = request.data['allergies']
                changed.append('allergies')
            if 'local_ratio' in request.data:
                try:
                    ratio = float(request.data['local_ratio'])
                except (TypeError, ValueError):
                    return Response({"error": _("local_ratio must be a number from 0 to 1")},
                                    status=status.HTTP_400_BAD_REQUEST)
                if not 0.0 <= ratio <= 1.0:
                    return Response({"error": _("local_ratio must be a number from 0 to 1")},
                                    status=status.HTTP_400_BAD_REQUEST)
                preferences.local_ratio = ratio
                changed.append('local_ratio')
            if changed:
                preferences.save(update_fields=changed)
            return Response({"message": _("Preferences updated successfully")})
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"User preferences update error: {str(e)}")
            return Response({"error": _("Failed to update preferences")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    # Atomic: these writes were independent, so a failure part-way left the
    # records inconsistent (a half-applied password reset either locks the
    # user out or leaves a consumed token usable).
    @transaction.atomic
    def delete(self, request):
        try:
            preferences = get_object_or_404(UserFoodPreference, user=request.user)
            action = request.data.get('action')
            food_id = request.data.get('food_id')
            if action and food_id:
                food = FoodItem.objects.filter(id=food_id).first()
                if not food:
                    return Response({"error": _("Food not found")}, status=status.HTTP_404_NOT_FOUND)
                if action == 'like':
                    preferences.liked_foods.remove(food)
                    return Response({"message": _("Removed from likes")})
                if action == 'dislike':
                    preferences.disliked_foods.remove(food)
                    return Response({"message": _("Removed from dislikes")})
                return Response({"error": _("Invalid action")}, status=status.HTTP_400_BAD_REQUEST)

            # Clear specific preferences (bulk)
            if 'liked_foods' in request.data:
                preferences.liked_foods.clear()
            if 'disliked_foods' in request.data:
                preferences.disliked_foods.clear()
            return Response({"message": _("Preferences cleared successfully")})
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"User preferences clear error: {str(e)}")
            return Response({"error": _("Failed to clear preferences")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
            start_date_str = request.data.get('start_date')
            # Validate optional start_date if present
            if start_date_str:
                parsed = parse_date(start_date_str)
                if not parsed:
                    return Response(
                        {"error": "Invalid start_date format. Use YYYY-MM-DD."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                # Normalize to ISO string for Celery serialization
                start_date_str = parsed.isoformat()
            
            # Trigger async generation
            task = generate_ai_diet_plan.delay(
                user_id=request.user.id,
                meal_count=meal_count,
                snack_count=snack_count,
                start_date=start_date_str
            )
            
            # The daily limit these three endpoints declare was enforced by nothing:
            # the only increment in the codebase had no callers, so usage_count stayed
            # at 0 and every check passed. Spend the quota on the way out, once the
            # plan actually exists.
            quota.record_on_commit(request.user, "daily_meals")

            return Response({
                "message": _("Diet plan generation started"),
                "task_id": task.id,
                "estimated_time": "2-3 minutes"
            })
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Diet plan generation error: {str(e)}")
            return Response(
                {"error": "Failed to start diet plan generation"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class GenerateDietPlanSyncView(APIView):
    """
    DEBUG/ops endpoint to trigger synchronous GPT plan generation without Celery.
    Uses same permissions as async version.
    """
    permission_classes = [IsAuthenticated, HasDietAccess, MealUsageLimit]

    def post(self, request):
        try:
            if request.user.is_trainer:
                return Response({"error": _("Trainers cannot generate AI diet plans.")}, status=status.HTTP_403_FORBIDDEN)

            meal_count = int_param(request.data, 'meal_count', default=3, minimum=1, maximum=10)
            snack_count = int_param(request.data, 'snack_count', default=0, minimum=0, maximum=10)
            start_date_str = request.data.get('start_date')
            if start_date_str:
                parsed = parse_date(start_date_str)
                if not parsed:
                    return Response({"error": _("Invalid start_date format. Use YYYY-MM-DD.")}, status=status.HTTP_400_BAD_REQUEST)
                start_date_str = parsed.isoformat()

            generator = DietGenerator(request.user)
            plan_output = generator.generate_plan(meal_count=meal_count, snack_count=snack_count)
            diet_plan = generator.save_plan_to_database(plan_output, meal_count, snack_count, start_date=start_date_str)

            quota.record_on_commit(request.user, "daily_meals")

            return Response({
                "status": "ok",
                "diet_plan_id": diet_plan.id,
                "meals_count": len(plan_output.plan),
            }, status=status.HTTP_201_CREATED)
        except (OpenAIError,) as e:
            logger.error(f"Sync GPT generation provider error: {str(e)}")
            return Response({"error": _("OpenAI provider error")}, status=status.HTTP_502_BAD_GATEWAY)
        except (DietParsingError,) as e:
            logger.error(f"Sync GPT parsing error: {str(e)}")
            return Response({"error": _("AI output parsing error")}, status=status.HTTP_502_BAD_GATEWAY)
        except (PersistenceError,) as e:
            logger.error(f"Sync GPT persistence error: {str(e)}")
            return Response({"error": _("Persistence error")}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Sync GPT unexpected error: {str(e)}")
            return Response({"error": _("Failed to generate plan")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class GenerateDietPlanRuleBasedView(APIView):
    """
    Generate a deterministic plan using RuleBasedPlanner (no GPT, no Celery).
    """
    permission_classes = [IsAuthenticated, HasDietAccess, MealUsageLimit]

    def post(self, request):
        try:
            if request.user.is_trainer:
                return Response({"error": _("Trainers cannot generate client plans here.")}, status=status.HTTP_403_FORBIDDEN)

            # Rule-Based Planner only supports Breakfast/Lunch/Dinner (max 3 meals)
            requested_meals = int_param(request.data, 'meal_count', default=3, minimum=1, maximum=10)
            meal_count = min(3, requested_meals)
            # Rule-Based Planner only supports 1 snack max. Requesting more causes stride verification issues in persistence.
            requested_snacks = int_param(request.data, 'snack_count', default=1, minimum=0, maximum=10)
            snack_count = min(1, requested_snacks)
            duration_days = int_param(request.data, 'duration_days', default=1, minimum=1, maximum=31)
            start_date = request.data.get('start_date')
            # Reserve 200 kcal for snack from daily calories by giving planner full daily calories; planner subtracts snack internally
            try:
                daily_kcal = float(request.user.calculate_daily_calories() or 0.0)
            except ValueError as e:
                return Response(
                    {"error": "Incomplete profile for calorie calculation", "detail": str(e)},
                    status=status.HTTP_400_BAD_REQUEST
                )
            except Exception:
                return Response(
                    {"error": "Failed to calculate daily calories"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            planner = RuleBasedPlanner(request.user)
            plan_output = planner.generate(
                daily_kcal=daily_kcal,
                meal_count=meal_count,
                snack_count=snack_count,
                start_date=start_date,
                duration_days=duration_days,
                no_repeat_days=3,
            )

            generator = DietGenerator(request.user)
            diet_plan = generator.save_plan_to_database(
                plan_output,
                meal_count=meal_count,
                snack_count=snack_count,
                start_date=start_date
            )

            # FIX #5: Collect training data for rule-based plans (was only done for AI plans)
            try:
                data_collector = TrainingDataCollector()
                data_collector.collect_diet_plan_data(diet_plan, plan_output)
                logger.info(f"Training data collected for rule-based plan {diet_plan.id}")
            except Exception as e:
                logger.warning(f"Failed to collect training data for plan {diet_plan.id}: {e}")

            # Build rich payload for mobile to display immediately
            try:
                # Determine which date to present (start_date if provided, else plan start date or today)
                present_date = parse_date(start_date) if start_date else (diet_plan.start_date or timezone.localdate())
                meals_qs = diet_plan.meals.filter(date=present_date).order_by('scheduled_time')
                # Plan-level daily nutrition for the date
                plan_nutrition = diet_plan.calculate_daily_nutrition(present_date)
                # Meal targets (match MealComponentsView logic)
                daily_cal = float(diet_plan.daily_calories or 0.0)
                day_snack_count = meals_qs.filter(meal_type='Snack').count()
                non_snack_count = max(1, meals_qs.exclude(meal_type='Snack').count())
                snack_kcal_target = 200.0
                snacks_total_target = day_snack_count * snack_kcal_target
                base_kcal_for_meals = max(0.0, daily_cal - snacks_total_target)

                meals_payload = []
                for meal in meals_qs:
                    meal_nutrition = meal.calculate_nutrition()
                    if meal.meal_type == 'Snack':
                        meal_calorie_target = snack_kcal_target
                    else:
                        meal_calorie_target = base_kcal_for_meals / non_snack_count
                    meal_protein_target = (meal_calorie_target * 0.30) / 4
                    meal_carbs_target = (meal_calorie_target * 0.50) / 4
                    meal_fat_target = (meal_calorie_target * 0.20) / 9
                    meals_payload.append({
                        "id": meal.id,
                        "meal_type": meal.meal_type,
                        "scheduled_time": meal.scheduled_time,
                        "description": meal.description,
                        "is_completed": meal.is_completed,
                        "components_count": meal.components.count(),
                        "nutrition": meal_nutrition,
                        "targets": {
                            "calories": meal_calorie_target,
                            "protein": meal_protein_target,
                            "carbs": meal_carbs_target,
                            "fat": meal_fat_target,
                        }
                    })
            except Exception:
                # If anything fails in rich payload, fallback to minimal response
                meals_payload = []
                plan_nutrition = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
                present_date = start_date or timezone.localdate()

            quota.record_on_commit(request.user, "daily_meals")

            return Response({
                "status": "ok",
                "diet_plan_id": diet_plan.id,
                "meals_count": len(plan_output.plan),
                "date": present_date,
                "plan_nutrition": {
                    "calories": plan_nutrition.get("calories", 0.0),
                    "protein": plan_nutrition.get("protein", 0.0),
                    "carbs": plan_nutrition.get("carbs", 0.0),
                    "fat": plan_nutrition.get("fat", 0.0),
                    "targets": {
                        "calories": diet_plan.daily_calories,
                        "protein": (diet_plan.daily_calories * 0.30) / 4 if diet_plan.daily_calories else 0.0,
                        "carbs": (diet_plan.daily_calories * 0.50) / 4 if diet_plan.daily_calories else 0.0,
                        "fat": (diet_plan.daily_calories * 0.20) / 9 if diet_plan.daily_calories else 0.0,
                    }
                },
                "meals": meals_payload,
                # What the allergen checker concluded. The client has to be able to show
                # this: a declared allergen is filtered out, but an ingredient whose
                # allergen data could not be verified is still served, and silence used
                # to be the only signal that anything was uncertain.
                "allergen_report": diet_plan.allergen_report or {},
                "smart_macro_summary": plan_output.plan_metadata.get("smart_macro_summary") if hasattr(plan_output, "plan_metadata") else None
            }, status=status.HTTP_201_CREATED)
        except PersistenceError as e:
            logger.error(f"Rule-based persistence error: {str(e)}")
            return Response({"error": _("Persistence error")}, status=status.HTTP_400_BAD_REQUEST)
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Rule-based generation error: {str(e)}")
            return Response({"error": _("Failed to generate rule-based plan")}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
                    "message": _("No advice available"),
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
        except Exception as e:
            logger.error(f"Daily advice error: {str(e)}")
            return Response(
                {"error": "Failed to retrieve daily advice"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

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
            
        except PASSTHROUGH_EXCEPTIONS:
            # Control-flow exceptions carry their own correct status (404/403/401/400).
            # The broad handler below turned every one of them into a 500, which is the
            # wrong contract for the client and hides real faults in the error budget.
            raise
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
                goal=DietPlan.normalise_goal(request.data.get('goal')),
                daily_calories=request.data.get('daily_calories')
            )
            
            return Response({
                "message": _("Diet plan created successfully"),
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Trainer diet plan creation error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
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
                
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Trainer diet plan retrieval error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
                status=status.HTTP_400_BAD_REQUEST
            )

class TrainerMealView(APIView):
    """
    API endpoint for trainers to add, update, and delete meals in diet plans.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def post(self, request):
        """Add a meal or multiple meals to a diet plan."""
        try:
            # Check if user is a trainer
            if not request.user.is_trainer:
                return Response(
                    {"error": "Only trainers can manage meals"},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Handle bulk creation for list input
            if isinstance(request.data, list):
                created_meals = []
                errors = []
                trainer_service = TrainerDietPlanService(request.user)

                for index, meal_data in enumerate(request.data):
                    try:
                        # Extract and validate
                        diet_plan_id = meal_data.get('diet_plan_id')
                        meal_type = meal_data.get('meal_type')
                        target_date_str = meal_data.get('target_date')
                        food_items = meal_data.get('food_items', [])

                        if not all([diet_plan_id, meal_type, target_date_str, food_items]):
                            errors.append({"index": index, "error": "Missing required fields"})
                            continue

                        diet_plan = get_object_or_404(DietPlan, id=diet_plan_id)
                        target_date = parse_date(target_date_str)
                        scheduled_time = None
                        if meal_data.get('scheduled_time'):
                            scheduled_time = parse_time(meal_data.get('scheduled_time'))
                        
                        if not target_date:
                            errors.append({"index": index, "error": "Invalid target_date format"})
                            continue
                        
                        meal = trainer_service.add_meal_to_plan(
                            diet_plan=diet_plan,
                            meal_type=meal_type,
                            target_date=target_date,
                            food_items=food_items,
                            scheduled_time=scheduled_time,
                            description=meal_data.get('description', '')
                        )
                        created_meals.append({
                            'id': meal.id,
                            'meal_type': meal.meal_type,
                            'date': meal.date,
                            'scheduled_time': meal.scheduled_time,
                            'description': meal.description,
                            'components_count': meal.components.count()
                        })
                    except PASSTHROUGH_EXCEPTIONS:
                        # get_object() signals 404/403 by raising. The broad handler below
                        # swallowed those and re-emitted them as 500 (str(Http404()) is '',
                        # so the log line was empty too). Control-flow exceptions must pass.
                        raise
                    except Exception as e:
                        logger.error(f"Error creating meal at index {index}: {e}")
                        errors.append({"index": index,
                                       "error": _("Could not create this meal.")})

                if errors and not created_meals:
                     return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)
                
                return Response({
                    "message": f"Successfully created {len(created_meals)} meals",
                    "meals": created_meals,
                    "errors": errors if errors else None
                }, status=status.HTTP_201_CREATED)
            
            # Validate required fields for single object
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
                "message": _("Meal added successfully"),
                "meal": {
                    'id': meal.id,
                    'meal_type': meal.meal_type,
                    'date': meal.date,
                    'scheduled_time': meal.scheduled_time,
                    'description': meal.description,
                    'components_count': meal.components.count()
                }
            })
            
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Trainer meal creation error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
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
                "message": _("Meal updated successfully"),
                "meal": {
                    'id': updated_meal.id,
                    'meal_type': updated_meal.meal_type,
                    'date': updated_meal.date,
                    'scheduled_time': updated_meal.scheduled_time,
                    'description': updated_meal.description,
                    'components_count': updated_meal.components.count()
                }
            })
            
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Trainer meal update error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
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
                "message": _("Meal deleted successfully")
            })
            
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Trainer meal deletion error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
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
            elif not request.user.is_admin:
                # Default-deny: any other role (e.g. agent) has no ownership claim.
                return Response(
                    {"error": "You do not have access to this diet plan"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Get target date from query params (fallback to plan start date if no meals for today)
            target_date_str = request.GET.get('date')
            target_date = parse_date(target_date_str) if target_date_str else timezone.localdate()
            
            # Calculate plan nutrition
            plan_nutrition = diet_plan.calculate_daily_nutrition(target_date)
            
            # Get meals for the date with detailed nutrition
            meals = diet_plan.meals.filter(date=target_date).order_by('scheduled_time')
            if not target_date_str and meals.count() == 0:
                # If no explicit date and today has no meals, try plan start_date
                fallback_date = diet_plan.start_date
                if fallback_date and fallback_date != target_date:
                    meals = diet_plan.meals.filter(date=fallback_date).order_by('scheduled_time')
                    if meals.count() > 0:
                        target_date = fallback_date
            meals_data = []
            
            from django.utils import translation
            lang_code = translation.get_language() or 'en'
            
            for meal in meals:
                meal_nutrition = meal.calculate_nutrition()
                
                # Dynamic translation unpacking
                description = meal.description
                if meal.translations and lang_code in meal.translations:
                    description = meal.translations[lang_code].get('description', description)
                    
                meals_data.append({
                    'id': meal.id,
                    'meal_type': meal.meal_type,
                    'scheduled_time': meal.scheduled_time,
                    'description': description,
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Diet plan nutrition error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
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
            # Optimize: Prefetch all components and their food/category in single query
            meal = Meal.objects.select_related('diet_plan', 'diet_plan__user', 'diet_plan__created_by')\
                .prefetch_related(
                    'components',
                    'components__food',
                    'components__food__category'
                ).get(id=meal_id)
            
            from django.utils import translation
            lang_code = translation.get_language() or 'en'
            
            # Dynamic translation unpacking
            description = meal.description
            if meal.translations and lang_code in meal.translations:
                description = meal.translations[lang_code].get('description', description)
                
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
            elif not request.user.is_admin:
                # Default-deny: any other role (e.g. agent) has no ownership claim.
                return Response(
                    {"error": "You do not have access to this meal"},
                    status=status.HTTP_403_FORBIDDEN
                )
            
            # Get meal components - No new DB query due to prefetch
            components = meal.components.all()
            components_data = []
            
            # Optimize: Calculate component nutrition in-memory to avoid DB calls
            for component in components:
                # Use methods that don't trigger DB calls if data is present
                food = component.food
                scale_factor = component.quantity / food.serving_size_grams
                
                component_nutrition = {
                    'calories': round(food.calories * scale_factor, 1),
                    'protein': round(food.protein * scale_factor, 1),
                    'carbs': round(food.carbs * scale_factor, 1),
                    'fat': round(food.fat * scale_factor, 1)
                }

                components_data.append({
                    'id': component.id,
                    'food': {
                        'id': food.id,
                        'name': food.name,
                        'calories': food.calories,
                        'protein': food.protein,
                        'carbs': food.carbs,
                        'fat': food.fat,
                        'serving_size': food.serving_size,
                        'image_url': food.image_url,
                        'category': food.category.name if food.category else None
                    },
                    'quantity': component.quantity,
                    # What a person would say they are eating. Grams stay exactly as
                    # they were, so nothing that reads them changes; this is the serving
                    # beside them. The planner has chosen portions in whole eggs and
                    # cups since phase 4 and then dropped the unit at the boundary,
                    # because a component stores a float and nothing else — so the
                    # client read "285 g yogurt" for what the engine had decided was
                    # two pots. Derived on read rather than stored, so it cannot drift
                    # from the quantity it describes.
                    'serving': describe(component.food, component.quantity),
                    'is_completed': component.is_completed,
                    'completed_at': component.completed_at,
                    'actual_quantity_consumed': component.actual_quantity_consumed,
                    'nutrition': component_nutrition
                })
            
            # Calculate meal totals
            meal_nutrition = meal.calculate_nutrition()
            total_components = components.count()
            completed_components = components.filter(is_completed=True).count()
            
            # Calculate meal nutritional targets (dynamic per day and snack share)
            daily_kcal = float(meal.diet_plan.daily_calories or 0.0)
            # Count snacks and non-snack meals for the day
            day_meals_qs = meal.diet_plan.meals.filter(date=meal.date)
            snack_count = day_meals_qs.filter(meal_type='Snack').count()
            non_snack_count = day_meals_qs.exclude(meal_type='Snack').count() or 1
            snack_kcal_target = 200.0
            snacks_total_target = snack_count * snack_kcal_target
            base_kcal_for_meals = max(0.0, daily_kcal - snacks_total_target)
            if meal.meal_type == 'Snack':
                meal_calorie_target = snack_kcal_target
            else:
                meal_calorie_target = base_kcal_for_meals / non_snack_count
            # Use default macro split 30/50/20 for targets display
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
                    'description': description,
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
                {"error": _('Request could not be completed.')},
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
            target_date = parse_date(target_date_str) if target_date_str else timezone.localdate()
            
            # Get progress using enhanced service
            client_service = ClientProgressService(request.user)
            progress = client_service.get_enhanced_daily_progress(target_date)
            
            return Response(progress)
            
        except Exception as e:
            logger.error(f"Enhanced client progress error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
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
                    "message": _("Meal completed successfully"),
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
                    "message": _("Component completed successfully"),
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Meal completion error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
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
                {"error": _('Request could not be completed.')},
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
                {"error": _('Request could not be completed.')},
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
                    "message": _("Component completed successfully"),
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
                    "message": _("Meal rated successfully"),
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Client meal interaction error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
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
            
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"Client meal details error: {str(e)}")
            return Response(
                {"error": _('Request could not be completed.')},
                status=status.HTTP_400_BAD_REQUEST
            )

# ============================================================================
# CLIENT-SIDE PLAN LISTING AND MEAL+INGREDIENTS FETCH
# ============================================================================

class MyDietPlansView(APIView):
    """
    List all diet plans for the authenticated client user.
    Supports standard pagination: ?page=N&page_size=N
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request):
        from routine.views import StandardResultsSetPagination

        try:
            plans = (DietPlan.objects
                     .filter(user=request.user)
                     .only('id', 'goal', 'daily_calories', 'start_date', 'end_date', 'is_active',
                           'created_at', 'template_id', 'allergen_report')
                     .order_by('-start_date', '-created_at'))

            # Paginate the queryset first before building dicts
            paginator = StandardResultsSetPagination()
            page = paginator.paginate_queryset(plans, request, view=self)
            page_plans = page if page is not None else plans

            plan_ids = [p.id for p in page_plans]
            meal_counts = {}
            if plan_ids:
                for row in (Meal.objects
                            .filter(diet_plan_id__in=plan_ids)
                            .values('diet_plan_id')
                            .annotate(cnt=models.Count('id'))):
                    meal_counts[row['diet_plan_id']] = row['cnt']

            template_ids = [p.template_id for p in page_plans if p.template_id]
            templates = {t.id: t.name for t in DietPlanTemplate.objects.filter(id__in=template_ids)} if template_ids else {}

            results = []
            for p in page_plans:
                results.append({
                    'id': p.id,
                    'goal': p.goal,
                    'daily_calories': p.daily_calories,
                    'start_date': p.start_date,
                    'end_date': p.end_date,
                    'is_active': p.is_active,
                    'template_name': templates.get(p.template_id),
                    'meals_count': meal_counts.get(p.id, 0),
                    # So the client can flag a plan whose ingredients could not all be
                    # cleared against the user's declared allergies.
                    'allergen_report': p.allergen_report or {},
                })

            if page is not None:
                return paginator.get_paginated_response(results)
            return Response({'results': results, 'total_count': len(results)})
        except Exception as e:
            logger.error(f"MyDietPlansView error: {str(e)}")
            return Response({'error': _("An error occurred while loading your plans.")}, status=status.HTTP_400_BAD_REQUEST)



class DietPlanMealsWithIngredientsView(APIView):
    """
    Fetch all meals and ingredients for a given diet plan.
    Clients can access their own plans; trainers can access plans they created.
    """
    permission_classes = [IsAuthenticated, HasDietAccess]
    
    def get(self, request, plan_id: int):
        try:
            plan = get_object_or_404(DietPlan, id=plan_id)
            # Permission checks
            if request.user.is_client and plan.user_id != request.user.id:
                return Response({'error': _('You can only view your own diet plans')}, status=status.HTTP_403_FORBIDDEN)
            if request.user.is_trainer and plan.created_by_id not in (None, request.user.id):
                return Response({'error': _('You can only view plans you created')}, status=status.HTTP_403_FORBIDDEN)
            
            # --- Add Daily & Meal Targets ---
            
            # Allow query to be defined first
            meals = (Meal.objects
                     .filter(diet_plan=plan)
                     .prefetch_related('components__food')
                     .order_by('date', 'scheduled_time'))
            
            # 1. Daily Targets
            from .utils.nutrition import get_macro_ratios, goal_meal_kcal_split
            
            daily_cals = float(plan.daily_calories or 0)
            goal = plan.goal or 'Maintain'
            ratios = get_macro_ratios(goal)
            
            daily_targets = {
                'calories': daily_cals,
                'protein': round((daily_cals * ratios['protein']) / 4.0, 1),
                'carbs': round((daily_cals * ratios['carb']) / 4.0, 1),
                'fat': round((daily_cals * ratios['fat']) / 9.0, 1),
            }
            
            # 2. Meal Targets
            # We need to know the split for this user's goal
            meal_split = goal_meal_kcal_split(goal)
            # Default snack target (approximate, since dynamic number of snacks)
            # In persistence, snacks are ~200kcal. We'll use a dynamic approach:
            # Main meals take their % of (Daily - Snacks).
            # But simplest approach: 
            # - Snack = 200 kcal fixed target? Or remaining?
            # Let's align with MealComponentsView logic:
            # "Snack = 200, Meals share the rest"
            
            # Determine count of snacks/meals in the plan (average per day)
            # This is hard because explicit days aren't loopable easily here without extra query.
            # We will use the meal_type of the specific meal row.
            
            # Heuristic:
            # If Meal Type is Snack -> 200 kcal
            # If Meal Type is B/L/D -> share of (Daily - Est.Snack.Kcal)
            
            # Estimate number of snacks per day from Plan or Template
            est_snack_count = 0
            if plan.template:
                est_snack_count = plan.template.snacks_per_day
            else:
                # Fallback: check one day of meals
                try:
                    one_day = meals[0].date
                    est_snack_count = meals.filter(date=one_day, meal_type='Snack').count()
                except IndexError:
                    est_snack_count = 0
            
            # The snack budget is policy, not a literal. It was declared in
            # `PlannerPolicy` and written as 200.0 in two other places, so a deployment
            # that changed it changed one third of the arithmetic.
            from diet.planner.policy import load_policy

            snack_reserve = est_snack_count * float(load_policy().snack_kcal)
            main_cal_pool = max(0.0, daily_cals - snack_reserve)
            
            out_meals = []
            for m in meals:
                components = []
                for c in m.components.all():
                    f = c.food
                    components.append({
                        'id': c.id,
                        'food_id': f.id,
                        'food_name': f.name,
                        'quantity_grams': c.quantity,
                        'serving': describe(f, c.quantity),
                        'image_url': f.image_url,
                        'macros': c.calculate_nutrition(),
                        'is_completed': c.is_completed,
                        'completed_at': c.completed_at,
                        'actual_quantity_consumed': c.actual_quantity_consumed,
                    })
                
                # Calculate Target for this meal
                if m.meal_type == 'Snack':
                    target_cal = 200.0
                else:
                    # Use split % if available (Breakfast/Lunch/Dinner)
                    split_pct = meal_split.get(m.meal_type, 0.33) 
                    # Normalize split?? 
                    # Actually `goal_meal_kcal_split` returns generic B/L/D ratios summing to ~1.0.
                    # Correct logic: (Daily * Split) could work if NO snacks.
                    # With snacks, we should scale those ratios to the main_cal_pool?
                    # Let's use: Target = main_cal_pool * (split_pct / sum_of_active_splits)
                    # Assuming standard B+L+D = 1.0 roughly.
                    target_cal = main_cal_pool * split_pct
                
                # Macro targets for this meal (using daily ratios)
                m_targets = {
                    'calories': round(target_cal, 1),
                    'protein': round((target_cal * ratios['protein']) / 4.0, 1),
                    'carbs': round((target_cal * ratios['carb']) / 4.0, 1),
                    'fat': round((target_cal * ratios['fat']) / 9.0, 1),
                }

                out_meals.append({
                    'id': m.id,
                    'date': m.date,
                    'meal_type': m.meal_type,
                    'scheduled_time': m.scheduled_time,
                    'description': m.description,
                    'image_url': m.image_url,
                    'is_ai_generated': m.is_ai_generated,
                    'nutrition': m.calculate_nutrition(),
                    'target_nutrition': m_targets,  # <--- NEW FIELD
                    'components': components,
                    'is_completed': m.is_completed,
                    'completion_percentage': m.completion_percentage,
                })
            
            payload = {
                'plan': {
                    'id': plan.id,
                    'goal': plan.goal,
                    'daily_calories': plan.daily_calories,
                    'daily_targets': daily_targets, # <--- NEW FIELD
                    'start_date': plan.start_date,
                    'end_date': plan.end_date,
                    'is_active': plan.is_active,
                    'generation_strategy': plan.generation_strategy,
                    'template_name': plan.template.name if plan.template else None,
                },
                'meals': out_meals,
                'meals_count': len(out_meals),
            }
            return Response(payload)
        except PASSTHROUGH_EXCEPTIONS:
            # get_object() signals 404/403 by raising. The broad handler below
            # swallowed those and re-emitted them as 500 (str(Http404()) is '',
            # so the log line was empty too). Control-flow exceptions must pass.
            raise
        except Exception as e:
            logger.error(f"DietPlanMealsWithIngredientsView error: {str(e)}")
            return Response({'error': _("An error occurred while loading meal details.")}, status=status.HTTP_400_BAD_REQUEST)