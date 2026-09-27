"""Checkout sessions under `/api/v1/checkout/` (the web payment hub, the payments spec
section 5). See `subscription/checkout_views.py`."""
from django.urls import path

from .checkout_views import CheckoutSessionDetailView, CheckoutSessionsView

app_name = "checkout"

# Both spellings of the collection, as the capabilities route: the spec names it without
# the slash, and a POST cannot be redirected to the other one.
urlpatterns = [
    path("sessions", CheckoutSessionsView.as_view(), name="sessions"),
    path("sessions/", CheckoutSessionsView.as_view(), name="sessions-slash"),
    path("sessions/<uuid:session_id>/", CheckoutSessionDetailView.as_view(), name="session"),
]
