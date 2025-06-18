"""
urls.py - API Routing for Diet App

Defines all RESTful endpoints for food items, categories, user preferences, diet plans,
meals, meal components, and daily advice. Includes custom actions for AI plan generation,
Edamam import, and reporting. Uses DRF routers for standard resources and explicit paths
for custom features. All endpoints are versioned for scalability.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()

# --- Food Items ---
router.register(r'v1/foods', views.FoodItemViewSet, basename='fooditem')

# --- Food Categories ---
router.register(r'v1/categories', views.FoodCategoryViewSet, basename='foodcategory')

# --- User Food Preferences ---
router.register(r'v1/preferences', views.UserFoodPreferenceViewSet, basename='userfoodpreference')

# --- Diet Plans ---
router.register(r'v1/plans', views.DietPlanViewSet, basename='dietplan')

# --- Meals ---
router.register(r'v1/meals', views.MealViewSet, basename='meal')

# --- Meal Components ---
router.register(r'v1/meal-components', views.MealComponentViewSet, basename='mealcomponent')

# --- Daily Advice ---
router.register(r'v1/advice', views.DailyAdviceViewSet, basename='dailyadvice')

urlpatterns = [
    # Core RESTful endpoints
    path('', include(router.urls)),

    # --- Custom/Utility Endpoints ---

    # Food Item Search & Edamam Import
    path('v1/foods/search/', views.FoodItemSearchView.as_view(), name='fooditem-search'),
    path('v1/foods/import-edamam/', views.EdamamImportView.as_view(), name='fooditem-import-edamam'),

    # User Preferences: Generate Plan
    path('v1/preferences/<int:pk>/generate-plan/', views.GenerateDietPlanForPreferenceView.as_view(), name='generate-plan-for-preference'),

    # Diet Plan: Generate, Report, and GPT
    path('v1/plans/generate/', views.GenerateDietPlanView.as_view(), name='generate-diet-plan'),
    path('v1/plans/<int:pk>/report/', views.DietPlanReportView.as_view(), name='dietplan-report'),
    path('v1/plans/<int:pk>/gpt/', views.GPTDietPlanView.as_view(), name='gpt-diet-plan'),

    # Meals: Image Generation
    path('v1/meals/<int:pk>/image/', views.MealImageView.as_view(), name='meal-image'),

    # Daily Advice: Latest
    path('v1/advice/latest/', views.LatestDailyAdviceView.as_view(), name='latest-daily-advice'),

    # Admin/Utility: Trigger Edamam Import, GPT Plan Generation
    path('v1/admin/import-edamam/', views.AdminEdamamImportView.as_view(), name='admin-import-edamam'),
    path('v1/admin/generate-gpt-plan/', views.AdminGenerateGPTPlanView.as_view(), name='admin-generate-gpt-plan'),
]