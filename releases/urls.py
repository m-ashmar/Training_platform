from django.urls import path

from .views import LatestReleaseView

app_name = "releases"

# Both spellings, as the capabilities route: the spec names it without the slash.
urlpatterns = [
    path("latest", LatestReleaseView.as_view(), name="latest"),
    path("latest/", LatestReleaseView.as_view(), name="latest-slash"),
]
