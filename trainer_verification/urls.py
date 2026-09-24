"""The trainer's own verification, mounted at `/api/v1/trainer/verification/`."""
from django.urls import path

from .views import DocumentView, MyVerificationView, SubmitView

app_name = "trainer_verification"

urlpatterns = [
    path("", MyVerificationView.as_view(), name="mine"),
    path("documents/<str:slot>/", DocumentView.as_view(), name="document"),
    path("submit/", SubmitView.as_view(), name="submit"),
]
