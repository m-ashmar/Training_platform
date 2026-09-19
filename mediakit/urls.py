from django.urls import path

from .views import AppMediaView

app_name = "mediakit"

urlpatterns = [
    path("media/", AppMediaView.as_view(), name="app-media"),
]
