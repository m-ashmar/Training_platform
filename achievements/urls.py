"""
Achievement URLs - API routing configuration.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AchievementViewSet, AchievementCategoriesView

# Create router
router = DefaultRouter()
router.register(r'achievements', AchievementViewSet, basename='achievement')

app_name = 'achievements'

urlpatterns = [
    # Router URLs
    path('', include(router.urls)),
    
    # Additional endpoints
    path('achievements/categories/', AchievementCategoriesView.as_view(), name='categories'),
]
