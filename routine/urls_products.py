"""The buyer's side of routine products, under `/api/v1/products/` (plan step 10).
See `routine/product_views.py`."""
from django.urls import path

from .product_views import ProductBuyView, ProductCatalogueView, ProductDetailView

app_name = "products"

urlpatterns = [
    path("", ProductCatalogueView.as_view(), name="catalogue"),
    path("<int:pk>/", ProductDetailView.as_view(), name="detail"),
    path("<int:pk>/buy/", ProductBuyView.as_view(), name="buy"),
]
