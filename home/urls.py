from django.urls import path

from .views import HomeView

app_name = "home"

# Both spellings, as `policy` and `mediakit` do: the contract names this route without a
# trailing slash, and a redirect would cost the app a round trip on the screen it opens on.
urlpatterns = [
    path("home", HomeView.as_view(), name="home"),
    path("home/", HomeView.as_view(), name="home-slash"),
]
