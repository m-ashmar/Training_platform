from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.staticfiles.urls import staticfiles_urlpatterns

# drf-yasg imports
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

# Swagger restricted to admin users in production, authenticated in dev
_swagger_permission = permissions.AllowAny if getattr(settings, 'DEBUG', False) else permissions.IsAdminUser

schema_view = get_schema_view(
    openapi.Info(
        title="Training Platform API",
        default_version='v1',
        description="API documentation for the Training Platform",
        terms_of_service="https://www.google.com/policies/terms/",
        contact=openapi.Contact(email="support@example.com"),
        license=openapi.License(name="BSD License"),
    ),
    public=False,  # Not public — requires authentication
    permission_classes=(_swagger_permission,),
)

urlpatterns = [
    # Custom Admin Dashboard (replaces default Django admin)
    path("dj-admin/", include('admin_dashboard.urls')),

    # Original Django Admin (exposed separately)
    path("admin/", admin.site.urls),

    # API Authentication & User Management
    path('api/auth/', include('users.urls', namespace='users')),

    # Core API Applications
    path('api/routine/', include('routine.urls', namespace='routine')),
    path('api/subscription/', include('subscription.urls', namespace='subscription')),
    path('api/diet/', include('diet.urls', namespace='diet')),

    # New Feature APIs
    path('', include('analytics.urls')),  # Analytics API
    path('api/', include('achievements.urls')),  # Achievements API
    path('', include('social.urls')),     # Social Features API
    path('api/wallet/', include('wallet.urls', namespace='wallet')),
    path('api/ai/', include('ai_assistant.urls', namespace='ai_assistant')),
]

# Swagger/Redoc only served in DEBUG mode — completely hidden in production
if settings.DEBUG:
    urlpatterns += [
        path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
        path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
    ]

# Serve media files in development
if settings.DEBUG:
    # Serve app/static and contrib/admin static files in development
    urlpatterns += staticfiles_urlpatterns()
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Language-aware error handlers (prevent default English leakage)
handler404 = 'training_platform.error_handlers.handler404'
handler500 = 'training_platform.error_handlers.handler500'