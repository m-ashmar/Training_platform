"""
settings_base.py - Shared configuration for all environments.

This file contains configuration that is common across local and production.
No secrets are hardcoded here. All secrets are loaded via settings_secrets helpers.
Environment-specific overrides live in settings_local.py and settings_production.py.
"""

from pathlib import Path
import os
import logging
from datetime import timedelta

from .settings_secrets import require_env, get_env, get_int_env, get_secret

# ========================
# Path Configuration
# ========================
BASE_DIR = Path(__file__).resolve().parent.parent

# ========================
# Security Configuration (secrets loaded from environment)
# ========================
SECRET_KEY = get_secret("DJANGO_SECRET_KEY")

# Support hitless key rotation — old key remains valid for active sessions
_old_key = os.environ.get("DJANGO_OLD_SECRET_KEY_1")
SECRET_KEY_FALLBACKS = [_old_key] if _old_key else []

# Key for EncryptedTextField (training_platform/encrypted_fields.py), which protects
# free-text health data at rest. Comma-separated for rotation: the first key encrypts,
# all of them decrypt. Optional here so local development runs without it; production
# refuses to boot without it (see enforce_production_safety).
FIELD_ENCRYPTION_KEY = get_env("FIELD_ENCRYPTION_KEY", "")

# ========================
# Application Definition
# ========================
INSTALLED_APPS = [
    # Translation
    "modeltranslation",

    # Django Core
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",

    # Transparent ORM Caching (must be before rest_framework and apps)
    "cachalot",

    # Project Apps
    "drf_yasg",
    "users",
    "routine",
    "diet",
    "subscription",
    "challenges",
    "analytics",
    "achievements",
    "social",
    "admin_dashboard",
    "wallet",
    "notifications",
    "ai_assistant",

    # Third-party
    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "dj_rest_auth",
    "dj_rest_auth.registration",
    "corsheaders",
    "channels",
    # Used by routine.views via DjangoFilterBackend — must be installed so its
    # app checks and templates load.
    "django_filters",
]

ASGI_APPLICATION = "training_platform.asgi.application"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            # DB5: dedicated channel layer — isolated from sessions (DB0) and all other caches
            "hosts": [f"redis://{get_env('REDIS_HOST', '127.0.0.1')}:{get_int_env('REDIS_PORT', 6379)}/5"],
        },
    },
}

# SecurityMiddleware MUST be first (per Django security best practices)
MIDDLEWARE = [
    # Must run first: rejects oversized bodies before Django buffers them to disk.
    'training_platform.middleware.RequestSizeLimitMiddleware',
    "django.middleware.security.SecurityMiddleware",
    # Serves everything collectstatic wrote to STATIC_ROOT. Without it nothing serves
    # /static/ at all once DEBUG is off: staticfiles_urlpatterns() is dev-only and there
    # is no CDN or NGINX in front on Fly. The admin dashboard at /dj-admin/ was loading
    # with no CSS or JS. Must be immediately after SecurityMiddleware so redirects and
    # security headers still apply to static responses.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "training_platform.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",

    # Auth
    "allauth.account.middleware.AccountMiddleware",

    "training_platform.middleware.LanguageResolutionMiddleware",
    "training_platform.middleware.RateLimitMiddleware",
    "training_platform.middleware.RequestLoggingMiddleware",
    "training_platform.middleware.DatabaseQueryCountMiddleware",
    "training_platform.middleware.CacheMiddleware",
    "training_platform.middleware.APIVersionMiddleware",
    "training_platform.middleware.ErrorHandlingMiddleware",
]

# ========================
# URL & Template Config
# ========================
ROOT_URLCONF = "training_platform.urls"
WSGI_APPLICATION = "training_platform.wsgi.application"
SITE_ID = 1

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / 'templates'],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
            ],
        },
    },
]

# ========================
# Authentication
# ========================
AUTH_USER_MODEL = 'users.CustomUser'
AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Allauth — disable native signup flow, force through custom OTP adapter
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_AUTHENTICATION_METHOD = 'email'
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_ADAPTER = "users.adapters.CustomAccountAdapter"

# REST Auth
REST_AUTH_SERIALIZERS = {
    'REGISTER_SERIALIZER': 'users.serializers.CustomRegisterSerializer',
    'LOGIN_SERIALIZER': 'users.serializers.CustomLoginSerializer',
}

# ========================
# REST Framework
# ========================
from django.utils.translation import gettext_lazy as _

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'charging': '10/minute',
    },
    # ONE pagination shape for every list endpoint: {count, next, previous, results}.
    # Previously three shapes were in play (paginated dict, bare array, cursor) and
    # seven viewsets had no pagination at all, so they returned EVERY row — an
    # unbounded response on a 512MB box and three parsers for the mobile client.
    # Project paginator: same {count, next, previous, results} shape as before, but
    # ?page_size=N is honoured (capped at 100) instead of silently ignored.
    'DEFAULT_PAGINATION_CLASS': 'training_platform.pagination.StandardPagination',
    'PAGE_SIZE': 25,
    'NON_FIELD_ERRORS_KEY': 'non_field_errors',
    # One error shape with a locale-independent `code`. The API is bilingual, so a
    # client branching on translated message text breaks as soon as a user switches
    # to Arabic. See training_platform/exception_handler.py.
    'EXCEPTION_HANDLER': 'training_platform.exception_handler.api_exception_handler',
}

# ========================
# Wallet / Agent configuration
# ========================
from decimal import Decimal as _Decimal
# Agent top-up caps. IMPORTANT: limits are enforced fail-closed — a value of 0
# means "no top-ups permitted", NOT unlimited. To allow more, raise the value
# (tunable per-agent in the admin). Launch model: trusted prepaid cash-in.
AGENT_DEFAULT_DAILY_LIMIT = _Decimal(get_env("AGENT_DEFAULT_DAILY_LIMIT", "200"))
AGENT_DEFAULT_MONTHLY_LIMIT = _Decimal(get_env("AGENT_DEFAULT_MONTHLY_LIMIT", "5000"))
# Optional dedicated Fernet key (base64) for encrypting agent API secrets at rest.
# If unset, a key is derived from SECRET_KEY — a DB leak alone still cannot forge
# signatures, since SECRET_KEY is not stored in the database.
AGENT_APIKEY_ENC_KEY = get_env("AGENT_APIKEY_ENC_KEY", "")

# ========================
# JWT Configuration — RS256 asymmetric signing
# Private key signs tokens; public key verifies.
# Both loaded from secrets manager — never hardcoded.
# NOTE: Changing from HS256 to RS256 invalidates all previously issued tokens.
# ========================
_jwt_private_key = get_secret("JWT_PRIVATE_KEY")   # PEM-encoded RSA private key
_jwt_public_key  = get_secret("JWT_PUBLIC_KEY")    # PEM-encoded RSA public key

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,

    # RS256: asymmetric — private key signs, public key verifies
    'ALGORITHM': 'RS256',
    'SIGNING_KEY': _jwt_private_key,
    'VERIFYING_KEY': _jwt_public_key,
    'AUDIENCE': None,
    'ISSUER': None,
    'JWK_URL': None,
    'LEEWAY': 0,

    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    'USER_AUTHENTICATION_RULE': 'rest_framework_simplejwt.authentication.default_user_authentication_rule',

    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
    'TOKEN_TYPE_CLAIM': 'token_type',
    'TOKEN_USER_CLASS': 'rest_framework_simplejwt.models.TokenUser',

    'JTI_CLAIM': 'jti',

    'SLIDING_TOKEN_REFRESH_EXP_CLAIM': 'refresh_exp',
    'SLIDING_TOKEN_LIFETIME': timedelta(minutes=60),
    'SLIDING_TOKEN_REFRESH_LIFETIME': timedelta(days=1),
}

# ========================
# CORS Configuration
# ========================
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in get_env("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

# ========================
# AI Integration — All keys from environment, no hardcoded tokens
# ========================
# Optional integrations: loaded with get_env so a missing key disables the
# feature (graceful degradation) instead of crashing the whole app at startup.
# This matches deploy-pipeline.md, which lists these as "optional but expected".
OPENAI_API_KEY = get_env("OPENAI_API_KEY", "")
OPENAI_MODEL = get_env("OPENAI_MODEL", "gpt-4o-mini")
AI_PROVIDER = get_env("AI_PROVIDER", "openai")
HUGGINGFACE_API_TOKEN = get_env("HUGGINGFACE_API_TOKEN", "")

AI_ASSISTANT_CONFIG = {
    "MODEL": get_env("AI_ASSISTANT_MODEL", "gpt-4o-mini"),
    "MAX_RESPONSE_TOKENS": 2000,
    "TEMPERATURE": 0.7,
    "MAX_TOOL_CALLS_PER_TURN": 5,
    "MAX_MESSAGES_PER_DAY": 50,
    "SESSION_TIMEOUT_MINUTES": 30,
    "DAILY_COST_ALERT_USD": "50.00",
    # Hard platform-wide ceiling. DAILY_COST_ALERT_USD only logs; this one
    # actually refuses further completions once the day's spend passes it.
    "DAILY_COST_LIMIT_USD": "200.00",
    "SYSTEM_PROMPT_BUDGET": 800,
    "HISTORY_BUDGET": 3000,
    "TOOL_RESULTS_BUDGET": 2000,
    "MAX_INPUT_LENGTH": 2000,
}

# ========================
# Celery
# ========================
# DB6/DB7 — deliberately outside the 6 logical DBs the caches use. The default used to
# be DB0, which settings_production assigns to SESSIONS, so queue keys and session keys
# shared a keyspace.
CELERY_BROKER_URL = get_env('CELERY_BROKER_URL', 'redis://localhost:6379/6')
CELERY_RESULT_BACKEND = get_env('CELERY_RESULT_BACKEND', 'redis://localhost:6379/7')

# Acknowledge a task only after it finishes. With the default (ack on delivery) a worker
# killed mid-task — routine on Fly, where machines stop when idle — dropped the job
# permanently. Paired with idempotent consumers this gives at-least-once delivery.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# A hung external call (LLM, Edamam, FCM) previously held its worker slot forever.
CELERY_TASK_SOFT_TIME_LIMIT = int(get_env('CELERY_TASK_SOFT_TIME_LIMIT', 300))
CELERY_TASK_TIME_LIMIT = int(get_env('CELERY_TASK_TIME_LIMIT', 360))
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_EAGER_PROPAGATES = True

from celery.schedules import crontab

# autodiscover_tasks() only scans `<installed_app>/tasks.py`. These two live outside
# that pattern — one in a subpackage of the diet app, one in a plain package that is not
# an installed app — so the worker never registered them and both scheduled jobs (the
# planner's learning loop and the GDPR retention purge) silently never ran. Beat does
# not error on an unregistered task name; it just skips it.
CELERY_IMPORTS = (
    'diet.planner.tasks',
    'training_platform.privacy.tasks',
)

CELERY_BEAT_SCHEDULE = {
    # Workout reminders. `session_reminder` was registered and templated from the
    # start but nothing ever emitted it, so the platform had no re-engagement loop.
    # Hourly, not daily: the task itself decides who is due, by comparing each user's
    # local hour (via their preferred_timezone) against their workout_reminder_hour.
    # A single fixed-UTC daily run would reach users at a different point in each of
    # their days.
    'send-workout-reminders': {
        'task': 'notifications.send_workout_reminders',
        'schedule': crontab(minute=0),
    },
    # Was declared in celery.py as `app.conf.beat_schedule = {...}` AFTER
    # config_from_object(). That config loads lazily, so settings won on first access
    # and the assignment was discarded — the task existed, was scheduled on paper, and
    # never ran once. It belongs here, with every other periodic task.
    'generate-daily-advice': {
        'task': 'diet.tasks.generate_daily_advice',
        'schedule': crontab(hour=6, minute=0),  # 06:00 daily
    },
    # The dead-letter queue was written to and never read. This replays what can
    # be replayed and escalates what cannot.
    'drain-notification-dlq': {
        'task': 'notifications.drain_dead_letter_queue',
        'schedule': 3600,
    },
    # Turns is_liked / is_completed / actual_quantity_consumed — all collected and
    # previously read by nothing — into planner ranking weights.
    'refresh-food-weights': {
        'task': 'diet.planner.refresh_food_weights',
        'schedule': 86400,
    },
    # Retention. Every source in training_platform/privacy/sources.py that declares
    # retention_days is enforced here — analytics IPs at 180 days, notifications at
    # 90, OTP/reset tokens at 1.
    'purge-expired-personal-data': {
        'task': 'training_platform.privacy.purge_expired_personal_data',
        'schedule': 86400,
    },
    'close-idle-ai-sessions': {
        'task': 'ai_assistant.tasks.close_idle_sessions',
        'schedule': 600,
    },
    'compute-daily-ai-insights': {
        'task': 'ai_assistant.tasks.compute_all_user_insights',
        'schedule': 86400,
    },
    'check-ai-cost-alert': {
        'task': 'ai_assistant.tasks.check_daily_cost',
        'schedule': 3600,
    },
}

# ========================
# Nutrition & AI Chef
# ========================
AI_CHEF_CONFIG = {
    "MAX_RETRIES": 2,
    "TEMPERATURE": 0.3,
    "MACRO_TOLERANCE": {
        "calories": 0.1,
        "protein": 0.15,
        "carbs": 0.2,
        "fat": 0.25,
    },
    "PORTION_GUARDRAILS": {
        "protein": (50, 350),
        "carb": (100, 400),
        "fat": (20, 100),
        "per_meal": {
            "protein": (20, 150),
            "carb": (30, 200),
            "fat": (5, 30),
        },
    },
    "EDAMAM": {
        "MAX_RESULTS": 8,
        "CACHE_TTL": 86400,
    },
}

DIET_SMART_MACRO_PLANNER = True
DIET_DYNAMIC_MEAL_ALLOCATION = True
DIET_STAGED_MEAL_FILL = True

# Edamam API — optional; empty disables nutrition lookups (graceful degradation)
EDAMAM_APP_ID = get_env("EDAMAM_APP_ID", "")
EDAMAM_APP_KEY = get_env("EDAMAM_APP_KEY", "")

# ========================
# Firebase / Push Notifications — optional; empty disables push (already degrades)
# ========================
FIREBASE_CREDENTIALS_PATH = get_env("FIREBASE_CREDENTIALS_PATH", "")
FIREBASE_PROJECT_ID = get_env("FIREBASE_PROJECT_ID", "")

# ========================
# Internationalization & Localization
# ========================
LANGUAGE_CODE = "en"
# Server-local timezone. This drives timezone.localdate(), which every training
# date, streak and "today" check depends on. It was UTC while the user base is
# UTC+3, so workouts logged 00:00-03:00 local were stored on the PREVIOUS day
# (breaking streaks) and a legitimate "today" could be rejected as a future date.
# Env-overridable so a future region change needs no code edit.
TIME_ZONE = get_env("TIME_ZONE", "Asia/Damascus")
USE_I18N = True
USE_L10N = True
USE_TZ = True

LANGUAGES = (
    ('en', _('English')),
    ('ar', _('Arabic')),
)

LOCALE_PATHS = [
    BASE_DIR / 'locale',
]

# ========================
# Static & Media Files
# ========================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

MEDIA_URL = '/media/'
# MEDIA_ROOT must NOT sit inside the container image: Fly's filesystem is ephemeral
# and `min_machines_running = 0` stops the machine when idle, so every upload was
# destroyed on restart/redeploy. In production this points at a mounted volume
# (fly.toml [mounts] -> /data), overridable by env for an S3/R2 migration later.
MEDIA_ROOT = get_env("MEDIA_ROOT", str(BASE_DIR / 'media'))

# Set true once files are served by an external backend (S3/R2/CDN); Django then
# stops serving /media/ itself. Keeping local serving is fine at current scale but
# it does occupy an app worker per file request.
USE_EXTERNAL_MEDIA_STORAGE = get_env("USE_EXTERNAL_MEDIA_STORAGE", "False") == "True"

# ========================
# Default Auto Field
# ========================
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ========================
# Email Configuration (base — overridden per environment)
# ========================
EMAIL_HOST_USER = get_env('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = get_env('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = get_env('DEFAULT_FROM_EMAIL', 'noreply@trainingplatform.com')

# ========================
# CSRF Configuration
# ========================
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in get_env("CSRF_TRUSTED_ORIGINS", "http://localhost").split(",")
    if origin.strip()
]

# ========================
# Platform Escrow
# ========================
PLATFORM_ESCROW_EMAIL = get_env('PLATFORM_ESCROW_EMAIL', 'platform_escrow@local')
PLATFORM_ESCROW_USERNAME = get_env('PLATFORM_ESCROW_USERNAME', 'platform_escrow')

# ========================
# Logging Configuration
# ========================
os.makedirs(BASE_DIR / 'logs', exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {asctime} {message}',
            'style': '{',
        },
    },
    'filters': {
        'require_debug_true': {
            '()': 'django.utils.log.RequireDebugTrue',
        },
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse',
        },
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
            'filters': ['require_debug_true'],
        },
        'file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs/django.log',
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'diet_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs/diet.log',
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'routine_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs/routine.log',
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'users_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs/users.log',
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'subscription_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs/subscription.log',
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'challenges_file': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs/challenges.log',
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'verbose',
        },
        'mail_admins': {
            'level': 'ERROR',
            'class': 'django.utils.log.AdminEmailHandler',
            'formatter': 'verbose',
            'filters': ['require_debug_false'],
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file', 'mail_admins'],
            'level': 'INFO',
            'propagate': True,
        },
        'django.request': {
            'handlers': ['console', 'file', 'mail_admins'],
            'level': 'ERROR',
            'propagate': False,
        },
        'django.server': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.db.backends': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'diet': {
            'handlers': ['console', 'diet_file', 'mail_admins'],
            'level': 'INFO',
            'propagate': False,
        },
        'routine': {
            'handlers': ['console', 'routine_file', 'mail_admins'],
            'level': 'INFO',
            'propagate': False,
        },
        'users': {
            'handlers': ['console', 'users_file', 'mail_admins'],
            'level': 'INFO',
            'propagate': False,
        },
        'subscription': {
            'handlers': ['console', 'subscription_file', 'mail_admins'],
            'level': 'INFO',
            'propagate': False,
        },
        'challenges': {
            'handlers': ['console', 'challenges_file', 'mail_admins'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# ========================
# Transparent ORM Dictionary Caching (django-cachalot)
# ========================
CACHALOT_CACHE = 'default'  # Map DB queries naturally to shared DB0 backend
CACHALOT_TIMEOUT = 86400  # Long duration because cachalot automatically invalidates on every write
CACHALOT_UNCACHABLE_TABLES = frozenset([
    # CRITICAL: Prevent race conditions by bypassing cache on ledger/auth reads
    'wallet_agentapikey', 'wallet_wallet', 'wallet_transaction',
    'auth_user', 'users_customuser', 'django_session',
    'auth_permission', 'auth_group', 'django_admin_log',
    'django_celery_results_taskresult',
    # Financial state — reads drive idempotency/activation; must never be stale
    'wallet_idempotencykey', 'wallet_walletauditlog', 'wallet_agentprofile',
    'subscription_subscription', 'subscription_payment', 'subscription_subscriptionusage',
    # Auth/security — token revocation & permission boundaries must not be stale
    'token_blacklist_blacklistedtoken', 'token_blacklist_outstandingtoken',
    'users_trainerclientrelation', 'users_otpverification', 'users_passwordresettoken',
])

# Multipart bodies are buffered here before a view ever sees them. Unset, this is the
# container's ephemeral rootfs rather than the mounted volume, so a large upload fills
# the machine's disk. Paired with MAX_REQUEST_BODY_BYTES, which caps the body up front.
MAX_REQUEST_BODY_BYTES = int(get_env('MAX_REQUEST_BODY_BYTES', 15 * 1024 * 1024))
FILE_UPLOAD_TEMP_DIR = get_env('FILE_UPLOAD_TEMP_DIR', None) or None

# Media is served without per-request auth (the mobile client cannot attach a JWT
# to image loads), so URLs are signed and time-limited instead. Signing happens in
# SignedMediaStorage.url(), so every `.url` in every serializer is covered.
MEDIA_URL_SIGNING = get_env('MEDIA_URL_SIGNING', 'True').lower() in ('1', 'true', 'yes')
MEDIA_URL_TTL = int(get_env('MEDIA_URL_TTL', 24 * 3600))
STORAGES = {
    # Only signs when Django itself serves the files. With an external backend the
    # signature would be meaningless — that provider issues (and secures) its own URLs.
    'default': {'BACKEND': (
        'django.core.files.storage.FileSystemStorage'
        if USE_EXTERNAL_MEDIA_STORAGE
        else 'training_platform.media_storage.SignedMediaStorage'
    )},
    # Compressed + hashed filenames, so static assets can be cached immutably and a
    # deploy cannot serve a stale mix of old and new files. The manifest is generated
    # by collectstatic during `docker build` (settings_build imports this module) and
    # ships inside the image.
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

# modeltranslation turns `name` into a virtual field resolving to `name_<lang>`.
# Rows imported outside the ORM only ever filled the BASE column, so with no
# fallback configured `.name` returned '' — the exercise library and the food
# database came back from the API with every name blank. The fallback makes a
# missing translation degrade to English instead of to an empty string.
MODELTRANSLATION_DEFAULT_LANGUAGE = 'en'
MODELTRANSLATION_FALLBACK_LANGUAGES = ('en', 'ar')

# How long AI training snapshots (which contain health context) are retained.
AI_TRAINING_RETENTION_DAYS = int(get_env('AI_TRAINING_RETENTION_DAYS', 365))
