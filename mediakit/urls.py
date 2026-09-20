from django.urls import path

from .views import AppMediaView, ExerciseMediaView, VideoUploadView

app_name = "mediakit"

urlpatterns = [
    # Both spellings, as for capabilities: the contract names it without the slash
    # (BACKEND_BUGS B-035).
    path("media", AppMediaView.as_view(), name="app-media-no-slash"),
    path("media/", AppMediaView.as_view(), name="app-media"),
    # Video upload, both spellings. Not cosmetic here: `RequestSizeLimitMiddleware` runs
    # FIRST and matches on the exact path, so a POST to the unslashed spelling would be
    # measured against the 15 MB cap and refused before `CommonMiddleware` could redirect
    # it — and a redirected POST loses its body anyway. Both spellings are in
    # settings.LARGE_UPLOAD_PATHS; the three must move together.
    path("media/video", VideoUploadView.as_view(), name="video-upload-no-slash"),
    path("media/video/", VideoUploadView.as_view(), name="video-upload"),
    path("media/exercise/<int:exercise_id>/", ExerciseMediaView.as_view(),
         name="exercise-media"),
]
