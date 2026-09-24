"""The trainer's side of routine products, under `/api/v1/trainer/products/` (plan
step 10). See `routine/product_views.py`."""
from django.urls import path

from .product_views import (
    TrainerProductDetailView, TrainerProductRetireView, TrainerProductSubmitView,
    TrainerProductsView,
)

app_name = "trainer_products"

urlpatterns = [
    path("", TrainerProductsView.as_view(), name="list"),
    path("<int:pk>/", TrainerProductDetailView.as_view(), name="detail"),
    path("<int:pk>/submit/", TrainerProductSubmitView.as_view(), name="submit"),
    path("<int:pk>/retire/", TrainerProductRetireView.as_view(), name="retire"),
]
