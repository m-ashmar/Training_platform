"""
Django settings for training_platform project

Optimized Configuration (v3.2)
- Clean section organization
- Added missing AI Chef parameters
- Preserved all original values
- Enhanced security headers
"""

from pathlib import Path
import os
import rest_framework
from datetime import timedelta

# ========================
# Path Configuration
# ========================
BASE_DIR = Path(__file__).resolve().parent.parent

# ========================
# Security Configuration
# ========================
SECRET_KEY = "django-insecure-l^ub=e^ee47%mee&u9vt#u##q5%1^=6=iy43kd45z+4jddxlzq"
DEBUG = True
ALLOWED_HOSTS = ['*']

CSRF_TRUSTED_ORIGINS = [
    'http://0.0.0.0:8000',
    'http://192.168.1.107',
    'http://localhost'
]

CSRF_EXEMPT_URLS = [
    r'^auth/.*$',
    r'^api/.*$',
]
# ========================
# Application Definition
# ========================
INSTALLED_APPS = [
    # Django Core
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    
    # Project Apps
    "drf_yasg",
    "users",
    "routine",
    "diet",
    "subscription",
    "challenges",
    "analytics",
    "social",
    "admin_dashboard",  # New comprehensive admin dashboard
    "wallet",
    
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
    "channels",  # Added for WebSocket support
]

ASGI_APPLICATION = "training_platform.asgi.application"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [(os.getenv("REDIS_HOST", "127.0.0.1"), int(os.getenv("REDIS_PORT", "6379")))],
        },
    },
}

MIDDLEWARE = [
    # Security & Core
    "training_platform.middleware.SecurityHeadersMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    
    # Auth
    "allauth.account.middleware.AccountMiddleware",
    
    # Custom Security & Performance
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
            ],
        },
    },
]

# ========================
# Database
# ========================
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

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

# Email
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Allauth
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_AUTHENTICATION_METHOD = 'email'
ACCOUNT_USERNAME_REQUIRED = False

# REST Auth
REST_AUTH_SERIALIZERS = {
    'REGISTER_SERIALIZER': 'users.serializers.CustomRegisterSerializer',
    'LOGIN_SERIALIZER': 'users.serializers.CustomLoginSerializer',
}

# ========================
# REST Framework
# ========================
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_RATES': {
        # Relax throttling in development
        'charging': '1000/second' if os.getenv('WALLET_DEV_MODE', 'False') == 'True' or os.getenv('DJANGO_DEBUG', 'True') == 'True' or DEBUG else '10/minute',
    },
}

# ========================
# JWT Configuration
# ========================
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'VERIFYING_KEY': None,
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
    'http://localhost:3000',  # Updated default React port
    'https://yourfrontend.com',
]

# ========================
# AI Integration
# ========================
# OpenAI
OPENAI_API_KEY = "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"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-nano")
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai")

# Celery
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')

# ========================
# Nutrition & AI Chef
# ========================
AI_CHEF_CONFIG = {
    # Generation Parameters
    "MAX_RETRIES": 2,
    "TEMPERATURE": 0.3,
    
    # Nutrition Validation
    "MACRO_TOLERANCE": {
        "calories": 0.1,
        "protein": 0.15,
        "carbs": 0.2,
        "fat": 0.25
    },
    
    # Portion Safety
    "PORTION_GUARDRAILS": {
        "protein": (50, 350),
        "carb": (100, 400),
        "fat": (20, 100),
        "per_meal": {
            "protein": (20, 150),
            "carb": (30, 200),
            "fat": (5, 30)
        }
    },
    
    # Edamam
    "EDAMAM": {
        "MAX_RESULTS": 8,
        "CACHE_TTL": 86400
    }
}

# Feature flags
DIET_SMART_MACRO_PLANNER = True
DIET_DYNAMIC_MEAL_ALLOCATION = True
DIET_STAGED_MEAL_FILL = True

# Edamam API
EDAMAM_APP_ID = "291502d0"
EDAMAM_APP_KEY = "3d9ebc3df89fa042bb6ca7088d532265"

# ========================
# Internationalization
# ========================
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ========================
# Static Files
# ========================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

# ========================
# Media Files
# ========================
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# ========================
# Default Auto Field
# ========================
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"







CACHES = {
    "default": {
        "BACKEND": os.getenv('DJANGO_CACHE_BACKEND', "django.core.cache.backends.locmem.LocMemCache"),
        "LOCATION": os.getenv('DJANGO_CACHE_LOCATION', "unique-default"),
    },
    "edamam": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-edamam",
    },
}

# ========================
# Logging Configuration
# ========================
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
            'maxBytes': 1024 * 1024 * 5,  # 5 MB
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

# Create logs directory if it doesn't exist
os.makedirs(BASE_DIR / 'logs', exist_ok=True)

# Hugging Face API token for LLM access (move to environment variable in production!)
HUGGINGFACE_API_TOKEN = "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"

# --- Production hardening toggles (enable in prod via env) ---
SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'False') == 'True'
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'False') == 'True'
CSRF_COOKIE_SECURE = os.getenv('CSRF_COOKIE_SECURE', 'False') == 'True'
SESSION_COOKIE_SAMESITE = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax')
CSRF_COOKIE_SAMESITE = os.getenv('CSRF_COOKIE_SAMESITE', 'Lax')
SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv('SECURE_HSTS_INCLUDE_SUBDOMAINS', 'False') == 'True'
SECURE_HSTS_PRELOAD = os.getenv('SECURE_HSTS_PRELOAD', 'False') == 'True'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if os.getenv('USE_PROXY_SSL_HEADER', 'False') == 'True' else None
SECURE_REDIRECT_EXEMPT = [r'^api/.*', r'^admin/.*', r'^swagger/.*', r'^redoc/.*']

# Platform escrow for wallet flows
PLATFORM_ESCROW_EMAIL = os.getenv('PLATFORM_ESCROW_EMAIL', 'platform_escrow@local')
PLATFORM_ESCROW_USERNAME = os.getenv('PLATFORM_ESCROW_USERNAME', 'platform_escrow')

# Wallet development mode toggle (relaxes HMAC/IP/timestamp checks for faster local testing)
WALLET_DEV_MODE = os.getenv('WALLET_DEV_MODE', 'True' if DEBUG else 'False') == 'True'