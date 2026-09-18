from django.urls import path

from .views import CapabilitiesView

app_name = "policy"

urlpatterns = [
    path("capabilities", CapabilitiesView.as_view(), name="capabilities"),
    path("capabilities/", CapabilitiesView.as_view(), name="capabilities-slash"),
]
