from django.contrib import admin
import re

from django.urls import path, include, re_path
from django.conf import settings
from django.contrib.staticfiles.urls import staticfiles_urlpatterns

from training_platform.media_views import serve_media
from trainer_verification.storage import serve_private_document

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

    # Data-subject rights (GDPR Art. 15 export / Art. 17 erasure), derived from the
    # personal-data registry in training_platform/privacy/.
    path('api/privacy/', include('training_platform.privacy.urls', namespace='privacy')),

    # What each build may show and sell (X-App-Channel). See policy/models.py.
    path('api/v1/config/', include('policy.urls', namespace='policy')),

    # Pictures the app itself shows (intro story, banners, empty states). Media for
    # trainers, exercises and programs travels inside those objects. See mediakit/.
    path('api/v1/content/', include('mediakit.urls', namespace='mediakit')),
    # A coach's OWN demonstration of a shared exercise, so their client watches them
    # rather than a stock clip. See mediakit/trainer_views.py; whose media a given
    # viewer is shown is mediakit/resolution.py and nowhere else.
    path('api/v1/trainer/', include('mediakit.urls_trainer', namespace='trainer_media')),
    # The trainer's own verification: documents, submit, state (trainer_verification/).
    # The operator side is the console at /dj-admin/.
    path('api/v1/trainer/verification/',
         include('trainer_verification.urls', namespace='trainer_verification')),
    # The trainer's home queue: requests, form checks, idle and slipping clients and
    # programmes ending, in one ordered list (users/attention_queue.py, B-091).
    path('api/v1/trainer/', include('users.urls_trainer_home', namespace='trainer_home')),
    # The trainer's withdrawals and payout methods (payouts/, B-098). The operator side
    # is the withdrawal queue in the console at /dj-admin/.
    path('api/v1/trainer/', include('payouts.urls', namespace='payouts')),

    # The operator's phone (16-agent-operator/OperatorPhone): the four queue counts and
    # a sign-in link for the web console, off unless OPS_QR_LOGIN_ENABLED (B-119, B-120).
    # Everything else operators do is the console at /dj-admin/. See admin_dashboard/.
    path('api/v1/ops/', include('admin_dashboard.api_urls', namespace='ops')),

    # The design-v2 trainer screens: roster, profile, matches. See users/trainer_views.py.
    path('api/v1/trainers/', include('users.urls_v1', namespace='trainers')),
    # The client's monthly coaching (users/coaching.py).
    path('api/v1/coaching/', include('users.urls_coaching', namespace='coaching')),

    # Text messages between a trainer and their client. See chat/views.py; the live
    # half is the WebSocket at ws/chat/<id>/ (chat/routing.py).
    path('api/v1/chat/', include('chat.urls', namespace='chat')),
    # Reporting, blocking and AI consent: the safety layer a store review asks for.
    # Mounted at the version root, not under a prefix of its own — `reports/`,
    # `blocks/` and `ai-consent/` are platform-wide, not one feature's sub-resource.
    path('api/v1/', include('moderation.urls', namespace='moderation')),

    # The server-driven home screen: an ordered list of typed sections, built from
    # what every other area already answers. See home/views.py for the contract.
    path('api/v1/', include('home.urls', namespace='home')),

    # Notification preferences (listing lives under /api/social/notifications/)
    path('api/notifications/', include('notifications.urls', namespace='notifications')),
]

# Swagger/Redoc only served in DEBUG mode — completely hidden in production
if settings.DEBUG:
    urlpatterns += [
        path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
        path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
    ]

# Static files. In production WhiteNoise (see MIDDLEWARE) serves STATIC_ROOT;
# this pattern only covers the dev server, which has no WhiteNoise in the chain.
if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()

# User-uploaded media.
# NOTE: `static()` is deliberately NOT used here. It returns [] whenever DEBUG is
# false, so it silently registers nothing in production and every upload 404s —
# moving it out of the `if settings.DEBUG:` block above did not fix that.
# When an external storage backend is configured (GCS, S3/R2), files are served by that
# backend's own signed URLs and Django must not serve them locally — MEDIA_ROOT is empty
# there, so this route could only ever answer 404 while still costing an app worker.
_SERVES_ITS_OWN_URLS = (getattr(settings, 'USE_EXTERNAL_MEDIA_STORAGE', False)
                        or getattr(settings, 'MEDIA_BACKEND', 'local') != 'local')
if not _SERVES_ITS_OWN_URLS:
    urlpatterns += [
        re_path(
            r'^%s(?P<path>.*)$' % re.escape(settings.MEDIA_URL.lstrip('/')),
            serve_media,
            name='serve-media',
        ),
        # Identity documents on the filesystem backend: a signed, expiring URL is the
        # only way in (trainer_verification/storage.py). On GCS the bucket signs its own.
        re_path(
            r'^%s(?P<path>.*)$' % re.escape(
                getattr(settings, 'PRIVATE_MEDIA_URL', '/private-media/').lstrip('/')),
            serve_private_document,
            name='serve-private-document',
        ),
    ]

# Language-aware error handlers (prevent default English leakage)
handler404 = 'training_platform.error_handlers.handler404'
handler500 = 'training_platform.error_handlers.handler500'