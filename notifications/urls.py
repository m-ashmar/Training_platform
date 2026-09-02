"""
notifications/urls.py — notification preference routes.

Notification listing/mark-read lives at /api/social/notifications/.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .trainer_message import TrainerMessageView
from .views import NotificationPreferenceViewSet

app_name = 'notifications'

router = DefaultRouter()
router.register(r'preferences', NotificationPreferenceViewSet, basename='notification-preference')

urlpatterns = [
    path('', include(router.urls)),
    # Trainer -> client free-text message. The `custom` event type existed with a
    # template but had no way to be produced.
    path('message-client/', TrainerMessageView.as_view(), name='message-client'),
]
