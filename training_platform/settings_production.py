"""
settings_production.py - Production environment configuration.

Usage:
    DJANGO_SETTINGS_MODULE=training_platform.settings_production gunicorn ...

All secrets MUST be injected via environment variables or AWS Secrets Manager.
Missing secrets will crash the application at startup (fail-closed).

CRITICAL: This file contains runtime assertions that prevent deployment
with unsafe configurations.
"""

import os
import sys

# Import all shared configuration
from .settings_base import *  # noqa: F401, F403
from .settings_secrets import require_env, get_env, get_int_env, get_secret

# ========================
# PRODUCTION INVARIANTS — Crash if violated
# ========================
DEBUG = False  # Hardcoded. NEVER configurable in production.

ALLOWED_HOSTS = require_env("DJANGO_ALLOWED_HOSTS").split(",")

# ========================
# Database — credentials from environment
# ========================
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": require_env("DB_NAME"),
        "USER": require_env("DB_USER"),
        "PASSWORD": get_secret("DB_PASSWORD"),
        "HOST": require_env("DB_HOST"),
        "PORT": get_env("DB_PORT", "5432"),
        "CONN_MAX_AGE": 600,
        "OPTIONS": {
            "connect_timeout": 10,
        },
    }
}

# ========================
# Redis Cache — Segmented by logical DB (zero cross-contamination)
# ========================
_REDIS_URL_BASE = require_env("REDIS_URL")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"{_REDIS_URL_BASE}/0",  # DB0: sessions
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
            "RETRY_ON_TIMEOUT": True,
            "CONNECTION_POOL_KWARGS": {"max_connections": 50},
        },
        "KEY_PREFIX": "tp_session",
        "TIMEOUT": 300,
    },
    "ratelimit": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"{_REDIS_URL_BASE}/1",  # DB1: rate limiting
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
        "KEY_PREFIX": "tp_rl",
        "TIMEOUT": 3600,
    },
    "public": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"{_REDIS_URL_BASE}/2",  # DB2: public cache
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
        "KEY_PREFIX": "tp_pub",
        "TIMEOUT": 300,
    },
    "private": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"{_REDIS_URL_BASE}/3",  # DB3: private user cache
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
        "KEY_PREFIX": "tp_priv",
        "TIMEOUT": 300,
    },
    "edamam": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"{_REDIS_URL_BASE}/4",  # DB4: Edamam API cache
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
        "KEY_PREFIX": "edamam",
        "TIMEOUT": 86400,
    },
}

# Use Redis for session store
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# ========================
# Email — SMTP in production
# ========================
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = require_env('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = require_env('EMAIL_HOST_PASSWORD')

# ========================
# Celery — use real workers in production
# ========================
CELERY_TASK_ALWAYS_EAGER = False

# ========================
# Security Headers — Full hardening
# ========================
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_BROWSER_XSS_FILTER = True

SECURE_REDIRECT_EXEMPT = []  # No exemptions in production

# ========================
# CORS — strict origins only
# ========================
CORS_ALLOWED_ORIGINS = require_env("CORS_ALLOWED_ORIGINS").split(",")

# ========================
# CSRF
# ========================
CSRF_TRUSTED_ORIGINS = require_env("CSRF_TRUSTED_ORIGINS").split(",")

# ========================
# Throttle — strict in production
# ========================
REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] = {  # noqa: F405
    'charging': '10/minute',
}

# ========================
# WALLET_DEV_MODE — FORBIDDEN in production
# ========================
WALLET_DEV_MODE = False  # Hardcoded. No env override allowed.

# ========================
# IP Handling — Trusted proxy chain
# ========================
NUM_PROXIES = get_int_env("NUM_PROXIES", 1)
TRUSTED_PROXY_IPS = get_env("TRUSTED_PROXY_IPS", "").split(",")


# ========================
# PRODUCTION SAFETY ENFORCEMENT
# Called at wsgi/asgi startup to crash if config is unsafe.
# ========================
def enforce_production_safety():
    """
    Runtime invariant checks. Any violation crashes the process.
    This prevents silent misconfigurations from reaching production.
    """
    errors = []

    if DEBUG:
        errors.append("DEBUG is True in production")

    if os.environ.get("WALLET_DEV_MODE") == "True":
        errors.append("WALLET_DEV_MODE is True in production environment")

    # Note: AWS Secrets are strictly validated automatically during settings import via get_secret()

    if errors:
        error_msg = "PRODUCTION SAFETY CHECK FAILED:\n" + "\n".join(f"  - {e}" for e in errors)
        print(error_msg, file=sys.stderr)
        raise SystemExit(error_msg)
