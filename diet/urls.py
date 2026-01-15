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
    path('v1/plans/generate-sync/', views.GenerateDietPlanSyncView.as_view(), name='v1_generate_plan_sync'),
    path('v1/plans/generate-rule/', views.GenerateDietPlanRuleBasedView.as_view(), name='v1_generate_plan_rule'),

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
    # v1 aliases for food catalog and preferences
    path('v1/food/search/', views.FoodSearchView.as_view(), name='v1_food_search'),
    path('v1/food/list/', views.FoodListView.as_view(), name='v1_food_list'),
    path('v1/food/categories/', views.FoodCategoryListView.as_view(), name='v1_food_categories'),
    path('v1/food/import/', views.FoodImportView.as_view(), name='v1_food_import'),
    path('v1/preferences/', views.UserPreferencesView.as_view(), name='v1_preferences'),
    path('v1/preferences/food-category/', views.UserFoodCategoryPreferenceView.as_view(), name='v1_user_food_category'),
    path('v1/preferences/food-category/<int:food_id>/', views.UserFoodCategoryPreferenceDetailView.as_view(), name='v1_user_food_category_detail'),
    
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
    # v1 aliases for trainer endpoints
    path('v1/trainer/templates/', views.TrainerTemplatesView.as_view(), name='v1_trainer_templates'),
    path('v1/trainer/diet-plans/', views.TrainerDietPlanView.as_view(), name='v1_trainer_diet_plans'),
    path('v1/trainer/meals/', views.TrainerMealView.as_view(), name='v1_trainer_meals'),
    path('v1/trainer/meals/<int:meal_id>/', views.TrainerMealView.as_view(), name='v1_trainer_meal_detail'),
    
    # --- CLIENT ENDPOINTS ---
    
    # Progress tracking
    path('api/client/progress/', views.ClientProgressView.as_view(), name='api_client_progress'),
    path('api/client/progress/weekly/', views.ClientWeeklyProgressView.as_view(), name='api_client_weekly_progress'),
    path('api/client/progress/enhanced/', views.EnhancedClientProgressView.as_view(), name='api_client_enhanced_progress'),
    
    # Meal interaction
    path('api/client/meals/interact/', views.ClientMealInteractionView.as_view(), name='api_client_meal_interaction'),
    path('api/client/meals/<int:meal_id>/', views.ClientMealDetailsView.as_view(), name='api_client_meal_details'),
    path('api/client/meals/<int:meal_id>/complete/', views.MealCompletionView.as_view(), name='api_client_meal_completion'),
    # v1 aliases for client endpoints
    path('v1/client/progress/', views.ClientProgressView.as_view(), name='v1_client_progress'),
    path('v1/client/progress/weekly/', views.ClientWeeklyProgressView.as_view(), name='v1_client_weekly_progress'),
    path('v1/client/progress/enhanced/', views.EnhancedClientProgressView.as_view(), name='v1_client_enhanced_progress'),
    path('v1/client/meals/interact/', views.ClientMealInteractionView.as_view(), name='v1_client_meal_interaction'),
    path('v1/client/meals/<int:meal_id>/', views.ClientMealDetailsView.as_view(), name='v1_client_meal_details'),
    path('v1/client/meals/<int:meal_id>/complete/', views.MealCompletionView.as_view(), name='v1_client_meal_completion'),
    
    # --- ENHANCED NUTRITIONAL TRACKING ENDPOINTS ---
    
    # Diet plan nutrition details
    path('api/nutrition/plan/<int:plan_id>/', views.DietPlanNutritionView.as_view(), name='api_diet_plan_nutrition'),
    
    # Meal components details
    path('api/meals/<int:meal_id>/components/', views.MealComponentsView.as_view(), name='api_meal_components'),
    path('preferences/food-category/', views.UserFoodCategoryPreferenceView.as_view(), name='user-food-category'),
    path('preferences/food-category/<int:food_id>/', views.UserFoodCategoryPreferenceDetailView.as_view(), name='user-food-category-detail'),
    # v1 aliases for nutrition and components
    path('v1/nutrition/plan/<int:plan_id>/', views.DietPlanNutritionView.as_view(), name='v1_diet_plan_nutrition'),
    path('v1/meals/<int:meal_id>/components/', views.MealComponentsView.as_view(), name='v1_meal_components'),
    
    # --- NEW CLIENT-FACING ENDPOINTS ---
    path('v1/my/diet-plans/', views.MyDietPlansView.as_view(), name='v1_my_diet_plans'),
    path('v1/plan/<int:plan_id>/meals-with-ingredients/', views.DietPlanMealsWithIngredientsView.as_view(), name='v1_plan_meals_with_ingredients'),
    
    # Client plan listing and plan meals+ingredients
    path('api/my/diet-plans/', views.MyDietPlansView.as_view(), name='api_my_diet_plans'),
    path('api/plan/<int:plan_id>/meals-with-ingredients/', views.DietPlanMealsWithIngredientsView.as_view(), name='api_plan_meals_with_ingredients'),
]