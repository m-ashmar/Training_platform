from django.urls import path

from .views import CapabilitiesView, SupportView, ThemeView

app_name = "policy"

urlpatterns = [
    path("capabilities", CapabilitiesView.as_view(), name="capabilities"),
    path("capabilities/", CapabilitiesView.as_view(), name="capabilities-slash"),
    path("support", SupportView.as_view(), name="support"),
    path("support/", SupportView.as_view(), name="support-slash"),
    path("theme", ThemeView.as_view(), name="theme"),
    path("theme/", ThemeView.as_view(), name="theme-slash"),
]
