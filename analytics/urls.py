"""
Analytics App URLs

URL routing for analytics API endpoints.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    UserActivityViewSet, PerformanceMetricViewSet,
    UserGoalViewSet, UserSessionViewSet, AnalyticsDashboardViewSet
)

# Create DRF router
router = DefaultRouter()
router.register(r'activities', UserActivityViewSet, basename='activity')
router.register(r'metrics', PerformanceMetricViewSet, basename='metric')
router.register(r'goals', UserGoalViewSet, basename='goal')
router.register(r'sessions', UserSessionViewSet, basename='session')
router.register(r'dashboard', AnalyticsDashboardViewSet, basename='dashboard')

app_name = 'analytics'

urlpatterns = [
    path('api/analytics/', include(router.urls)),
] 