"""`/api/v1/coaching/` — see `users/coaching_views.py`."""
from django.urls import path

from .coaching_views import CurrentCoachingView

app_name = 'coaching'

urlpatterns = [
    path('current/', CurrentCoachingView.as_view(), name='current'),
]
