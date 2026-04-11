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
    "django.middleware.security.SecurityMiddleware",
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
    'NON_FIELD_ERRORS_KEY': 'non_field_errors',
}

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
OPENAI_API_KEY = get_secret("OPENAI_API_KEY")
OPENAI_MODEL = get_env("OPENAI_MODEL", "gpt-4o-mini")
AI_PROVIDER = get_env("AI_PROVIDER", "openai")
HUGGINGFACE_API_TOKEN = get_secret("HUGGINGFACE_API_TOKEN")

AI_ASSISTANT_CONFIG = {
    "MODEL": get_env("AI_ASSISTANT_MODEL", "gpt-4o-mini"),
    "MAX_RESPONSE_TOKENS": 2000,
    "TEMPERATURE": 0.7,
    "MAX_TOOL_CALLS_PER_TURN": 5,
    "MAX_MESSAGES_PER_DAY": 50,
    "SESSION_TIMEOUT_MINUTES": 30,
    "DAILY_COST_ALERT_USD": "50.00",
    "SYSTEM_PROMPT_BUDGET": 800,
    "HISTORY_BUDGET": 3000,
    "TOOL_RESULTS_BUDGET": 2000,
    "MAX_INPUT_LENGTH": 2000,
}

# ========================
# Celery
# ========================
CELERY_BROKER_URL = get_env('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = get_env('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
CELERY_TASK_EAGER_PROPAGATES = True

CELERY_BEAT_SCHEDULE = {
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

# Edamam API — loaded from environment
EDAMAM_APP_ID = get_secret("EDAMAM_APP_ID")
EDAMAM_APP_KEY = get_secret("EDAMAM_APP_KEY")

# ========================
# Firebase / Push Notifications — loaded from environment
# ========================
FIREBASE_CREDENTIALS_PATH = get_secret("FIREBASE_CREDENTIALS_PATH")
FIREBASE_PROJECT_ID = get_env("FIREBASE_PROJECT_ID", "")

# ========================
# Internationalization & Localization
# ========================
LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
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
MEDIA_ROOT = BASE_DIR / 'media'

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
    'django_celery_results_taskresult'
])
