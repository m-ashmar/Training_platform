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
import logging

logger = logging.getLogger(__name__)

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
        # Required alongside CONN_MAX_AGE. Without it Django hands a reused connection
        # straight to the next request without testing it, so after any Postgres
        # restart, failover or idle-timeout kill every request that picks up a dead
        # connection raises InterfaceError — and keeps doing so for up to 10 minutes
        # until the pool cycles. One cheap round trip per reuse buys that back.
        "CONN_HEALTH_CHECKS": True,
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
            # Non-sensitive catalog cache: on a Redis blip, degrade to the DB
            # (miss) instead of 500-ing the request.
            "IGNORE_EXCEPTIONS": True,
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
            # External-API cache: on a Redis blip, degrade to a live lookup.
            "IGNORE_EXCEPTIONS": True,
        },
        "KEY_PREFIX": "edamam",
        "TIMEOUT": 86400,
    },
}
# NOTE (accepted posture): 'default' (sessions), 'ratelimit', and 'private' do NOT
# ignore exceptions — a Redis outage must surface for those rather than silently
# lose sessions, drop rate-limit counters, or leak private data across the miss.
# The rate-limit MIDDLEWARE currently fails OPEN on Redis errors (availability
# choice); see OD-2 in SECURITY_AUDIT_FIXES.md for the fail-closed trade-off.

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

# Fly's internal health check reaches the container over plain HTTP with no
# X-Forwarded-Proto header, so SECURE_SSL_REDIRECT would answer 301 and the check
# would never see a 200. Exempt ONLY the health path (regex, no leading slash).
SECURE_REDIRECT_EXEMPT = [r'^api/auth/health/$']

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
# Error Monitoring — Sentry (optional; enabled only when SENTRY_DSN is set)
# sentry-sdk was a declared dependency that was never initialized, so no errors
# were ever reported despite the docs claiming Sentry monitoring.
# ========================
SENTRY_DSN = get_env("SENTRY_DSN", "")
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.celery import CeleryIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration(), CeleryIntegration()],
            traces_sample_rate=float(get_env("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
            environment=get_env("SENTRY_ENVIRONMENT", "production"),
            # Never ship PII (emails, request bodies) to a third party.
            send_default_pii=False,
        )
    except ImportError:
        # Optional side effect: swallowing this silently is what made the
        # surrounding failures invisible in logs. Control flow is unchanged.
        logger.debug('suppressed non-fatal error', exc_info=True)


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

    # --- Background job infrastructure ---
    # The broker default is a localhost URL. Nothing in fly.toml or the Dockerfile ever
    # set CELERY_BROKER_URL, so production pointed at a Redis that does not exist inside
    # the container — every queued job was lost with no error anywhere. Fail at boot
    # rather than discovering it from notifications that never arrive.
    _broker = os.environ.get("CELERY_BROKER_URL", "")
    if not _broker:
        errors.append("CELERY_BROKER_URL is not set — background jobs would be lost")
    elif "localhost" in _broker or "127.0.0.1" in _broker:
        errors.append(
            f"CELERY_BROKER_URL points at localhost ({_broker!r}); "
            "there is no broker inside the app container"
        )
    elif _broker.rstrip('/').endswith(('/0', '/1', '/2', '/3', '/4', '/5')):
        # DB0-DB5 are the six segmented caches (sessions, ratelimit, public, private,
        # edamam, channels). The broker must not share a keyspace with them.
        errors.append(
            f"CELERY_BROKER_URL uses a Redis DB reserved for caches ({_broker!r}); "
            "use DB6 or higher"
        )

    # --- Payment gateway (ShamCash) production readiness ---
    # PAYMENT_DEBUG must never be on in production.
    if os.environ.get("PAYMENT_DEBUG", "False").lower() == "true":
        errors.append("PAYMENT_DEBUG is True in production environment")
    # When ShamCash is configured (token present), enforce full correctness.
    # If unconfigured (pre-approval), payments are simply disabled and boot proceeds.
    _shamcash_token = os.environ.get("SHAMCASH_API_TOKEN", "")
    if _shamcash_token:
        if os.environ.get("GATEWAY_MODE", "production") != "production":
            errors.append("GATEWAY_MODE must be 'production' when ShamCash is configured")
        if not os.environ.get("SHAMCASH_ACCOUNT_ID"):
            errors.append("SHAMCASH_ACCOUNT_ID is required when ShamCash is configured")
        _wh = os.environ.get("SHAMCASH_WEBHOOK_SECRET", "")
        if _wh and len(_wh) < 16:
            errors.append("SHAMCASH_WEBHOOK_SECRET is too short (min 16 chars)")

    # --- Origin hygiene: no wildcard/localhost in production allowlists ---
    for _name, _vals in (
        ("ALLOWED_HOSTS", ALLOWED_HOSTS),
        ("CORS_ALLOWED_ORIGINS", CORS_ALLOWED_ORIGINS),
        ("CSRF_TRUSTED_ORIGINS", CSRF_TRUSTED_ORIGINS),
    ):
        _bad = [v for v in (_vals or []) if v and (('*' in v) or ('localhost' in v) or ('127.0.0.1' in v))]
        if _bad:
            errors.append(f"{_name} contains wildcard/localhost entries in production: {_bad}")

    # --- Field encryption key ---
    # Without it EncryptedTextField writes health data in plain text, and it does so
    # silently — the failure is invisible until someone reads a database dump.
    _fek = (globals().get("FIELD_ENCRYPTION_KEY") or "").strip()
    if not _fek:
        errors.append(
            "FIELD_ENCRYPTION_KEY is not set — health data would be stored unencrypted"
        )
    else:
        try:
            from cryptography.fernet import Fernet
            for _k in [k.strip() for k in _fek.split(",") if k.strip()]:
                Fernet(_k.encode("utf-8"))
        except Exception:
            errors.append("FIELD_ENCRYPTION_KEY is not a valid Fernet key")

    # --- SECRET_KEY strength ---
    if ('django-insecure' in (SECRET_KEY or '')) or (len(SECRET_KEY or '') < 32):
        errors.append("SECRET_KEY is weak or a Django dev placeholder in production")

    # Note: AWS Secrets are strictly validated automatically during settings import via get_secret()

    if errors:
        error_msg = "PRODUCTION SAFETY CHECK FAILED:\n" + "\n".join(f"  - {e}" for e in errors)
        print(error_msg, file=sys.stderr)
        raise SystemExit(error_msg)
