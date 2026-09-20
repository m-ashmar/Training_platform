"""The coach's own exercise library, mounted at `/api/v1/trainer/`.

Separate from `mediakit/urls.py` because that one is `/api/v1/content/` — the app's own
pictures and the catalogue's. These routes are a person's library, and the prefix says
so.
"""
from django.urls import path

from .trainer_views import TrainerExerciseMediaDetailView, TrainerExerciseMediaView

app_name = "trainer_media"

urlpatterns = [
    # Both spellings, and not for cosmetic reasons: `RequestSizeLimitMiddleware` runs
    # FIRST and matches on the exact path, so a POST to the unslashed spelling would
    # be measured against the 15 MB cap and refused before `CommonMiddleware` could
    # redirect it — and a redirected POST loses its body anyway. Both spellings are in
    # settings.LARGE_UPLOAD_PATHS; the three move together (mediakit/urls.py says the
    # same thing about the video route).
    path("exercise-media", TrainerExerciseMediaView.as_view(),
         name="exercise-media-no-slash"),
    path("exercise-media/", TrainerExerciseMediaView.as_view(), name="exercise-media"),
    path("exercise-media/<int:asset_id>/", TrainerExerciseMediaDetailView.as_view(),
         name="exercise-media-detail"),
]
