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

# The key AI training copies are pseudonymised with (`settings_base`, `ai_assistant/
# anonymise.py`). Required, from the secret store or the environment like every other
# secret: without it no copy could be keyed, and none could be found again to be deleted.
AI_TRAINING_PSEUDONYM_KEY = get_secret("AI_TRAINING_PSEUDONYM_KEY")

# ========================
# Database — credentials from environment
# ========================
# Connections are NOT kept between requests by default (CONN_MAX_AGE 0).
#
# The web process is Daphne (ASGI). Django runs each request's sync code on a thread of
# that request's own, and a database connection belongs to the thread that opened it —
# so a connection kept for CONN_MAX_AGE is never handed to the next request: it idles,
# still counted against Postgres's max_connections, until the garbage collector frees
# the dead thread's objects. The old 600 s turned every burst of traffic into a pile of
# idle connections exactly when new ones were needed (Django's async docs say to disable
# persistent connections under ASGI and pool outside Django; the audit's deploy
# finding 17 and quality finding 6). With 0 a connection is opened on a request's first
# query and closed when it ends, so the connections in use are the requests in flight.
#
# DB_POOLER declares a pooler between the app and Postgres (PgBouncer, a provider's
# pooled endpoint): "transaction" or "session". Only then may DB_CONN_MAX_AGE keep
# connections, because what idles is a cheap client connection to the pooler, not a
# Postgres backend; the boot check refuses a kept connection without one. Under a
# TRANSACTION pooler each transaction may get a different server connection, and a
# server-side cursor (`.iterator()`) lives on the one that opened it, so those are
# switched off; and the database's own timezone must be UTC, or Django issues a
# session-level SET TIME ZONE that the pooler hands on to other clients (Django's docs,
# "Transaction pooling and server-side cursors"). DB_SSLMODE is libpq's sslmode
# ("require" for a managed Postgres reached over the internet); unset leaves libpq's
# default.
DB_POOLER = get_env("DB_POOLER", "").strip().lower()
DB_CONN_MAX_AGE = get_int_env("DB_CONN_MAX_AGE", 0)
DB_SSLMODE = get_env("DB_SSLMODE", "").strip()
DB_POOLERS = ("transaction", "session")
DB_SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": require_env("DB_NAME"),
        "USER": require_env("DB_USER"),
        "PASSWORD": get_secret("DB_PASSWORD"),
        "HOST": require_env("DB_HOST"),
        "PORT": get_env("DB_PORT", "5432"),
        "CONN_MAX_AGE": DB_CONN_MAX_AGE,
        # Kept whatever CONN_MAX_AGE is. With connections kept it tests one before
        # reuse, so a Postgres restart, failover or idle-timeout kill costs one round
        # trip instead of an InterfaceError on every request that picks up the dead one.
        "CONN_HEALTH_CHECKS": True,
        "DISABLE_SERVER_SIDE_CURSORS": DB_POOLER == "transaction",
        "OPTIONS": {
            "connect_timeout": 10,
            **({"sslmode": DB_SSLMODE} if DB_SSLMODE else {}),
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
# Keep the root logger configured below. By default a worker replaces root's handlers
# with its own multi-line format, so the worker's logs — the payment poller's among
# them — would be the one stream not in one-line JSON.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

# ========================
# Logging — one JSON line per record, on stdout, and nowhere else
# ========================
# A machine's filesystem is gone when it stops, so the rotating files under logs/ that
# settings_base keeps for development lost every production log with it; the only
# stdout handler was DEBUG-only; wallet, payouts, policy and chat had no configuration at
# all and reached stderr only at WARNING, bare. Here every logger reaches the root
# handler, which writes one JSON object per record (training_platform/log_format.py) to
# stdout, where the platform's log stream picks it up. No mail_admins: it sent nothing
# (ADMINS is set nowhere) and, if set, would send synchronously through SMTP inside the
# failing request. Errors are alerted by Sentry when SENTRY_DSN is set.
LOG_LEVEL = get_env("LOG_LEVEL", "INFO").strip().upper()
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL, got {LOG_LEVEL!r}")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json_line": {"()": "training_platform.log_format.OneLineJsonFormatter"},
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json_line",
        },
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        # Every app logger ('diet', 'wallet', 'subscription.tasks', ...) and Django's own
        # propagate here with no handler of their own. Only levels are set below.
        "django": {"level": LOG_LEVEL},
        # SQL at DEBUG would be every query of every request.
        "django.db.backends": {"level": "WARNING"},
        # Celery gives its task logger a stderr handler of its own unless it already
        # has one; giving it this one keeps task logs in the same format, once.
        "celery.task": {"handlers": ["stdout"], "level": LOG_LEVEL, "propagate": False},
        # The same for the prefork pool's own logger (billiard's "multiprocessing").
        "multiprocessing": {"handlers": ["stdout"], "level": "WARNING", "propagate": False},
    },
}

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
# UPDATE the base rates, never replace them. Assigning a new dict here silently dropped
# every scope settings_base declares, so a view throttled on a base scope raised in this
# environment for a reason that pointed nowhere near this file.
REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] = {  # noqa: F405
    **REST_FRAMEWORK.get('DEFAULT_THROTTLE_RATES', {}),  # noqa: F405
    'charging': '10/minute',
}

# ========================
# WALLET_DEV_MODE — FORBIDDEN in production
# ========================
WALLET_DEV_MODE = False  # Hardcoded. No env override allowed.

# ========================
# IP Handling — Trusted proxy chain
# ========================
# How many proxies of OURS append to X-Forwarded-For before a request reaches Daphne.
# REMOTE_ADDR is the last of them, never the client. Fly's edge proxy is one, so the
# client is the right-most entry. Put a CDN or load balancer in front and this becomes
# 2. Every client IP the app records or checks (the rate limiter, agent allow-lists, the
# wallet audit log, analytics) goes through `training_platform.middleware.
# get_trusted_client_ip`, which reads it.
NUM_PROXIES = get_int_env("NUM_PROXIES", 1)
TRUSTED_PROXY_IPS = get_env("TRUSTED_PROXY_IPS", "").split(",")

# ========================
# Error Monitoring — Sentry (optional; enabled only when SENTRY_DSN is set)
# ========================
# Errors, plus a cron monitor for each beat job in BACKGROUND_WATCHED_JOBS (the money
# jobs): see training_platform/observability.py. Optional, but not silently: the boot
# check logs a WARNING when SENTRY_DSN is unset, and REFUSES to boot when it is set and
# Sentry could not start — an operator who set a DSN believes errors are being tracked.
# (A failed start used to be swallowed at DEBUG, which production never logs.)
SENTRY_DSN = get_env("SENTRY_DSN", "").strip()
SENTRY_STARTED = False
SENTRY_START_ERROR = ""
if SENTRY_DSN:
    try:
        from .observability import start_sentry

        start_sentry(
            SENTRY_DSN,
            beat_schedule=CELERY_BEAT_SCHEDULE,  # noqa: F405
            monitored=set(BACKGROUND_WATCHED_JOBS),  # noqa: F405
            environment=get_env("SENTRY_ENVIRONMENT", "production"),
            traces_sample_rate=float(get_env("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
        )
        SENTRY_STARTED = True
    except Exception as exc:  # BadDsn, ImportError, a bad sample rate
        SENTRY_START_ERROR = f"{type(exc).__name__}: {exc}"


# ========================
# PRODUCTION SAFETY ENFORCEMENT
# Called at wsgi/asgi startup to crash if config is unsafe.
# ========================
def _gcs_key_problem(name):
    """Why the GCS signing key named `name` is unusable, or None when it is fine.

    Read the way `training_platform.gcs_storage._credentials` reads it
    (`settings_secrets.get_secret`: the secret store when `AWS_SECRET_NAME` is set, the
    environment otherwise), so the boot check and the storage can never disagree about
    where the key lives. Usable means: present, a JSON object, and carrying the two
    fields a V4 signature is made from, `private_key` and `client_email`.
    """
    import json

    from django.core.exceptions import ImproperlyConfigured

    from .settings_secrets import get_secret

    if not os.environ.get(name) and not os.environ.get("AWS_SECRET_NAME"):
        return (f"{name} is not set and there is no secret store to read it from "
                "— media URLs cannot be signed")
    try:
        raw = get_secret(name)
    except ImproperlyConfigured as exc:
        return f"{name} could not be read — media URLs cannot be signed: {exc}"
    try:
        info = json.loads(raw)
    except ValueError:
        return f"{name} is not valid JSON — it must be the service account key file"
    if not isinstance(info, dict) or not info.get("private_key") \
            or not info.get("client_email"):
        return (f"{name} holds no service account private_key/client_email — "
                "media URLs cannot be signed")
    return None


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

    # --- WebSocket group layer ---
    # Same failure shape as the broker: the channel layer used to be assembled from
    # REDIS_HOST/REDIS_PORT, which nothing in the deploy sets, so it defaulted to a
    # localhost Redis that does not exist in the container. It now comes from REDIS_URL
    # like every cache; this refuses to boot if something puts it back on localhost.
    try:
        _channel_hosts = CHANNEL_LAYERS["default"]["CONFIG"]["hosts"]  # noqa: F405
    except (KeyError, TypeError):
        _channel_hosts = []
    for _host in _channel_hosts:
        if isinstance(_host, str) and ("localhost" in _host or "127.0.0.1" in _host):
            errors.append(
                f"CHANNEL_LAYERS points at localhost ({_host!r}); "
                "there is no Redis inside the app container — WebSockets would be dead"
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

    # --- Object storage ---
    # 'local' is the filesystem backend, and it cannot be production. A machine's own
    # filesystem is lost when it stops, and a volume attaches to ONE machine while the
    # web group runs several, so a picture uploaded through machine A is a 404 through
    # machine B; video does not fit on one at all. fly.toml mounts no volume. The
    # deploy sets MEDIA_BACKEND=gcs (the owner, 2026-09-19). Refused at boot rather than
    # discovered from uploads that vanish.
    _media_backend = (globals().get("MEDIA_BACKEND") or "local").lower()
    if _media_backend != "gcs":
        errors.append(
            f"MEDIA_BACKEND is {_media_backend!r} — production must use object storage "
            "('gcs'); the local filesystem backend loses every upload"
        )
    else:
        if not globals().get("GS_BUCKET_NAME"):
            errors.append("GS_BUCKET_NAME is not set — MEDIA_BACKEND=gcs cannot store anything")
        # Signing a V4 URL needs the service account's private key. Without it every
        # media URL raises at serialization time, which is a 500 on screens that work
        # perfectly in development.
        #
        # The key is READ here, from wherever `gcs_storage` will read it, and checked for
        # what signing needs. Accepting "AWS_SECRET_NAME is set" as proof booted green a
        # deploy whose store did not hold the key at all: the storage then fell back to
        # ambient credentials that cannot sign, and every screen with media answered 500
        # (SIDE_BUGS S-148).
        _cred_name = globals().get("GS_CREDENTIALS_SECRET_NAME", "GS_CREDENTIALS_JSON")
        _cred_problem = _gcs_key_problem(_cred_name)
        if _cred_problem:
            errors.append(_cred_problem)
        # Identity documents (trainer_verification/). With uniform bucket-level access a
        # public grant covers the WHOLE bucket, so a media bucket that serves the app's
        # own pictures publicly would serve every trainer's identity card too. They need
        # a bucket of their own in that configuration.
        _docs_bucket = (globals().get("VERIFICATION_GS_BUCKET_NAME")
                        or globals().get("GS_BUCKET_NAME"))
        if (globals().get("MEDIA_PUBLIC_APP_CONTENT")
                and _docs_bucket == globals().get("GS_BUCKET_NAME")):
            errors.append(
                "MEDIA_PUBLIC_APP_CONTENT makes the media bucket public, and trainer "
                "identity documents would be stored in it — set "
                "VERIFICATION_GS_BUCKET_NAME to a private bucket")

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

    # --- AI training pseudonym key ---
    # A short key is guessable, and one shared with SECRET_KEY rotates with it: every
    # training copy would then be orphaned, and "help us improve" off could not delete it.
    _aik = (globals().get("AI_TRAINING_PSEUDONYM_KEY") or "").strip()
    if len(_aik) < 32:
        errors.append("AI_TRAINING_PSEUDONYM_KEY is too short (min 32 chars)")
    elif _aik == (SECRET_KEY or ""):
        errors.append("AI_TRAINING_PSEUDONYM_KEY must not be SECRET_KEY")

    # --- SECRET_KEY strength ---
    if ('django-insecure' in (SECRET_KEY or '')) or (len(SECRET_KEY or '') < 32):
        errors.append("SECRET_KEY is weak or a Django dev placeholder in production")

    # --- Database connections (see DATABASES above for the reasoning) ---
    if DB_POOLER and DB_POOLER not in DB_POOLERS:
        errors.append(f"DB_POOLER is {DB_POOLER!r} — it must be one of "
                      f"{', '.join(DB_POOLERS)}, or unset when the app talks to Postgres "
                      "directly")
    if DB_CONN_MAX_AGE < 0:
        errors.append("DB_CONN_MAX_AGE cannot be negative")
    elif DB_CONN_MAX_AGE > 0 and not DB_POOLER:
        errors.append(
            f"DB_CONN_MAX_AGE is {DB_CONN_MAX_AGE} with no DB_POOLER: under Daphne each "
            "request runs on a thread of its own, so a kept connection is never reused "
            "and idles against Postgres's max_connections. Leave DB_CONN_MAX_AGE at 0, or "
            "set DB_POOLER to the pooler (transaction|session) that fronts Postgres")
    if DB_SSLMODE and DB_SSLMODE not in DB_SSLMODES:
        errors.append(f"DB_SSLMODE is {DB_SSLMODE!r} — libpq accepts "
                      f"{', '.join(DB_SSLMODES)}")

    # --- Error tracking ---
    if SENTRY_DSN and not SENTRY_STARTED:
        errors.append(f"SENTRY_DSN is set but Sentry could not start ({SENTRY_START_ERROR}) "
                      "— fix the DSN, or unset it to run without error tracking")
    if not SENTRY_DSN:
        # Optional, so a warning rather than a refusal — but loud, once per process: the
        # only other error path is this log stream, which alerts nobody.
        logging.getLogger("training_platform.boot").warning(
            "SENTRY_DSN is not set: errors go only to the log stream and NOTHING WILL "
            "ALERT on a failing payment, OTP email or background job, and the money jobs "
            "have no cron monitor. Set SENTRY_DSN (training_platform/observability.py).")

    # Note: AWS Secrets are strictly validated automatically during settings import via get_secret()

    if errors:
        error_msg = "PRODUCTION SAFETY CHECK FAILED:\n" + "\n".join(f"  - {e}" for e in errors)
        print(error_msg, file=sys.stderr)
        raise SystemExit(error_msg)
