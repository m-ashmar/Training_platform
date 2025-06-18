from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()

# --- Subscription Plans ---
router.register(r'v1/plans', views.SubscriptionPlanViewSet, basename='subscriptionplan')

# --- User Subscriptions ---
router.register(r'v1/subscriptions', views.SubscriptionViewSet, basename='subscription')

# --- Payments ---
router.register(r'v1/payments', views.PaymentViewSet, basename='payment')

# --- Features ---
router.register(r'v1/features', views.SubscriptionFeatureViewSet, basename='subscriptionfeature')

# --- Usage Tracking ---
router.register(r'v1/usage', views.SubscriptionUsageViewSet, basename='subscriptionusage')

urlpatterns = [
    # Core RESTful endpoints
    path('', include(router.urls)),
    
    # --- Custom/Utility Endpoints ---
    
    # Subscription Management (Admin)
    path('v1/admin/management/', views.SubscriptionManagementView.as_view(), name='subscription-management'),
    
    # Access Control
    path('v1/access/check/', views.SubscriptionAccessView.as_view(), name='subscription-access-check'),
    
    # User's Current Subscription
    path('v1/subscriptions/current/', views.SubscriptionViewSet.as_view({'get': 'current'}), name='current-subscription'),
    
    # --- Payment Gateway Endpoints ---
    
    # Payment Gateway Operations
    path('v1/gateways/', views.PaymentGatewayView.as_view(), name='payment-gateways'),
    
    # Payment Status Check
    path('v1/payments/<uuid:payment_id>/status/', views.PaymentStatusView.as_view(), name='payment-status'),
    
    # Webhook Endpoints (for payment gateway notifications)
    path('webhook/<str:gateway_name>/', views.PaymentWebhookView.as_view(), name='payment-webhook'),
] 