"""`/api/v1/trainers/` — the design-v2 trainer screens. See `users/trainer_views.py`."""
from django.urls import path

from .trainer_views import TrainerDetailView, TrainerMatchesView, TrainerRosterView

app_name = 'trainers'

urlpatterns = [
    path('', TrainerRosterView.as_view(), name='roster'),
    # Both spellings, as for capabilities: the design's contract names it without the
    # slash, and a client that normalises paths adds one.
    path('matches', TrainerMatchesView.as_view(), name='matches'),
    path('matches/', TrainerMatchesView.as_view(), name='matches-slash'),
    path('<int:trainer_id>/', TrainerDetailView.as_view(), name='detail'),
]
