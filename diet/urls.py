"""
urls.py — API routing for the diet app.

CONTRACT: every endpoint is served at exactly one path, under `v1/`.

Until the Phase 16 freeze every view here was mounted three ways — `v1/...`,
`api/...` and in two cases with no prefix at all — so `/api/diet/api/food/list/` and
`/api/diet/v1/food/list/` were the same view at two URLs with nothing marking either
as canonical. Two more paths (`generate/`, `generate-plan/`) served a session-
authenticated HTML page from under `/api/`, which answered a mobile client with a 302
to a login page rather than JSON.

The duplicates are removed rather than deprecated: the mobile client does not exist
yet, so there is no consumer to break, and this is the last moment where removing them
is free. After the freeze, changes here are additive or go behind `v2/`.
"""

from django.urls import path

from . import views

app_name = 'diet'

urlpatterns = [
    # --- Plan generation (clients) ---
    path('v1/plans/generate/', views.GenerateDietPlanView.as_view(), name='generate-diet-plan'),
    path('v1/plans/generate-sync/', views.GenerateDietPlanSyncView.as_view(), name='v1_generate_plan_sync'),
    path('v1/plans/generate-rule/', views.GenerateDietPlanRuleBasedView.as_view(), name='v1_generate_plan_rule'),

    # --- Daily advice ---
    path('v1/advice/latest/', views.DailyAdviceView.as_view(), name='latest-daily-advice'),

    # --- Food catalogue ---
    path('v1/food/search/', views.FoodSearchView.as_view(), name='v1_food_search'),
    path('v1/food/list/', views.FoodListView.as_view(), name='v1_food_list'),
    path('v1/food/categories/', views.FoodCategoryListView.as_view(), name='v1_food_categories'),
    path('v1/food/import/', views.FoodImportView.as_view(), name='v1_food_import'),

    # --- Preferences ---
    path('v1/preferences/', views.UserPreferencesView.as_view(), name='v1_preferences'),
    path('v1/preferences/food-category/', views.UserFoodCategoryPreferenceView.as_view(), name='v1_user_food_category'),
    path('v1/preferences/food-category/<int:food_id>/', views.UserFoodCategoryPreferenceDetailView.as_view(), name='v1_user_food_category_detail'),

    # --- Trainer ---
    path('v1/trainer/templates/', views.TrainerTemplatesView.as_view(), name='v1_trainer_templates'),
    path('v1/trainer/diet-plans/', views.TrainerDietPlanView.as_view(), name='v1_trainer_diet_plans'),
    path('v1/trainer/meals/', views.TrainerMealView.as_view(), name='v1_trainer_meals'),
    path('v1/trainer/meals/<int:meal_id>/', views.TrainerMealView.as_view(), name='v1_trainer_meal_detail'),

    # --- Client progress ---
    path('v1/client/progress/', views.ClientProgressView.as_view(), name='v1_client_progress'),
    path('v1/client/progress/weekly/', views.ClientWeeklyProgressView.as_view(), name='v1_client_weekly_progress'),
    path('v1/client/progress/enhanced/', views.EnhancedClientProgressView.as_view(), name='v1_client_enhanced_progress'),

    # --- Client meals ---
    path('v1/client/meals/interact/', views.ClientMealInteractionView.as_view(), name='v1_client_meal_interaction'),
    path('v1/client/meals/<int:meal_id>/', views.ClientMealDetailsView.as_view(), name='v1_client_meal_details'),
    path('v1/client/meals/<int:meal_id>/complete/', views.MealCompletionView.as_view(), name='v1_client_meal_completion'),

    # --- Nutrition detail ---
    path('v1/nutrition/plan/<int:plan_id>/', views.DietPlanNutritionView.as_view(), name='v1_diet_plan_nutrition'),
    path('v1/meals/<int:meal_id>/components/', views.MealComponentsView.as_view(), name='v1_meal_components'),

    # --- Client plan listing ---
    path('v1/my/diet-plans/', views.MyDietPlansView.as_view(), name='v1_my_diet_plans'),
    path('v1/plan/<int:plan_id>/meals-with-ingredients/', views.DietPlanMealsWithIngredientsView.as_view(), name='v1_plan_meals_with_ingredients'),
]
