"""
Social App URLs

URL routing for social networking API endpoints.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    UserFollowViewSet, PostViewSet, CommentViewSet,
    ChallengeViewSet, AchievementViewSet, NotificationViewSet,
    PublicUserProfileViewSet
)

# Create DRF router
router = DefaultRouter()
router.register(r'follows', UserFollowViewSet, basename='follow')
router.register(r'posts', PostViewSet, basename='post')
router.register(r'comments', CommentViewSet, basename='comment')
router.register(r'challenges', ChallengeViewSet, basename='challenge')
router.register(r'achievements', AchievementViewSet, basename='achievement')
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'users/public-profile', PublicUserProfileViewSet, basename='public-user-profile')

app_name = 'social'

urlpatterns = [
    path('api/social/', include(router.urls)),
] 