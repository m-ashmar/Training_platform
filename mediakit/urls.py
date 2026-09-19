from django.urls import path

from .views import AppMediaView

app_name = "mediakit"

urlpatterns = [
    # Both spellings, as for capabilities: the contract names it without the slash
    # (BACKEND_BUGS B-035).
    path("media", AppMediaView.as_view(), name="app-media-no-slash"),
    path("media/", AppMediaView.as_view(), name="app-media"),
]
