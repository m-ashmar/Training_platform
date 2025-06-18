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

# ========================
# Path Configuration
# ========================
BASE_DIR = Path(__file__).resolve().parent.parent

# ========================
# Security Configuration
# ========================
SECRET_KEY = "django-insecure-l^ub=e^ee47%mee&u9vt#u##q5%1^=6=iy43kd45z+4jddxlzq"
DEBUG = True
ALLOWED_HOSTS = ['192.168.1.109', 'localhost', '127.0.0.1']

CSRF_TRUSTED_ORIGINS = [
    'http://0.0.0.0:8000',
    'http://192.168.1.111',
    'http://localhost'
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
    "users",
    "routine",
    "diet",
    "subscription",
    
    # Third-party
    "rest_framework",
    "rest_framework.authtoken",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "dj_rest_auth",
    "corsheaders"
]

MIDDLEWARE = [
    # Security & Core
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
    
    # Custom
    # "training_platform.utils.LoggingMiddleware",  # Removed for now
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
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ]
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

# Celery
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'

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
STATIC_URL = "static/"

# ========================
# Default Auto Field
# ========================
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"









CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-default",
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