from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

# drf-yasg imports
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

schema_view = get_schema_view(
    openapi.Info(
        title="Training Platform API",
        default_version='v1',
        description="API documentation for the Training Platform",
        terms_of_service="https://www.google.com/policies/terms/",
        contact=openapi.Contact(email="support@example.com"),
        license=openapi.License(name="BSD License"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)

urlpatterns = [
    # Custom Admin Dashboard (replaces default Django admin)
    path("dj-admin/", include('admin_dashboard.urls')),

    # Original Django Admin (exposed separately)
    path("admin/", admin.site.urls),

    # API Authentication & User Management
    path('api/auth/', include('users.urls', namespace='users')),
    path('api/users/', include('users.urls', namespace='users')),

    # Core API Applications
    path('api/routine/', include('routine.urls', namespace='routine')),
    path('api/subscription/', include('subscription.urls', namespace='subscription')),
    path('api/diet/', include('diet.urls', namespace='diet')),
    
    # New Feature APIs
    path('', include('analytics.urls')),  # Analytics API
    path('', include('social.urls')),     # Social Features API
    path('api/wallet/', include('wallet.urls', namespace='wallet')),
    
    # Legacy dj-rest-auth endpoints (for backward compatibility)
    path('api/auth/', include('dj_rest_auth.urls')),

    # Swagger/OpenAPI/Redoc
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)