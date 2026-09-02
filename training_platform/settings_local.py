"""
settings_local.py - Local development environment overrides.

Usage:
    DJANGO_SETTINGS_MODULE=training_platform.settings_local python manage.py runserver

Required environment variables (set in .env or shell):
    DJANGO_SECRET_KEY       - Any string for local dev
    JWT_SIGNING_KEY         - Any string for local dev
    OPENAI_API_KEY          - Your OpenAI API key
    HUGGINGFACE_API_TOKEN   - Your HuggingFace token
    EDAMAM_APP_ID           - Your Edamam App ID
    EDAMAM_APP_KEY          - Your Edamam App Key
    FIREBASE_CREDENTIALS_PATH - Path to Firebase service account JSON
"""

# Load .env file for local development convenience
from pathlib import Path
import os

_BASE_DIR = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(_BASE_DIR / '.env')
except ImportError:
    # Optional side effect: swallowing this silently is what made the
    # surrounding failures invisible in logs. Control flow is unchanged.
    logger.debug('suppressed non-fatal error', exc_info=True)

# Import all shared configuration
from .settings_base import *  # noqa: F401, F403
import logging

logger = logging.getLogger(__name__)

# ========================
# Local Overrides
# ========================
DEBUG = True
ALLOWED_HOSTS = ['*']

# Local database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DB_NAME", "training_platform"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "postgres"),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5433"),
    }
}

# Local cache - named backends mirror production DB segmentation
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-default",
    },
    "ratelimit": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-ratelimit",
    },
    "public": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-public",
    },
    "private": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-private",
    },
    "edamam": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-edamam",
    },
}

# Celery runs synchronously in local dev
CELERY_TASK_ALWAYS_EAGER = True

# Email — console backend for local dev
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# CORS — relaxed for local dev
CORS_ALLOWED_ORIGINS = [
    'http://localhost:3000',
    'http://127.0.0.1:3000',
]

# CSRF
CSRF_TRUSTED_ORIGINS = [
    'http://0.0.0.0:8000',
    'http://192.168.1.107',
    'http://localhost',
]

# Throttle rates — relaxed for local dev
REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] = {  # noqa: F405
    'charging': '1000/second',
}

# ========================
# WALLET_DEV_MODE — Local only, NEVER in production
# Must be explicitly set in environment. No default.
# ========================
from .settings_secrets import get_bool_env_optional
WALLET_DEV_MODE = get_bool_env_optional("WALLET_DEV_MODE", default=False)

# Development-only field-encryption key, so the encrypted-column code path is actually
# exercised locally and by the test suite instead of silently falling back to plaintext.
# Production loads its own from the environment and refuses to boot without one; this
# value guards nothing real and is not a secret.
FIELD_ENCRYPTION_KEY = os.environ.get("FIELD_ENCRYPTION_KEY", "07DLC_JTJpDiu578mrD6xH4t4Hz_0FgKnKYEhUdCsz0=")
