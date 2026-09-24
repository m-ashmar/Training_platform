"""The trainer's own home, mounted at `/api/v1/trainer/` beside the coach's exercise
library (`mediakit/urls_trainer.py`). See `users/attention_queue.py`."""
from django.urls import path

from .attention_queue import AttentionQueueView

app_name = 'trainer_home'

urlpatterns = [
    path('attention/', AttentionQueueView.as_view(), name='attention'),
]
