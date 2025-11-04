"""
urls.py - API Routing for Diet App

Defines all RESTful endpoints for diet plan generation and daily advice.
Uses explicit paths for custom features. All endpoints are versioned for scalability.
Enhanced to support both AI-generated and trainer-created diet plans.
"""

from django.urls import path
from . import views

app_name = 'diet'

urlpatterns = [
    # --- Custom/Utility Endpoints ---

    # Diet Plan: Generate (AI - Clients Only)
    path('v1/plans/generate/', views.GenerateDietPlanView.as_view(), name='generate-diet-plan'),

    # Daily Advice: Latest
    path('v1/advice/latest/', views.DailyAdviceView.as_view(), name='latest-daily-advice'),

    # Web view for diet plan generation
    path('generate/', views.generate_diet_plan, name='generate-diet-plan-web'),

    # Web views
    path('generate-plan/', views.generate_diet_plan, name='generate_plan'),
    
    # API endpoints
    path('api/generate-plan/', views.GenerateDietPlanView.as_view(), name='api_generate_plan'),
    path('api/generate-plan-sync/', views.GenerateDietPlanSyncView.as_view(), name='api_generate_plan_sync'),
    path('api/generate-plan-rule/', views.GenerateDietPlanRuleBasedView.as_view(), name='api_generate_plan_rule'),
    path('api/daily-advice/', views.DailyAdviceView.as_view(), name='api_daily_advice'),
    
    # Food search and import endpoints (Available to all users)
    path('api/food/search/', views.FoodSearchView.as_view(), name='api_food_search'),
    path('api/food/list/', views.FoodListView.as_view(), name='api_food_list'),
    path('api/food/categories/', views.FoodCategoryListView.as_view(), name='api_food_categories'),
    path('api/food/import/', views.FoodImportView.as_view(), name='api_food_import'),
    
    # User preferences endpoints
    path('api/preferences/', views.UserPreferencesView.as_view(), name='api_preferences'),
    
    # --- TRAINER ENDPOINTS ---
    
    # Diet plan templates
    path('api/trainer/templates/', views.TrainerTemplatesView.as_view(), name='api_trainer_templates'),
    
    # Diet plan management
    path('api/trainer/diet-plans/', views.TrainerDietPlanView.as_view(), name='api_trainer_diet_plans'),
    
    # Meal management
    path('api/trainer/meals/', views.TrainerMealView.as_view(), name='api_trainer_meals'),
    path('api/trainer/meals/<int:meal_id>/', views.TrainerMealView.as_view(), name='api_trainer_meal_detail'),
    
    # --- CLIENT ENDPOINTS ---
    
    # Progress tracking
    path('api/client/progress/', views.ClientProgressView.as_view(), name='api_client_progress'),
    path('api/client/progress/weekly/', views.ClientWeeklyProgressView.as_view(), name='api_client_weekly_progress'),
    path('api/client/progress/enhanced/', views.EnhancedClientProgressView.as_view(), name='api_client_enhanced_progress'),
    
    # Meal interaction
    path('api/client/meals/interact/', views.ClientMealInteractionView.as_view(), name='api_client_meal_interaction'),
    path('api/client/meals/<int:meal_id>/', views.ClientMealDetailsView.as_view(), name='api_client_meal_details'),
    path('api/client/meals/<int:meal_id>/complete/', views.MealCompletionView.as_view(), name='api_client_meal_completion'),
    
    # --- ENHANCED NUTRITIONAL TRACKING ENDPOINTS ---
    
    # Diet plan nutrition details
    path('api/nutrition/plan/<int:plan_id>/', views.DietPlanNutritionView.as_view(), name='api_diet_plan_nutrition'),
    
    # Meal components details
    path('api/meals/<int:meal_id>/components/', views.MealComponentsView.as_view(), name='api_meal_components'),
    path('preferences/food-category/', views.UserFoodCategoryPreferenceView.as_view(), name='user-food-category'),
    path('preferences/food-category/<int:food_id>/', views.UserFoodCategoryPreferenceDetailView.as_view(), name='user-food-category-detail'),
]