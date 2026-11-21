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
from .settings_secrets import (require_env, get_env, get_int_env, get_secret,
                               get_bool_env_optional)
from . import redis_layout
from django.core.exceptions import ImproperlyConfigured
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
# Redis — a URL per role, each defaulting to its database of REDIS_URL
# ========================
# The layout, what the boot check refuses and why the broker's Redis must be
# `noeviction`: training_platform/redis_layout.py. In short:
#   REDIS_CACHE_URL        the five caches below   (default REDIS_URL/0 ... /4)
#   REDIS_CHANNELS_URL     the channel layer       (default REDIS_URL/5)
#   CELERY_BROKER_URL      the job queue           (default REDIS_URL/6)
#   CELERY_RESULT_BACKEND  Celery results          (default REDIS_URL/7)
# REDIS_URL is required only while some role has no URL of its own.
_REDIS_URL_BASE = get_env("REDIS_URL", "").strip()


def _redis_url_for(variable, database):
    """The role's own URL when `variable` is set, else `database` of REDIS_URL.

    Refuses rather than guesses: with neither set there is nothing to connect to (the old
    defaults were localhost, a Redis that does not exist in the container), and a
    REDIS_URL that already names a database would be silently overridden by every role.
    """
    explicit = get_env(variable, "").strip()
    if explicit:
        return explicit
    if not _REDIS_URL_BASE:
        raise ImproperlyConfigured(
            f"{variable} is not set, and there is no REDIS_URL to take it from "
            f"(REDIS_URL/{database}). Set REDIS_URL for one Redis with a database per "
            f"role, or {variable} and the other role URLs for a Redis per role — see "
            "training_platform/redis_layout.py.")
    if redis_layout.names_database(_REDIS_URL_BASE):
        raise ImproperlyConfigured(
            f"REDIS_URL names a database ({redis_layout.describe(_REDIS_URL_BASE)}); it "
            "must be the server only, because each role adds its own: caches /0-/4, "
            "channels /5, broker /6, results /7. To put a role on a particular "
            "database, give it its own URL (REDIS_CACHE_URL, REDIS_CHANNELS_URL, "
            "CELERY_BROKER_URL, CELERY_RESULT_BACKEND).")
    return redis_layout.with_database(_REDIS_URL_BASE, database)


CHANNEL_LAYERS = {  # a new dict: settings_base's is shared by every settings module
    "default": {
        **CHANNEL_LAYERS["default"],  # noqa: F405
        "CONFIG": {**CHANNEL_LAYERS["default"]["CONFIG"],  # noqa: F405
                   "hosts": [_redis_url_for("REDIS_CHANNELS_URL", 5)]},
    },
}
CELERY_BROKER_URL = _redis_url_for("CELERY_BROKER_URL", 6)
CELERY_RESULT_BACKEND = _redis_url_for("CELERY_RESULT_BACKEND", 7)

# One knob for every cache: with REDIS_CACHE_URL set, all five share the database it
# names (a managed Redis that serves only database 0), kept apart by KEY_PREFIX.
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": _redis_url_for("REDIS_CACHE_URL", 0),  # DB0: sessions
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
        "LOCATION": _redis_url_for("REDIS_CACHE_URL", 1),  # DB1: rate limiting
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
        "KEY_PREFIX": "tp_rl",
        "TIMEOUT": 3600,
    },
    "public": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": _redis_url_for("REDIS_CACHE_URL", 2),  # DB2: public cache
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
        "LOCATION": _redis_url_for("REDIS_CACHE_URL", 3),  # DB3: private user cache
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
        "KEY_PREFIX": "tp_priv",
        "TIMEOUT": 300,
    },
    "edamam": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": _redis_url_for("REDIS_CACHE_URL", 4),  # DB4: Edamam API cache
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
# Email — SMTP, any provider, every part from the environment
# ========================
# Every transactional provider speaks SMTP (SES, Postmark, SendGrid, Mailgun, Brevo,
# Resend, a Workspace mailbox), so the provider is four variables, not code. This used
# to hard-code Gmail: a personal mailbox caps sending at a few hundred a day, and mail
# from a datacenter IP with a From that does not match the account is rewritten or
# junked — while the sign-up code is the only way to open an account.
#
#   EMAIL_HOST            the provider's SMTP host                            required
#   EMAIL_PORT            587 (STARTTLS), 465 with EMAIL_USE_SSL, or 2525/25  587
#   EMAIL_USE_TLS / _SSL  'True' / 'False'; STARTTLS unless EMAIL_USE_SSL=True
#   EMAIL_HOST_USER       the SMTP user (an address, or a key id like 'apikey') required
#   EMAIL_HOST_PASSWORD   the SMTP password / key (secret store or environment) required
#   EMAIL_TIMEOUT         seconds a stalled connection may take                 10
#   DEFAULT_FROM_EMAIL    'Yalla Gym <no-reply@your-domain>', on a domain the
#                         provider has verified (SPF and DKIM)                  required
#   SERVER_EMAIL          From of the error mails to ADMINS          DEFAULT_FROM_EMAIL
#
# The combinations that cannot work are refused at boot (enforce_production_safety).
# The account emails are sent by the worker (users/tasks.py), not the request.
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = require_env('EMAIL_HOST')
EMAIL_USE_SSL = get_bool_env_optional('EMAIL_USE_SSL', default=False)
EMAIL_USE_TLS = get_bool_env_optional('EMAIL_USE_TLS', default=not EMAIL_USE_SSL)
EMAIL_PORT = get_int_env('EMAIL_PORT', 465 if EMAIL_USE_SSL else 587)
EMAIL_HOST_USER = require_env('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = get_secret('EMAIL_HOST_PASSWORD')
EMAIL_TIMEOUT = get_int_env('EMAIL_TIMEOUT', 10)
DEFAULT_FROM_EMAIL = require_env('DEFAULT_FROM_EMAIL')
SERVER_EMAIL = get_env('SERVER_EMAIL', '').strip() or DEFAULT_FROM_EMAIL

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


def _s3_key_problem(name):
    """Why one half of the S3 key pair is unusable, or None when it is fine.

    Read the way `training_platform.s3_storage._key` reads it, for the reason given on
    `_gcs_key_problem`: the boot check and the storage must never disagree about where
    a secret lives. There is no shape to check beyond "present and not blank": the pair
    is two opaque strings, and whether the store accepts them is a network question
    that `manage.py check_media_storage` answers after the deploy.
    """
    from django.core.exceptions import ImproperlyConfigured

    from .settings_secrets import get_secret

    if not os.environ.get(name) and not os.environ.get("AWS_SECRET_NAME"):
        return (f"{name} is not set and there is no secret store to read it from "
                "— MEDIA_BACKEND=s3 can neither store a file nor sign a URL")
    try:
        value = get_secret(name)
    except ImproperlyConfigured as exc:
        return f"{name} could not be read — media cannot be stored or signed: {exc}"
    if not str(value or "").strip():
        return f"{name} is blank — media cannot be stored or signed"
    return None


#: Domains no provider will send for: the development placeholder settings_base ships
#: (`noreply@trainingplatform.com` — the From every email had until 2026-09-27) and the
#: names reserved for documentation (RFC 2606).
_PLACEHOLDER_MAIL_DOMAINS = {"trainingplatform.com", "example.com", "example.net",
                             "example.org", "localhost"}


def _from_address_problem(value):
    """Why `value` cannot be the From of the platform's email, or None when it can.

    A display name is welcome (`Yalla Gym <no-reply@yalagym.com>`). The domain must be
    one the provider has verified — nothing here can check that, but a placeholder or a
    malformed address is certainly not, and the provider would refuse or junk every
    sign-up code sent from it.
    """
    from email.utils import parseaddr

    if not value or "\n" in value or "\r" in value:
        return "is empty or spans lines"
    _name, address = parseaddr(value)
    local, _at, domain = address.rpartition("@")
    domain = domain.lower().rstrip(".")
    if not local or "." not in domain:
        return (f"is not an email address ({value!r}); write it as "
                "'Yalla Gym <no-reply@your-domain>'")
    if domain in _PLACEHOLDER_MAIL_DOMAINS or domain.endswith((".example", ".invalid",
                                                                ".localhost")):
        return (f"is on a placeholder domain ({domain}); use an address on a domain "
                "your email provider has verified (SPF and DKIM)")
    return None


def heavy_work_problems(memory=None) -> list:
    """What is unsafe about how much this process may do at once. Empty when nothing.

    * Image decodes (`training_platform/image_budget.py`): each may hold
      DECODE_BUDGET_BYTES, so the slots, times that, beside IMAGE_DECODE_RESERVED_BYTES,
      must fit in the machine's memory — IMAGE_DECODE_MEMORY_BYTES when set, else what
      the process can read (its cgroup limit, else its RAM). A count the memory cannot
      hold is refused rather than trusted, and so is a machine too small for even one.
    * Periodic pages (`training_platform/batching.py`): a page must leave the row in
      progress room to finish, so it may spend at most half of
      CELERY_TASK_SOFT_TIME_LIMIT.
    * Privacy pages and the export spool (`training_platform/privacy/registry.py`): a
      page of at least one row, and a spool that stays small — past 64 MB in memory the
      export is not bounded any more.
    """
    from training_platform import image_budget

    g = globals()
    mb = 1024 * 1024
    problems = []

    slots = g.get("IMAGE_DECODE_SLOTS")
    reserved = int(g.get("IMAGE_DECODE_RESERVED_BYTES") or image_budget.DEFAULT_RESERVED_BYTES)
    budget = image_budget.DECODE_BUDGET_BYTES
    memory = memory or g.get("IMAGE_DECODE_MEMORY_BYTES") or image_budget.machine_memory_bytes()
    if slots is not None and int(slots) < 1:
        problems.append(f"IMAGE_DECODE_SLOTS is {slots}; it must be at least 1")
    if reserved < 0:
        problems.append("IMAGE_DECODE_RESERVED_BYTES must not be negative")
    if memory:
        if memory < reserved + budget:
            problems.append(
                f"the machine has {memory // mb} MB, and one image decode needs "
                f"{budget // mb} MB beside the {reserved // mb} MB the process keeps "
                "(IMAGE_DECODE_RESERVED_BYTES): give the machine more memory")
        elif slots is not None and int(slots) >= 1 \
                and int(slots) * budget + reserved > memory:
            problems.append(
                f"IMAGE_DECODE_SLOTS={slots} lets {int(slots) * budget // mb} MB of image "
                f"decodes run at once beside {reserved // mb} MB, and the machine has "
                f"{memory // mb} MB: at most "
                f"{image_budget.slots_for(memory, reserved=reserved)} fit — unset it to "
                "size the slots from the memory, or set IMAGE_DECODE_MEMORY_BYTES if the "
                "memory was misread")
    wait = g.get("IMAGE_DECODE_WAIT_SECONDS")
    if wait is not None and float(wait) < 0:
        problems.append("IMAGE_DECODE_WAIT_SECONDS must not be negative")

    soft = int(g.get("CELERY_TASK_SOFT_TIME_LIMIT") or 300)
    if int(g.get("PERIODIC_BATCH_SIZE") or 0) < 1:
        problems.append("PERIODIC_BATCH_SIZE must be at least 1")
    if int(g.get("PERIODIC_SCAN_SIZE") or 0) < 1:
        problems.append("PERIODIC_SCAN_SIZE must be at least 1")
    seconds = int(g.get("PERIODIC_BATCH_SECONDS") or 0)
    if seconds < 1 or seconds * 2 > soft:
        problems.append(
            f"PERIODIC_BATCH_SECONDS is {seconds}; a periodic page must spend between 1 "
            f"and {soft // 2} seconds (half of CELERY_TASK_SOFT_TIME_LIMIT={soft}), or "
            "the soft limit cuts it off part-way")
    if int(g.get("PRIVACY_BATCH_ROWS") or 0) < 1:
        problems.append("PRIVACY_BATCH_ROWS must be at least 1")
    spool = int(g.get("PRIVACY_EXPORT_SPOOL_BYTES") or 0)
    if spool < 0 or spool > 64 * mb:
        problems.append("PRIVACY_EXPORT_SPOOL_BYTES must be between 0 and 64 MB: past it "
                        "a data export is held in memory instead of on disk")
    if int(g.get("STORAGE_DELETE_BATCH") or 0) < 1:
        problems.append("STORAGE_DELETE_BATCH must be at least 1")
    return problems


def _public_pages_base_problem(value):
    """Why `PUBLIC_PAGES_BASE_URL` cannot be the public pages' base, or None.

    It must be an absolute https URL with a host that is not this machine: the store
    listings and every build open the privacy policy, terms and account-deletion page
    there (`legal/links.py`)."""
    from urllib.parse import urlsplit

    base = (value or "").strip()
    if not base:
        return ("PUBLIC_PAGES_BASE_URL is not set — the privacy policy, terms and "
                "account-deletion links every build is handed would follow whatever host "
                "a request reached; set it to the hub domain, e.g. https://yalagym.com")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        return (f"PUBLIC_PAGES_BASE_URL is {base!r} — it must be an https URL with a "
                "host, e.g. https://yalagym.com")
    if parts.hostname in ("localhost", "127.0.0.1", "::1") \
            or parts.hostname.endswith(".localhost"):
        return f"PUBLIC_PAGES_BASE_URL is {base!r} — a local address in production"
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

    # --- Redis: caches, WebSocket group layer, job queue, results ---
    # Every role is checked where it ACTUALLY points (the settings above), not where an
    # environment variable says: the broker used to default to localhost and nothing in
    # fly.toml set it, so every queued job was lost with no error anywhere; the channel
    # layer was assembled from REDIS_HOST/REDIS_PORT, which nothing set either. What is
    # refused and why — localhost, a TLS broker that would not check its certificate,
    # and above all a broker in the same database as a cache — is in
    # training_platform/redis_layout.py. The old rule ("the broker must be on DB6 or
    # higher", on ANY host) could not boot on a Redis that serves only database 0.
    try:
        _channel_hosts = CHANNEL_LAYERS["default"]["CONFIG"]["hosts"]  # noqa: F405
    except (KeyError, TypeError):
        _channel_hosts = []
    _redis_roles = [
        ("REDIS_CACHE_URL", f"the {alias!r} cache", config.get("LOCATION", ""),
         "sessions, rate limits and cached reads would fail")
        for alias, config in CACHES.items()
        if "redis" in str(config.get("BACKEND", "")).lower()
    ] + [
        ("REDIS_CHANNELS_URL", "the channel layer (CHANNEL_LAYERS)",
         _host.get("address", "") if isinstance(_host, dict) else str(_host),
         "WebSockets would be dead")
        for _host in _channel_hosts
    ] + [
        ("CELERY_BROKER_URL", "the Celery broker", CELERY_BROKER_URL,
         "background jobs would be lost"),
        ("CELERY_RESULT_BACKEND", "Celery results", CELERY_RESULT_BACKEND,
         "a task's outcome could not be read"),
    ]
    errors.extend(redis_layout.problems(_redis_roles, broker=CELERY_BROKER_URL))

    # --- Email ---
    # The sign-up code is the only way to open an account, so an email setup that cannot
    # deliver is as fatal as a missing database. Refused here rather than discovered from
    # the first person who never receives a code.
    if EMAIL_USE_TLS and EMAIL_USE_SSL:
        errors.append(
            "EMAIL_USE_TLS and EMAIL_USE_SSL are both True; they are two different ways "
            "to encrypt — set EMAIL_USE_SSL for port 465, EMAIL_USE_TLS for 587")
    elif not (EMAIL_USE_TLS or EMAIL_USE_SSL):
        errors.append(
            "EMAIL_USE_TLS and EMAIL_USE_SSL are both False — the SMTP password and every "
            "sign-up code would cross the internet in clear text")
    elif EMAIL_PORT == 465 and not EMAIL_USE_SSL:
        errors.append(
            "EMAIL_PORT is 465, which speaks implicit TLS: set EMAIL_USE_SSL=True "
            "(STARTTLS there stalls until the timeout and no email leaves)")
    elif EMAIL_PORT == 587 and EMAIL_USE_SSL:
        errors.append(
            "EMAIL_PORT is 587, which speaks STARTTLS: set EMAIL_USE_TLS=True, not "
            "EMAIL_USE_SSL")
    if not isinstance(EMAIL_TIMEOUT, int) or EMAIL_TIMEOUT <= 0:
        errors.append(
            "EMAIL_TIMEOUT must be a positive number of seconds — without one a stalled "
            "SMTP server holds a worker for ever")
    _from_problem = _from_address_problem(DEFAULT_FROM_EMAIL)
    if _from_problem:
        errors.append(f"DEFAULT_FROM_EMAIL {_from_problem}")

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

    # --- The public pages' host (BACKEND_BUGS B-174, the owner's decision of 2026-09-28) ---
    # The capabilities answer hands every build the privacy policy, terms and
    # account-deletion URLs (legal/links.py), and the store listings name the same
    # pages. Unset, the links followed whichever allowed host a request reached — the
    # API's own address, not the hub domain the listing names — so production refuses
    # to boot without an https base.
    _pages_problem = _public_pages_base_problem(globals().get("PUBLIC_PAGES_BASE_URL"))
    if _pages_problem:
        errors.append(_pages_problem)

    # --- Object storage ---
    # 'local' is the filesystem backend, and it cannot be production. A machine's own
    # filesystem is lost when it stops, and a volume attaches to ONE machine while the
    # web group runs several, so a picture uploaded through machine A is a 404 through
    # machine B; video does not fit on one at all. fly.toml mounts no volume. The
    # deploy sets MEDIA_BACKEND=gcs (the owner, 2026-09-19) or MEDIA_BACKEND=s3 (any
    # S3-compatible store: what the single-server deploy in deploy/ uses, so that a
    # rebuilt or replaced server still has every upload). Refused at boot rather than
    # discovered from uploads that vanish.
    _media_backend = (globals().get("MEDIA_BACKEND") or "local").lower()
    if _media_backend == "s3":
        _s3_bucket = globals().get("S3_BUCKET_NAME")
        if not _s3_bucket:
            errors.append("S3_BUCKET_NAME is not set — MEDIA_BACKEND=s3 cannot store anything")
        # The address is where every upload and every signed URL goes. Plain http would
        # carry members' photos and the signature itself in the clear, and a signed URL
        # is a bearer token for as long as it lives.
        _s3_endpoint = globals().get("S3_ENDPOINT_URL") or ""
        if _s3_endpoint and not _s3_endpoint.lower().startswith("https://"):
            errors.append(
                f"S3_ENDPOINT_URL is {_s3_endpoint!r} — it must be an https:// address "
                "(empty means AWS itself)")
        _s3_style = globals().get("S3_ADDRESSING_STYLE") or "virtual"
        if _s3_style not in ("virtual", "path"):
            errors.append(
                f"S3_ADDRESSING_STYLE is {_s3_style!r} — it must be 'virtual' or 'path'")
        # Both halves of the key pair, READ from wherever `s3_storage` will read them
        # (SIDE_BUGS S-148: "a secret store is configured" proved nothing for GCS).
        for _s3_secret in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"):
            _s3_problem = _s3_key_problem(_s3_secret)
            if _s3_problem:
                errors.append(_s3_problem)
        # Identity documents, by the same rule as on GCS below: a public media bucket
        # must not be the bucket that holds them.
        _s3_docs_bucket = globals().get("VERIFICATION_S3_BUCKET_NAME") or _s3_bucket
        if globals().get("MEDIA_PUBLIC_APP_CONTENT") and _s3_docs_bucket == _s3_bucket:
            errors.append(
                "MEDIA_PUBLIC_APP_CONTENT makes the media bucket public, and trainer "
                "identity documents would be stored in it — set "
                "VERIFICATION_S3_BUCKET_NAME to a private bucket")
    elif _media_backend != "gcs":
        errors.append(
            f"MEDIA_BACKEND is {_media_backend!r} — production must use object storage "
            "('gcs' or 's3'); the local filesystem backend loses every upload"
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

    # --- Heavy work in requests and jobs (the production-readiness audit, 2026-09-25) ---
    errors.extend(heavy_work_problems())
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
