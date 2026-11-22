"""`/api/v1/coaching/` — see `users/coaching_views.py`."""
from django.urls import path

from .coaching_views import CoachingRenewalView, CurrentCoachingView

app_name = 'coaching'

urlpatterns = [
    path('current/', CurrentCoachingView.as_view(), name='current'),
    # The client's «do not renew after this month» (Road to 10 S2.7).
    path('renewal/', CoachingRenewalView.as_view(), name='renewal'),
]
