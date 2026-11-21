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
from urllib.parse import urlsplit

from .settings_secrets import require_env, get_env, get_int_env, get_secret, get_bool_env_optional

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
    "policy",
    "mediakit",
    "chat",
    # Reporting, blocking, the moderation queue and AI consent (moderation/models.py).
    "moderation",
    # A trainer's verification: documents, review states, the console queue
    # (trainer_verification/models.py).
    "trainer_verification",
    # A trainer's withdrawals and payout methods, and the operator's queue
    # (payouts/models.py).
    "payouts",
    # Released builds per channel and the update endpoint (releases/models.py).
    "releases",
    # The privacy policy, the terms and the account-deletion page (legal/models.py).
    "legal",

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

# DB5: dedicated channel layer — isolated from sessions (DB0) and all other caches.
#
# Built from REDIS_URL, the same variable every cache uses, because this was the one
# Redis client in the project assembled from REDIS_HOST/REDIS_PORT instead. Neither
# appears in fly.toml, the Dockerfile or DEPLOY_CHECKLIST.md, so production silently
# resolved to redis://127.0.0.1:6379/5 — a Redis that does not exist inside the app
# container. Boot succeeded and the AI assistant and social consumers simply could not
# reach their group layer. REDIS_HOST/REDIS_PORT still work for a local dev box that
# sets them.
#
# Each Redis role can also take its own full URL (training_platform/redis_layout.py):
# REDIS_CHANNELS_URL here, CELERY_BROKER_URL and CELERY_RESULT_BACKEND below, and
# REDIS_CACHE_URL in settings_production. Unset, a role takes its database of this one.
from .redis_layout import with_database as _with_database

_REDIS_BASE = get_env("REDIS_URL") or (
    f"redis://{get_env('REDIS_HOST', '127.0.0.1')}:{get_int_env('REDIS_PORT', 6379)}"
)

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [get_env("REDIS_CHANNELS_URL") or _with_database(_REDIS_BASE, 5)],
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
    # Answers the machine health check (GET|HEAD /api/auth/health/, nothing else) before
    # CommonMiddleware reads the Host header, which the platform's checker does not set
    # to an allowed name. See training_platform/health_probe.py for the reasoning.
    "training_platform.health_probe.HealthProbeMiddleware",
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
    # After the general limiter, which is a coarse per-IP DoS ceiling. This one is
    # the brute-force control for the admin login, keyed on the username too.
    "training_platform.middleware.AdminLoginThrottleMiddleware",
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
# allauth 65 replaced ACCOUNT_AUTHENTICATION_METHOD = 'email', ACCOUNT_EMAIL_REQUIRED =
# True and ACCOUNT_USERNAME_REQUIRED = False with the two settings below — the exact
# values its own deprecation warnings name for that combination (Django 5.2 upgrade,
# 2026-09-27). They did NOT keep the register serializer's fields: dj-rest-auth 7.2
# reads a field missing from SIGNUP_FIELDS as required, so `username` became required
# and every app sign-up (it sends none) was refused. users/serializers.py
# `CustomRegisterSerializer` declares `username` optional itself (BACKEND_BUGS B-172).
ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']
ACCOUNT_ADAPTER = "users.adapters.CustomAccountAdapter"

# Signing in to the web console on a computer with a link made on the operator's phone
# (OperatorPhone, BACKEND_BUGS B-120; admin_dashboard/console_login.py). A NEW WAY INTO
# THE CONSOLE, so it is off until the owner approves it: while False every route of it
# answers `console_login_disabled` and nothing is written. Turning it on is the owner's
# decision, not a deploy detail.
OPS_QR_LOGIN_ENABLED = get_bool_env_optional("OPS_QR_LOGIN_ENABLED", False)
# The console's own origin (e.g. https://ops.yalagym.com) when it is served under a
# host of its own; empty means the host the API request came to. Used for the links the
# operator's phone is given (B-119 queue pages, the B-120 sign-in link).
OPS_CONSOLE_BASE_URL = get_env("OPS_CONSOLE_BASE_URL", "")
# Where the public pages — the privacy policy, the terms, the account-deletion page
# (legal/) — are reached from outside, e.g. https://yalagym.com when the hub domain serves
# or forwards `/legal/...`. The capabilities answer hands these URLs to every build
# (legal/links.py). Empty means the host the API request came to — in development only:
# production refuses to boot without an https value (BACKEND_BUGS B-174).
PUBLIC_PAGES_BASE_URL = get_env("PUBLIC_PAGES_BASE_URL", "")
# Returning from the web payment hub to the app (the payments spec, section 5): the
# Android App Links and iOS Universal Links association files at /.well-known/
# (releases/app_links.py). Each list is comma-separated. Empty means that file answers
# 404 `app_links_not_configured` — an id is never guessed. The Android fingerprints are
# the SHA-256 of every release-signing certificate (the direct APK's own key, and the
# Play app-signing key once there is a Play build); the paths are the hub URLs the app
# opens (e.g. "/app/*"), which iOS reads from the file and Android from the manifest.
APP_LINKS_ANDROID_PACKAGES = get_env("APP_LINKS_ANDROID_PACKAGES", "")
APP_LINKS_ANDROID_SHA256_FINGERPRINTS = get_env("APP_LINKS_ANDROID_SHA256_FINGERPRINTS", "")
APP_LINKS_IOS_TEAM_ID = get_env("APP_LINKS_IOS_TEAM_ID", "")
APP_LINKS_IOS_BUNDLE_IDS = get_env("APP_LINKS_IOS_BUNDLE_IDS", "")
APP_LINKS_PATHS = get_env("APP_LINKS_PATHS", "")

# REST Auth
REST_AUTH_SERIALIZERS = {
    'REGISTER_SERIALIZER': 'users.serializers.CustomRegisterSerializer',
    'LOGIN_SERIALIZER': 'users.serializers.CustomLoginSerializer',
}

# drf-yasg's "compat" renderers answer a `format` with a leading dot (`.json`), for a
# URLconf written as `swagger(?P<format>\.json|\.yaml)`. This one has no such route —
# only the `swagger/` and `redoc/` UIs, and only under DEBUG (training_platform/urls.py)
# — so the compat copies were dead weight and drf-yasg warned about them on every
# start. False is the library's future default.
SWAGGER_USE_COMPAT_RENDERERS = False

# ========================
# REST Framework
# ========================
from django.utils.translation import gettext_lazy as _

#: The one currency this platform charges and holds money in. Every SubscriptionPlan
#: is priced in it (`SubscriptionPlan.currency` defaults to SYP and calls itself the
#: single source of truth), every Payment inherits it — and `Wallet.currency` was
#: hardcoded to a default of "USD" that nothing ever set from the plan. Since
#: `move_funds_atomic` skipped its currency guard whenever there was no source wallet,
#: the mint leg of a payment credited 5,000 SYP into a wallet labelled USD at 1:1, and
#: the trainer's withdrawable balance read 5,000 USD for a subscription worth about
#: forty cents. The ledger reconciled perfectly the whole way, which is why it survived
#: every internal check: the arithmetic was right and the unit was wrong.
#:
#: THE BASE CURRENCY, not the only one. The platform sells in SYP and USD, both first
#: class (the owner, 2026-09-21; `wallet.currencies`). This is what a row gets when
#: nothing has said which money it is yet: a wallet nobody has funded or chosen a
#: currency for, a ledger row written outside `move_funds_atomic`. Everything that
#: MOVES money names its currency and never falls back here.
#:
#: Changing this does NOT re-denominate anything. Existing wallets keep the currency
#: they hold, and the platform's own ledger owners are addressed by currency
#: (`wallet.utils._ledger_owner_wallet`), so nothing is reinterpreted underneath.
PLATFORM_CURRENCY = get_env("PLATFORM_CURRENCY", "SYP")

#: How old the operator's USD/SYP rate may be before a CONVERSION is refused
#: (`wallet.exchange`). A same-currency purchase never consults the rate, so a stale
#: one can never stop a pound-priced plan being sold to a pound wallet. Seven days is
#: a chosen default and wants the owner's confirmation: long enough that a quiet week
#: does not stop sales, short enough that nobody sells dollars at a fortnight-old pound.
EXCHANGE_RATE_MAX_AGE_DAYS = int(get_env("EXCHANGE_RATE_MAX_AGE_DAYS", "7"))

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'charging': '10/minute',
        # Analytics rows are written by the app, and nothing bounded how fast. Generous
        # for real telemetry, fatal to a loop filling the tables.
        'analytics_write': '300/hour',
    },
    # DRF's own parsers, with one addition: a NUL byte anywhere in a request payload is
    # a 400 here rather than a ValueError from psycopg at execute time, which several
    # views caught as an unknown fault and answered 500. See training_platform/parsers.py.
    'DEFAULT_PARSER_CLASSES': [
        'training_platform.parsers.SafeJSONParser',
        'training_platform.parsers.SafeFormParser',
        'training_platform.parsers.SafeMultiPartParser',
    ],
    # ONE pagination shape for every list endpoint: {count, next, previous, results}.
    # Previously three shapes were in play (paginated dict, bare array, cursor) and
    # seven viewsets had no pagination at all, so they returned EVERY row — an
    # unbounded response on a 512MB box and three parsers for the mobile client.
    #
    # One endpoint is deliberately outside this: the notification list is cursor
    # paginated, because new notifications arrive between page loads and offset paging
    # drifts. It answers {next, previous, results} with no `count` — counting is the
    # cost a cursor exists to avoid — and rejects `?page=` rather than silently
    # returning the first page to a client using the wrong contract. See
    # social/views.py NotificationCursorPagination.
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
# (tunable per-agent in the console). Launch model: trusted prepaid cash-in.
#
# THE OWNER CONFIRMS THESE NUMBERS (plan step 9, 2026-09-24). The spec, section 8: "Raise
# the default limits. 200 SYP a day cannot cover one Basic plan at 1,000 SYP ... with the
# daily default above the price of the top plan." The top plan is Professional at 5,000
# SYP a month (`setup_subscription_plans`); a client also tops up to pay a coach by the
# month, which costs more than any plan. The four numbers are the ones the agent screens
# draw (`16-agent-operator/AgentHistory`: 50,000 a top-up, 250,000 a day, 5,000,000 a
# month; `AgentHome`: a 600,000 ceiling on the cash held for the platform) — a proposal,
# not a decision. They are SYRIAN POUNDS and they are what a NEW agent gets
# (`wallet.models.ensure_agent_profile`); migration wallet/0019 lifts an agent still on the
# old 200 / 5,000 pair to them, and leaves every cap an operator chose alone.
AGENT_DEFAULT_DAILY_LIMIT = _Decimal(get_env("AGENT_DEFAULT_DAILY_LIMIT", "250000"))
AGENT_DEFAULT_MONTHLY_LIMIT = _Decimal(get_env("AGENT_DEFAULT_MONTHLY_LIMIT", "5000000"))
#: The most ONE top-up may be. Empty (None) on an agent means no per-top-up cap of its own
#: (the daily and monthly caps still bound it).
AGENT_DEFAULT_PER_TRANSACTION_LIMIT = _Decimal(
    get_env("AGENT_DEFAULT_PER_TRANSACTION_LIMIT", "50000"))
#: The most cash an agent may hold for the platform, unsettled, before top-ups stop (the
#: spec: "Block top-ups when an agent's unsettled total passes their ceiling"). Empty on an
#: agent means its monthly cap in that currency (`wallet.agents.Usage.ceiling`).
AGENT_DEFAULT_RECEIVABLE_CEILING = _Decimal(
    get_env("AGENT_DEFAULT_RECEIVABLE_CEILING", "600000"))
# The DOLLAR defaults are still nobody's: AGENT_DEFAULT_DAILY_LIMIT_USD /
# AGENT_DEFAULT_MONTHLY_LIMIT_USD are unset, so a new agent tops up no dollars until an
# operator sets its dollar caps (SIDE_BUGS S-059), and its dollar per-top-up cap and
# ceiling are empty (see above) until the owner names numbers.
# Optional dedicated Fernet key (base64) for encrypting agent API secrets at rest.
# If unset, a key is derived from SECRET_KEY — a DB leak alone still cannot forge
# signatures, since SECRET_KEY is not stored in the database.
AGENT_APIKEY_ENC_KEY = get_env("AGENT_APIKEY_ENC_KEY", "")

# One-to-one coaching is sold by the month (owner, 2026-09-19): the first month is held
# when the trainer accepts and paid out when the first program arrives; each month after
# is charged from the client's wallet when the last one ends. The platform keeps
# COACHING_COMMISSION_RATE of EVERY month, first and renewals alike; the rate is copied
# onto each period when it is charged, so changing it never reprices a month already paid.
# Every client is told COACHING_RENEWAL_REMINDER_DAYS before a month ends. A month the
# wallet cannot cover FREEZES the coaching (the owner, 2026-09-19): the client still sees
# the trainer's routines and cannot start them, and is reminded on each day of
# COACHING_FROZEN_REMINDER_DAYS after the freeze, until a month is paid. See
# users/coaching.py.
COACHING_COMMISSION_RATE = _Decimal("0.20")
# A PLATFORM PLAN is not coaching: it stays 100% with the platform whether or not the
# client has a trainer (the owner, 2026-09-19). The share is kept as a setting, at zero,
# so the owner can decide later to pay a trainer for a plan their client buys — it is not
# deleted from the code, it is priced at nothing. Until then every plan payment lands in
# the platform's revenue wallet and no trainer is paid for it. See
# `wallet/signals.py:handle_payment_completed`.
SUBSCRIPTION_TRAINER_SHARE = _Decimal("0")
COACHING_PERIOD_DAYS = 30
COACHING_RENEWAL_REMINDER_DAYS = 3
COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)
# The platform plan, the same way: a notice SUBSCRIPTION_RENEWAL_NOTICE_DAYS before it
# ends, then one on each day of SUBSCRIPTION_LAPSED_REMINDER_DAYS after it ended (0 is
# the day it ends). Lapsed, the last diet plan stays readable and nothing can be used.
# See subscription/tasks.py.
# The checkout poller (subscription/tasks.py poll_open_payments). The grace is how long
# the webhook has to arrive before anything else asks; the TTL is how long a checkout
# nobody finished stays open, after which the customer can start a new one and a late
# payment for the old one is still honoured (`expired -> completed`).
PAYMENT_POLL_GRACE_SECONDS = 20
PAYMENT_CHECKOUT_TTL_MINUTES = 60
PAYMENT_POLL_BATCH = 100
# How long a CLOSED checkout is still asked about (subscription/tasks.py
# poll_expired_checkouts): the ShamCash adapter's own lookback, since nothing older
# could be found on the merchant account anyway.
PAYMENT_LATE_MONEY_WINDOW_HOURS = 72
# When the payment screens say "ShamCash is not responding, your payment is safe, we
# will keep checking" (the spec, section 6; subscription/gateway_health.py): after this
# many reads of the merchant account in a row could not reach it, over at least this
# many seconds. What the screens SAY only — nothing is expired or refused on it.
PAYMENT_GATEWAY_DEGRADED_AFTER_FAILURES = get_int_env("PAYMENT_GATEWAY_DEGRADED_AFTER_FAILURES", 2)
PAYMENT_GATEWAY_DEGRADED_AFTER_SECONDS = get_int_env("PAYMENT_GATEWAY_DEGRADED_AFTER_SECONDS", 60)

SUBSCRIPTION_RENEWAL_NOTICE_DAYS = 3
SUBSCRIPTION_LAPSED_REMINDER_DAYS = (0, 3, 7, 14, 30)
# The EMAIL before the plan ends (the payments spec, section 7: "Plan expires in 7 days,
# 3 days, 1 day — email: 7-day only"): once per cover, when its end first comes within
# this many days. Sent by the same daily job as the 3-day push, which it does not change.
SUBSCRIPTION_EXPIRY_EMAIL_DAYS = 7

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
# The build headers every client sends (policy/channels.py). Without this a browser — the
# web payment hub — fails its preflight, because neither is in the default allow-list.
from corsheaders.defaults import default_headers as _cors_default_headers  # noqa: E402
CORS_ALLOW_HEADERS = (*_cors_default_headers, "x-app-channel", "x-app-version")
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

# The AI coach's model endpoint (`ai_assistant/services/chat_service.py`, the one place
# a model is called). The coach speaks the OpenAI Chat Completions protocol, which
# most providers and self-hosted servers (vLLM, Ollama) also answer, so the provider
# is configuration, not code:
#
#   AI_API_KEY           the provider's key. Falls back to OPENAI_API_KEY, so an
#                        environment written before this setting keeps working.
#   AI_BASE_URL          the provider's address, ending in `/v1`. Empty = the client
#                        library's default (OpenAI's own API).
#   AI_ASSISTANT_MODEL   the model's name at that provider.
#   AI_EXTRA_BODY        optional JSON object merged into every request, for a switch
#                        the protocol has no field for (turning a model's "thinking"
#                        phase off is spelled differently by every provider).
#   AI_PRICE_INPUT_PER_1M_USD / AI_PRICE_OUTPUT_PER_1M_USD
#                        the model's price per million tokens, both or neither. They
#                        feed the daily cost ceiling below; without them a model the
#                        price table does not know is counted at the table's default.
#
# No key at all is a supported state, not an error: the app boots and the coach answers
# every turn with the `config_error` code instead of calling anything.
AI_API_KEY = get_env("AI_API_KEY", "") or OPENAI_API_KEY
AI_BASE_URL = get_env("AI_BASE_URL", "").strip()


def _ai_extra_body():
    """`AI_EXTRA_BODY` as a dict. Malformed JSON stops the boot: a typo here would
    otherwise be a silently ignored switch, found only on the provider's invoice."""
    import json

    from django.core.exceptions import ImproperlyConfigured

    raw = get_env("AI_EXTRA_BODY", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(f"AI_EXTRA_BODY is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ImproperlyConfigured("AI_EXTRA_BODY must be a JSON object, e.g. {}")
    return value


def _ai_price_per_1m_usd():
    """The configured model's price as `{"input": ..., "output": ...}` strings, or None.
    One without the other stops the boot: half a price is a wrong cost ceiling."""
    from decimal import Decimal, InvalidOperation

    from django.core.exceptions import ImproperlyConfigured

    names = ("AI_PRICE_INPUT_PER_1M_USD", "AI_PRICE_OUTPUT_PER_1M_USD")
    raw = [get_env(name, "").strip() for name in names]
    if not any(raw):
        return None
    if not all(raw):
        raise ImproperlyConfigured(f"Set both {names[0]} and {names[1]}, or neither.")
    for name, value in zip(names, raw):
        try:
            if Decimal(value) < 0:
                raise InvalidOperation
        except InvalidOperation:
            raise ImproperlyConfigured(f"{name} must be a number >= 0, got {value!r}") from None
    return {"input": raw[0], "output": raw[1]}


AI_ASSISTANT_CONFIG = {
    # `or`, not a default argument: an env file line `AI_ASSISTANT_MODEL=` sets the
    # variable to the empty string, and an empty model name is refused by every provider.
    "MODEL": get_env("AI_ASSISTANT_MODEL", "").strip() or "gpt-4o-mini",
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
    # Provider-specific request switches and the model's price: see the notes above.
    "EXTRA_BODY": _ai_extra_body(),
    "PRICE_PER_1M_USD": _ai_price_per_1m_usd(),
}

# ========================
# Celery
# ========================
# DB6/DB7 of REDIS_URL by default — deliberately outside the 6 logical DBs the caches
# and the channel layer use. The default used to be DB0, which settings_production
# assigns to SESSIONS, so queue keys and session keys shared a keyspace. Either can be a
# full URL of its own (a Redis that serves only database 0, a separate instance); the
# production boot check refuses a broker in the same database as a cache wherever it is
# (training_platform/redis_layout.py). The defaults followed only `localhost`, so a
# production that set REDIS_URL and forgot CELERY_RESULT_BACKEND stored results nowhere.
CELERY_BROKER_URL = get_env('CELERY_BROKER_URL') or _with_database(_REDIS_BASE, 6)
CELERY_RESULT_BACKEND = get_env('CELERY_RESULT_BACKEND') or _with_database(_REDIS_BASE, 7)

# How long a `.delay()` may wait to REACH Redis. Unset, a broker or result store that is
# unreachable (a firewall that drops packets) holds the caller for the operating
# system's TCP connect timeout — minutes — and the callers include requests: sign-up and
# password reset queue their email (users/utils.py `queue_otp_email`). A healthy Redis
# answers in milliseconds, so five seconds costs nothing and bounds the worst case.
CELERY_BROKER_TRANSPORT_OPTIONS = {'socket_connect_timeout': 5}
CELERY_REDIS_SOCKET_CONNECT_TIMEOUT = 5

# Acknowledge a task only after it finishes. With the default (ack on delivery) a worker
# killed mid-task — routine on Fly, where machines stop when idle — dropped the job
# permanently. Paired with idempotent consumers this gives at-least-once delivery.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# A hung external call (LLM, Edamam, FCM) previously held its worker slot forever.
CELERY_TASK_SOFT_TIME_LIMIT = int(get_env('CELERY_TASK_SOFT_TIME_LIMIT', 300))
CELERY_TASK_TIME_LIMIT = int(get_env('CELERY_TASK_TIME_LIMIT', 360))

# Periodic jobs over every user run a PAGE per invocation and hand the rest on to a
# fresh one (training_platform/batching.py): one task for every client under the soft
# limit above was cut off part-way once the platform grew, and whoever sorted later was
# silently skipped (the production-readiness audit, 2026-09-25). Rows a page; seconds a
# page may spend (the production boot check refuses a value that leaves no room under
# CELERY_TASK_SOFT_TIME_LIMIT); rows a page for scans whose per-row work is a comparison.
PERIODIC_BATCH_SIZE = get_int_env('PERIODIC_BATCH_SIZE', 200)
PERIODIC_BATCH_SECONDS = get_int_env('PERIODIC_BATCH_SECONDS', 120)
PERIODIC_SCAN_SIZE = get_int_env('PERIODIC_SCAN_SIZE', 5000)

# Export and erasure read, delete and anonymise a person's rows this many at a time
# (training_platform/privacy/registry.py), and an export larger than the spool limit is
# written to a temporary file and streamed from there instead of held in memory.
PRIVACY_BATCH_ROWS = get_int_env('PRIVACY_BATCH_ROWS', 1000)
PRIVACY_EXPORT_SPOOL_BYTES = get_int_env('PRIVACY_EXPORT_SPOOL_BYTES', 8 * 1024 * 1024)

# Stored files of deleted rows leave storage after the commit, through a Celery task,
# this many names a message (training_platform/storage_cleanup.py).
STORAGE_DELETE_BATCH = get_int_env('STORAGE_DELETE_BATCH', 100)
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
    'training_platform.background_health',
)

#: The beat jobs whose silence costs money, and how long each may go without a success
#: before it counts as stale (seconds; a few of its periods, so one slow or failed run is
#: not an alert). Watched two ways: `GET /api/auth/health/background/` reports each one
#: (training_platform/background_health.py), and when SENTRY_DSN is set each one is a
#: Sentry cron monitor (training_platform/observability.py). Keys are beat ENTRY names.
BACKGROUND_WATCHED_JOBS = {
    # Every minute. In reconcile mode the only thing that completes a paid checkout.
    'poll-open-payments': 5 * 60,
    # Every ten minutes. The backstop that pays a trainer an owed payout.
    'settle-owed-payouts': 35 * 60,
    # Hourly. Charges the coaching months that ended and freezes the unpaid ones.
    'renew-coaching-periods': 2 * 3600 + 10 * 60,
    # Hourly. Ends paid access whose cover ran out.
    'expire-lapsed-subscriptions': 2 * 3600 + 10 * 60,
}

#: The heartbeat runs every minute; three missed in a row is an outage, not a slow queue.
BACKGROUND_HEARTBEAT_MAX_AGE_SECONDS = get_int_env('BACKGROUND_HEARTBEAT_MAX_AGE_SECONDS', 180)

CELERY_BEAT_SCHEDULE = {
    # Proves beat, the broker and a worker are all alive: read by
    # GET /api/auth/health/background/ (training_platform/background_health.py). A
    # heartbeat still queued after 50 s is dropped rather than run late, so a backed-up
    # queue shows as a stale heartbeat instead of hiding behind a burst of old ones.
    'background-heartbeat': {
        'task': 'training_platform.background_heartbeat',
        'schedule': crontab(minute='*'),
        'options': {'expires': 50},
    },
    # Coaching is monthly: remind 3 days ahead, charge the months that ended, freeze the
    # ones the wallet cannot cover and keep reminding them (users/coaching.py). Hourly,
    # so a renewal — or a top-up that unfreezes — lands within the hour.
    'renew-coaching-periods': {
        'task': 'users.renew_coaching_periods',
        'schedule': crontab(minute=25),
    },
    # A checkout the gateway never told us about. Every minute: the webhook gets first
    # go (PAYMENT_POLL_GRACE_SECONDS), this backs it up, and a checkout nobody finished
    # is closed after PAYMENT_CHECKOUT_TTL_MINUTES so the customer can start again.
    # Without it one lost delivery left a customer unable to buy at all, for ever.
    'poll-open-payments': {
        'task': 'subscription.poll_open_payments',
        'schedule': crontab(minute='*'),
    },
    # Money that arrives for a checkout the poller already closed (SIDE_BUGS S-077). In
    # reconcile mode no webhook will ever say so, so this asks the gateway about each
    # closed checkout for PAYMENT_LATE_MONEY_WINDOW_HOURS.
    'poll-expired-checkouts': {
        'task': 'subscription.poll_expired_checkouts',
        'schedule': crontab(minute='*/10'),
    },
    # Money no checkout claimed: a transfer with no reference, a mistyped one, or one
    # for a checkout already paid. The pollers ask by reference and cannot see it; this
    # reads the account and queues it for an operator (BACKEND_BUGS B-051).
    'sweep-unmatched-transfers': {
        'task': 'subscription.sweep_unmatched_transfers',
        'schedule': crontab(minute='5-59/10'),
    },
    # The backstop PaymentService, wallet/signals.py and PaymentAdmin all describe for a
    # payout the on-commit receiver lost. It was registered and scheduled nowhere, so it
    # never ran (SIDE_BUGS S-076). Every ten minutes, matching its PAYOUT_GRACE.
    'settle-owed-payouts': {
        'task': 'subscription.settle_owed_payouts',
        'schedule': crontab(minute='5-59/10'),
    },
    # The platform plan: "ends soon" 3 days ahead, then "ended" on the cadence in
    # SUBSCRIPTION_LAPSED_REMINDER_DAYS (subscription/tasks.py). Daily at 17:00 UTC,
    # 20:00 in Damascus: a reminder, not a 3 a.m. push. Runs after the hourly sweep.
    'send-subscription-renewal-reminders': {
        'task': 'subscription.send_renewal_reminders',
        'schedule': crontab(hour=17, minute=35),
    },
    # The week, once a week (routine/tasks.py): each client's own recap and each
    # trainer's digest of who trained and who needs a look. Sunday 17:00 UTC is Sunday
    # evening in Damascus, when the week is done and the next one is being thought about.
    'send-weekly-progress': {
        'task': 'routine.send_weekly_progress',
        'schedule': crontab(day_of_week=0, hour=17, minute=5),
    },
    # A workout started and never finished stops claiming to be in progress
    # (SIDE_BUGS S-022). Ten minutes before the day sweep below, so a day whose session
    # was abandoned is recorded in the same night. Every reader already applies the
    # twelve-hour rule itself (routine/sessions.py), so this is housekeeping: a night
    # it does not run changes nobody's screen.
    'abandon-forgotten-sessions': {
        'task': 'routine.abandon_forgotten_sessions',
        'schedule': crontab(hour=1, minute=5),
    },
    # A training day opened and never closed still says what was left of it (the owner,
    # 2026-09-20). Nightly, after midnight in Damascus, so "yesterday" is really over.
    'close-open-training-days': {
        'task': 'routine.close_open_training_days',
        'schedule': crontab(hour=1, minute=15),
    },
    # `age` follows `date_of_birth` (users/models.py). Saves keep it right; this moves
    # everyone whose birthday it is and nobody else writes to their row today.
    'refresh-ages-from-birth-dates': {
        'task': 'users.refresh_ages_from_birth_dates',
        'schedule': crontab(hour=0, minute=10),
    },
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
        'schedule': crontab(minute=20),
    },
    # The email channel (notifications/channels/email.py) publishes each email after the
    # commit and never fails the caller, so a broker down at that moment, or a worker
    # killed mid-send, leaves an email owed with no message. Hourly, this publishes those
    # again and gives up what is too old (notifications/tasks.py).
    'sweep-unsent-emails': {
        'task': 'notifications.sweep_unsent_emails',
        'schedule': crontab(minute=50),
    },
    # A video whose metadata task was lost (Celery is at-least-once: a worker that dies
    # between the commit and the ack drops the message) would sit `pending` for ever —
    # playable, but with no poster and marked as if something were still coming.
    # Hourly; `fill_video_metadata` is idempotent, so a duplicate run is harmless.
    'sweep-pending-videos': {
        'task': 'mediakit.sweep_pending_videos',
        'schedule': crontab(minute=40),
    },
    # Turns is_liked / is_completed / actual_quantity_consumed — all collected and
    # previously read by nothing — into planner ranking weights.
    'refresh-food-weights': {
        'task': 'diet.planner.refresh_food_weights',
        # `crontab`, not a plain interval. An interval entry's clock starts at worker
        # startup — a fresh `ScheduleEntry` sets `last_run_at = now`, so the first fire
        # is a full interval later — and `celery -A ... worker --beat` keeps its schedule
        # file on the container filesystem, which `[mounts]` scopes to the web process
        # only. On a free-tier Fly machine a deploy, an OOM or a machine replacement
        # resets that clock, so a daily job never ran if the worker restarted more often
        # than daily. Two of these are the GDPR retention purges, whose entire point is a
        # legal deadline, and the beat log looked perfectly healthy throughout.
        'schedule': crontab(hour=2, minute=30),
    },
    # Retention. Every source in training_platform/privacy/sources.py that declares
    # retention_days is enforced here — analytics IPs at 180 days, notifications at
    # 90, OTP/reset tokens at 1.
    'purge-expired-personal-data': {
        'task': 'training_platform.privacy.purge_expired_personal_data',
        'schedule': crontab(hour=3, minute=15),
    },
    # A replaced identity document is kept 90 days for comparison at the next review,
    # then deleted with its files (trainer_verification/services.py).
    'purge-replaced-verification-documents': {
        'task': 'trainer_verification.purge_replaced_documents',
        'schedule': crontab(hour=3, minute=25),
    },
    # Paid access has to be able to lapse. subscription.utils.expire_subscriptions
    # said "This should be run as a scheduled task" and never was, so 64 of 72 rows
    # marked 'active' were already past their end_date. Hourly rather than daily: the
    # gap between cover ending and the sweep noticing is free access.
    'expire-lapsed-subscriptions': {
        'task': 'subscription.expire_lapsed_subscriptions',
        'schedule': crontab(minute=5),
    },
    # The API derives a challenge's phase from its dates; this moves the stored column
    # with it, so the admin changelist and its status filter agree with what clients see.
    'advance-challenge-phases': {
        'task': 'social.advance_challenge_phases',
        'schedule': crontab(minute=10),
    },
    'close-idle-ai-sessions': {
        'task': 'ai_assistant.tasks.close_idle_sessions',
        'schedule': 600,
    },
    'compute-daily-ai-insights': {
        'task': 'ai_assistant.tasks.compute_all_user_insights',
        'schedule': crontab(hour=4, minute=0),
    },
    'check-ai-cost-alert': {
        'task': 'ai_assistant.tasks.check_daily_cost',
        'schedule': crontab(minute=40),
    },
    # Registered with Celery and in no schedule, so the ONLY code that reads
    # `AITrainingData.retain_until` the way it is written — a deadline, not an age — never
    # ran. `purge-expired-personal-data` above covers the same rows through the privacy
    # registry; both are kept because they answer to different owners (this one to the
    # assistant's own consent record, that one to the retention policy) and either
    # disappearing should not silently end the other.
    'purge-expired-ai-training-data': {
        'task': 'ai_assistant.tasks.purge_expired_training_data',
        'schedule': crontab(hour=3, minute=45),
    },
    # `ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` mean every refresh writes two
    # permanent rows, and simplejwt ships `flushexpiredtokens` for exactly this — it
    # appeared in no schedule, no Dockerfile and no checklist. The full RS256 token is
    # stored in a text column at ~869 bytes: one active mobile client rotating hourly is
    # 25 rows a day, so 10,000 daily users is 250,000 rows and 217 MB of heap a day,
    # 91 million rows a year, on a 1 GB volume. The unique index on `jti` and its
    # `varchar_pattern_ops` twin make every refresh progressively slower long before the
    # disk fills.
    'flush-expired-jwt-tokens': {
        'task': 'training_platform.privacy.flush_expired_tokens',
        'schedule': crontab(hour=3, minute=50),
    },
}

# ========================
# Nutrition & AI Chef
# ========================

#: Per-goal overrides for `diet/planner/policy.py`. Empty by default, and declared here so
#: the name exists in one place a deployment can find. `load_policy` merges
#: `DIET_PLANNER_POLICY["*"]` then `DIET_PLANNER_POLICY[<goal>]` over the dataclass
#: defaults, taking any field it declares — `tolerance`, `meal_kcal_split`,
#: `max_kcal_per_meal`, `snack_kcal` and the rest.
#:
#:     DIET_PLANNER_POLICY = {"*": {"tolerance": {"fat": 0.25}},
#:                            "lose": {"snack_kcal": 150.0}}
#:
#: It was read by `load_policy` and declared nowhere, so every constant in `PlannerPolicy`
#: was unreachable to a deployment and the module's stated purpose — "a dietitian could
#: not change one without a deploy" — was not met by the one mechanism meant to meet it.
DIET_PLANNER_POLICY: dict = {}

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

DIET_DYNAMIC_MEAL_ALLOCATION = True
# When True, a failure inside one of the planner's paths raises instead of being
# logged and served around. The gate and settings_local set it; production leaves
# it off so a client gets dinner and an engineer gets an error log.
DIET_PLANNER_STRICT = get_bool_env_optional("DIET_PLANNER_STRICT", False)
# Whether food search also queries the external food database. Off: the curated
# catalogue (diet/data/catalogue.py) is the source of truth since the rebuild, and an
# external row cannot be chosen or planned until it is imported and vetted.
DIET_EXTERNAL_FOOD_SEARCH = get_bool_env_optional("DIET_EXTERNAL_FOOD_SEARCH", False)

# Edamam API — optional; empty disables nutrition lookups (graceful degradation)
EDAMAM_APP_ID = get_env("EDAMAM_APP_ID", "")
EDAMAM_APP_KEY = get_env("EDAMAM_APP_KEY", "")

# ========================
# Firebase / Push Notifications — optional; empty disables push (already degrades)
# ========================
FIREBASE_CREDENTIALS_PATH = get_env("FIREBASE_CREDENTIALS_PATH", "")
#: The service-account JSON itself, raw or base64. A PATH cannot be delivered to a Fly
#: machine — `fly secrets set` sets environment variables, and the key file is outside
#: the repository and out of the image — so the only reason a path ever resolved in
#: production was the developer `.env` that `COPY . .` used to bake in. Set this instead:
#:   fly secrets set FIREBASE_CREDENTIALS_JSON="$(base64 -i service-account.json)"
FIREBASE_CREDENTIALS_JSON = get_env("FIREBASE_CREDENTIALS_JSON", "")
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

# The youngest a person may be to hold an account, enforced at sign-up and on every
# date-of-birth or age write. The owner's decision (2026-09-19); one number for every
# user and every build.
MINIMUM_AGE_YEARS = 14
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
# The filesystem media backend's root: development and the test suite. Production
# stores media in object storage (MEDIA_BACKEND=gcs, enforced at boot) — a machine's
# filesystem is ephemeral and a volume attaches to one machine only — so nothing is
# written here there, and fly.toml mounts no volume.
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
# A development placeholder. Production requires its own and refuses this domain
# (settings_production `_from_address_problem`): no provider sends for it.
DEFAULT_FROM_EMAIL = get_env('DEFAULT_FROM_EMAIL', 'noreply@trainingplatform.com')
# Seconds an SMTP connection may stall before the send fails. Django's default is None —
# wait for ever — and a mail server that accepts the connection and never answers then
# held its caller for good: the sign-up request, before the account emails moved to a
# worker (users/tasks.py), and now a worker slot. Every environment gets a bound.
EMAIL_TIMEOUT = get_int_env('EMAIL_TIMEOUT', 10)

# ========================
# CSRF Configuration
# ========================
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in get_env("CSRF_TRUSTED_ORIGINS", "http://localhost").split(",")
    if origin.strip()
]

# ========================
# Platform Escrow and revenue
#
# These four are the BASE of an address, not an address. The platform holds one ledger
# wallet PER CURRENCY, so `wallet.utils._ledger_address` derives
# `platform_escrow_syp@local`, `platform_escrow_usd@local` and the revenue pair from
# them. One wallet for both currencies was SIDE_BUGS S-001: the first payment ever
# taken decided the platform's currency for ever, and revenue in the other one sat in
# customers' balances.
# ========================
PLATFORM_ESCROW_EMAIL = get_env('PLATFORM_ESCROW_EMAIL', 'platform_escrow@local')
PLATFORM_ESCROW_USERNAME = get_env('PLATFORM_ESCROW_USERNAME', 'platform_escrow')
# Where the platform's commission lands (wallet.utils.get_platform_revenue_wallet).
PLATFORM_REVENUE_EMAIL = get_env('PLATFORM_REVENUE_EMAIL', 'platform_revenue@local')
PLATFORM_REVENUE_USERNAME = get_env('PLATFORM_REVENUE_USERNAME', 'platform_revenue')
# Where a trainer's withdrawal waits between the request and the operator's transfer
# (wallet.utils.get_payout_clearing_wallet; the spec's `payout_clearing` account). Its
# balance is the sum of every open withdrawal in that currency.
PLATFORM_PAYOUT_CLEARING_EMAIL = get_env('PLATFORM_PAYOUT_CLEARING_EMAIL',
                                         'platform_payout_clearing@local')
PLATFORM_PAYOUT_CLEARING_USERNAME = get_env('PLATFORM_PAYOUT_CLEARING_USERNAME',
                                            'platform_payout_clearing')

# ========================
# Trainer withdrawals (payouts/; the spec, section 8 "Trainer payouts"). Every number
# here is a PROPOSAL THE OWNER CONFIRMS: the hold and the SYP minimum are the design's
# (`15-trainer-money/TrainerEarnings` «يتاح خلال 3 أيام», `TrainerWithdraw` «الحد
# الأدنى 10,000»); the daily cap, the USD figures and the method count are nobody's yet.
# Per currency, like the agent caps (SIDE_BUGS S-059): one number for both would let a
# dollar trainer withdraw the pound figure in dollars.
# ========================
#: Money released from escrow to a trainer (a coaching payout) is "settling", not
#: withdrawable, for this many days after the release, to cover disputes. 0 = no hold.
PAYOUT_HOLD_DAYS = int(get_env('PAYOUT_HOLD_DAYS', '3'))
#: The smallest withdrawal, per currency.
PAYOUT_MIN_AMOUNT = {"SYP": _Decimal("10000"), "USD": _Decimal("100")}
#: The most one trainer may withdraw in one local day, per currency (requests not
#: cancelled or failed). A currency missing here withdraws nothing: fail closed.
PAYOUT_DAILY_CAP = {"SYP": _Decimal("500000"), "USD": _Decimal("5000")}
#: How many payout methods one trainer may keep.
PAYOUT_METHODS_MAX = 5

# ========================
# Who the receipt says issued it (spec §7: invoice number, date, item, amount,
# reference and THE PLATFORM'S LEGAL NAME). Read by subscription.receipts.
#
# PLATFORM_NAME is deliberately EMPTY by default: the trading name is a translated
# string in the code, so the Arabic receipt reads "يلا جيم" and the English one
# "Yalla Gym". A value here overrides both and is used verbatim, which is what a
# deployment trading under another name wants. The legal name and the contact lines
# are printed only when they are set — an unset one is left off the document rather
# than printed blank, so nothing is invented for a company that is not registered yet.
# ========================
PLATFORM_NAME = get_env('PLATFORM_NAME', '')
PLATFORM_LEGAL_NAME = get_env('PLATFORM_LEGAL_NAME', '')
PLATFORM_CONTACT_EMAIL = get_env('PLATFORM_CONTACT_EMAIL', '')
PLATFORM_WEBSITE = get_env('PLATFORM_WEBSITE', '')

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
    # Withdrawals: "one open request" and the daily cap are read to decide whether money
    # may move, so a cached read is a second withdrawal (payouts/services.py).
    'payouts_withdrawal', 'payouts_withdrawalevent', 'payouts_payoutmethod',
])

# Multipart bodies over FILE_UPLOAD_MAX_MEMORY_SIZE are spooled here before a view ever
# sees them. Production points it at a directory under the container's /tmp (fly.toml;
# docker-entrypoint.sh creates it for the app user): the spool is scratch space and a
# file lives only as long as its request. Paired with MAX_REQUEST_BODY_BYTES, which caps
# the body up front.
MAX_REQUEST_BODY_BYTES = int(get_env('MAX_REQUEST_BODY_BYTES', 15 * 1024 * 1024))
FILE_UPLOAD_TEMP_DIR = get_env('FILE_UPLOAD_TEMP_DIR', None) or None

# Media is served without per-request auth (the mobile client cannot attach a JWT
# to image loads), so URLs are signed and time-limited instead. Signing happens in
# SignedMediaStorage.url(), so every `.url` in every serializer is covered.
MEDIA_URL_SIGNING = get_env('MEDIA_URL_SIGNING', 'True').lower() in ('1', 'true', 'yes')
MEDIA_URL_TTL = int(get_env('MEDIA_URL_TTL', 24 * 3600))

# ========================
# Object storage
# ========================
# WHERE uploaded files live, chosen by the environment and by nothing else. There is no
# code branch on "am I in production": a developer runs the filesystem backend, the
# deploy sets MEDIA_BACKEND=gcs, and the same code paths run in both.
#
#   'local'  the filesystem (MEDIA_ROOT). Development and the test suite. In production
#            it is a Fly volume, which attaches to ONE machine — so a second web machine
#            serves 404 for everything the first one stored, and the volume is 1 GB.
#            `enforce_production_safety()` refuses to boot on it.
#   'gcs'    Google Cloud Storage (the owner, 2026-09-19: video lives on GCS).
#   's3'     any S3-compatible object store (Hetzner Object Storage on the first
#            deployment, because Google Cloud refuses a Syrian billing account). The
#            same private objects and expiring signed URLs:
#            training_platform/s3_storage.py.
MEDIA_BACKEND = get_env('MEDIA_BACKEND', 'local').strip().lower()

GS_BUCKET_NAME = get_env('GS_BUCKET_NAME', '')
GS_PROJECT_ID = get_env('GS_PROJECT_ID', '')
# The service account. Either the JSON of the key itself (a secret: it comes from
# `fly secrets set` or AWS Secrets Manager through `get_secret`, NEVER from a literal
# here or a file in the image), or a path for Application Default Credentials. The JSON
# is read lazily in training_platform/gcs_storage.py, so nothing is fetched — and no
# credential is needed — when MEDIA_BACKEND is 'local'.
GS_CREDENTIALS_SECRET_NAME = get_env('GS_CREDENTIALS_SECRET_NAME', 'GS_CREDENTIALS_JSON')
GS_LOCATION = get_env('GS_LOCATION', '')

# The S3-compatible store (MEDIA_BACKEND=s3). The address is the provider's S3 endpoint
# (https://fsn1.your-objectstorage.com for a Hetzner bucket in Falkenstein, and the
# region is then that location code, `fsn1`); empty means AWS itself. The key pair is
# two secrets, S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY, read lazily through
# `get_secret` in training_platform/s3_storage.py, never here.
S3_BUCKET_NAME = get_env('S3_BUCKET_NAME', '')
S3_ENDPOINT_URL = get_env('S3_ENDPOINT_URL', '').strip().rstrip('/')
S3_REGION_NAME = get_env('S3_REGION_NAME', '').strip()
# A prefix inside the bucket, like GS_LOCATION.
S3_LOCATION = get_env('S3_LOCATION', '')
# 'virtual' (https://bucket.host/key) or 'path' (https://host/bucket/key).
S3_ADDRESSING_STYLE = get_env('S3_ADDRESSING_STYLE', 'virtual').strip().lower()

# How long a signed URL to a private object lives. Short: the app asks the API for an
# object and gets a URL it uses immediately, so an hour is already generous, and a URL
# that leaks stops working within it. The filesystem backend keeps MEDIA_URL_TTL above,
# which is bucketed so the URL stays byte-identical and therefore cacheable.
MEDIA_SIGNED_URL_TTL = int(get_env('MEDIA_SIGNED_URL_TTL', 3600))

# The app's OWN pictures — the intro story, the banners, the empty states — are the same
# for every viewer and are fetched before anyone signs in. Nothing is private about them,
# and an expiring signature means no CDN or browser can cache one. With this on they are
# served as plain public URLs instead. OFF by default because it only works if the bucket
# actually grants public read on those objects; turning it on without that grant serves
# 403s. Trainer, exercise, program and dish media are never public: they belong to people.
MEDIA_PUBLIC_APP_CONTENT = get_env('MEDIA_PUBLIC_APP_CONTENT', 'False') == 'True'

# Video (mediakit). Both caps are enforced on the BYTES, not on the filename or the
# Content-Type header: `training_platform.file_security.process_uploaded_video`.
MEDIA_VIDEO_MAX_BYTES = int(get_env('MEDIA_VIDEO_MAX_BYTES', 64 * 1024 * 1024))
# A slot video is a demonstration loop or a short intro, not a lesson. The duration is
# read from the container when it declares one, and by ffprobe afterwards.
MEDIA_VIDEO_MAX_SECONDS = int(get_env('MEDIA_VIDEO_MAX_SECONDS', 90))

# The body cap above (15 MB) exists to stop a large POST filling the volume before any
# view runs, and no video fits under it. Rather than raise it for the whole API, ONE
# path is allowed its own ceiling — the per-file content checks still run downstream.
# + a megabyte of multipart overhead. Both spellings, because this middleware runs
# before the one that would append the slash (mediakit/urls.py says why).
LARGE_UPLOAD_PATHS = {
    '/api/v1/content/media/video': MEDIA_VIDEO_MAX_BYTES + 1024 * 1024,
    '/api/v1/content/media/video/': MEDIA_VIDEO_MAX_BYTES + 1024 * 1024,
    # A coach's own demonstration of a shared exercise takes the same video through
    # the same pipeline, so it needs the same ceiling (mediakit/urls_trainer.py).
    '/api/v1/trainer/exercise-media': MEDIA_VIDEO_MAX_BYTES + 1024 * 1024,
    '/api/v1/trainer/exercise-media/': MEDIA_VIDEO_MAX_BYTES + 1024 * 1024,
    # A client's form-check video in the chat (BACKEND_BUGS B-091) — the same video
    # guard, the same caps, so the same ceiling (chat/urls.py, chat/media.py).
    '/api/v1/chat/media': MEDIA_VIDEO_MAX_BYTES + 1024 * 1024,
    '/api/v1/chat/media/': MEDIA_VIDEO_MAX_BYTES + 1024 * 1024,
}

# Chat media: how much one person may put into storage (review 2026-09-24). A chat file
# is kept for as long as its message row, tombstone included, so without a bound one
# account could write 64 MB per request at the hourly rate limit into the paid bucket.
# Per SENDER, over a ROLLING 24 hours, counting every photo, voice note and video they
# sent in that window (taken-back ones too: their files stay) — decided before the upload
# is decoded or stored, answered 429 `media_quota_exceeded` (chat/media.py
# `require_media_quota`). Bytes are what each message keeps in storage
# (`chat.Message.size_bytes`).
# PROPOSED NUMBERS — the owner has not confirmed them: 60 files and 512 MB a day, and a
# voice note capped at 10 MB (a 90 s AAC note is ~1.5 MB; the 64 MB video cap made a
# voice note a way to store anything the audio sniffer accepts).
CHAT_MEDIA_DAILY_COUNT = int(get_env('CHAT_MEDIA_DAILY_COUNT', 60))
CHAT_MEDIA_DAILY_BYTES = int(get_env('CHAT_MEDIA_DAILY_BYTES', 512 * 1024 * 1024))
CHAT_VOICE_MAX_BYTES = int(get_env('CHAT_VOICE_MAX_BYTES', 10 * 1024 * 1024))

# WHERE an exercise's video/photo media may point (routine/views.py
# ExerciseAddMediaView). Any http(s) URL was accepted, so one coach could aim every
# one of their clients' players at a host nobody here controls — a tracker, a paywall,
# or a site that serves something else next month — and the app loaded it because the
# server called it the exercise's demonstration.
#
# Exact hostnames, comma-separated; an entry beginning with a dot matches that domain
# and its subdomains (`.example.com` allows `cdn.example.com`); an entry with a path,
# `host/prefix`, matches that exact host and only paths under `/prefix/`. The project's
# own object storage is always in the list — as ITS BUCKET, never as the bare shared
# host: `storage.googleapis.com` serves every GCS bucket in the world, so allowing the
# host allowed any coach's own public bucket (a tracker, a paywall, content that
# changes next month), which is the hole this list exists to close (SIDE_BUGS S-081).
# With no bucket configured nothing on GCS is allowed. Adding a third party — a video
# site, say — is a product decision the owner makes here, never a coach's, and it is
# the only way in.
def _exercise_media_allowed_hosts(configured, bucket, s3_bucket='', s3_endpoint=''):
    listed = tuple(part.strip().lower() for part in (configured or '').split(',')
                   if part.strip())
    bucket = (bucket or '').strip().strip('/').lower()
    ours = (f'storage.googleapis.com/{bucket}', f'{bucket}.storage.googleapis.com') \
        if bucket else ()
    # The S3 store, by the same rule: the project's BUCKET at that provider, in both
    # address styles, never the provider's bare host (which serves everyone's buckets).
    s3_bucket = (s3_bucket or '').strip().strip('/').lower()
    s3_host = urlsplit(s3_endpoint or '').netloc.lower()
    if s3_bucket and s3_host:
        ours += (f'{s3_host}/{s3_bucket}', f'{s3_bucket}.{s3_host}')
    return listed + ours


EXERCISE_MEDIA_ALLOWED_HOSTS = _exercise_media_allowed_hosts(
    get_env('EXERCISE_MEDIA_ALLOWED_HOSTS', ''), GS_BUCKET_NAME,
    S3_BUCKET_NAME, S3_ENDPOINT_URL)

# WHERE the evidence of a REFUSED upload is kept (training_platform.file_security).
# A prefix, not a path: the sample is written through the DEFAULT media storage below,
# so it lands wherever media lands — `MEDIA_ROOT/<prefix>` on the filesystem backend and
# a private prefix inside the same bucket when MEDIA_BACKEND=gcs. It was hardcoded to
# `MEDIA_ROOT/quarantine` (SIDE_BUGS S-018), which since object storage went live meant
# the container's ephemeral disk — and the Celery worker, which validates uploads too,
# has no volume at all, so the proof of what an account sent us died at the next restart.
# Objects under this prefix are PRIVATE: nothing ever hands out a URL to one, and the
# bucket grants no public read. Keep it out of any prefix served as public app content.
QUARANTINE_LOCATION = get_env('QUARANTINE_LOCATION', 'quarantine').strip('/')

# How many images one process decodes at once (training_platform/image_budget.py): each
# decode may hold DECODE_BUDGET_BYTES (128 MB), and three together took a 512 MB machine
# and every connection on it down (the production-readiness audit, 2026-09-25). The
# count is what the machine's memory (its cgroup limit, else its RAM) holds beside
# IMAGE_DECODE_RESERVED_BYTES — two on 512 MB. IMAGE_DECODE_SLOTS overrides it, and
# IMAGE_DECODE_MEMORY_BYTES states the memory where it cannot be read; the production
# boot check refuses slots the memory cannot hold. An upload that waits
# IMAGE_DECODE_WAIT_SECONDS without a slot is answered 503 `upload_busy`, with
# `Retry-After: IMAGE_DECODE_RETRY_AFTER_SECONDS`.
IMAGE_DECODE_SLOTS = get_int_env('IMAGE_DECODE_SLOTS', 0) or None
IMAGE_DECODE_MEMORY_BYTES = get_int_env('IMAGE_DECODE_MEMORY_BYTES', 0) or None
IMAGE_DECODE_RESERVED_BYTES = get_int_env('IMAGE_DECODE_RESERVED_BYTES', 192 * 1024 * 1024)
try:
    IMAGE_DECODE_WAIT_SECONDS = float(get_env('IMAGE_DECODE_WAIT_SECONDS', '2'))
except ValueError:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Environment variable 'IMAGE_DECODE_WAIT_SECONDS' must be "
                               "a number of seconds.")
IMAGE_DECODE_RETRY_AFTER_SECONDS = get_int_env('IMAGE_DECODE_RETRY_AFTER_SECONDS', 5)

# How many assets one trainer may hold on ONE exercise (mediakit/trainer_views.py).
# Storage and egress are paid per byte and a library is per trainer PER EXERCISE, so
# the bill is this number times every exercise times every coach. Replacing a filled
# slot is free; a fourth different slot answers `media_library_full`. Three is the
# poster, the loop and one cue — which is the demonstration the designs ask for.
MEDIA_TRAINER_EXERCISE_MAX_ASSETS = int(get_env('MEDIA_TRAINER_EXERCISE_MAX_ASSETS', 3))

# Trainer verification documents (trainer_verification/). An identity card is the most
# sensitive file the platform holds, so it has its own storage, below in STORAGES.
#
# PRIVATE_MEDIA_ROOT: the filesystem backend's root. OUTSIDE MEDIA_ROOT on purpose, so
# the public media route (`/media/...`) can never reach a document; they are read only
# through `/private-media/` with a signature that expires.
PRIVATE_MEDIA_ROOT = get_env('PRIVATE_MEDIA_ROOT', str(BASE_DIR / 'private_media'))
PRIVATE_MEDIA_URL = '/private-media/'
# The bucket on GCS. Empty means the media bucket, under a `private/` prefix; production
# refuses that combination when the media bucket serves public app content
# (settings_production.enforce_production_safety), because a bucket-level public grant
# would cover identity cards too.
VERIFICATION_GS_BUCKET_NAME = get_env('VERIFICATION_GS_BUCKET_NAME', '')
# The same, on the S3 store.
VERIFICATION_S3_BUCKET_NAME = get_env('VERIFICATION_S3_BUCKET_NAME', '')
# How long one signed document URL lives, in seconds. Short: the app and the console ask
# for a fresh one every time they draw the page.
VERIFICATION_DOCUMENT_URL_TTL = int(get_env('VERIFICATION_DOCUMENT_URL_TTL', 300))
# The largest document accepted (a phone photo of a card, or a certificate's PDF). Under
# MAX_REQUEST_BODY_BYTES, so no route needs a larger body ceiling.
VERIFICATION_DOCUMENT_MAX_BYTES = int(get_env('VERIFICATION_DOCUMENT_MAX_BYTES',
                                              10 * 1024 * 1024))
# A document the trainer replaced after an operator decided on it is kept this long for
# comparison, then deleted with its files (trainer_verification.purge_replaced_documents).
VERIFICATION_REPLACED_DOCUMENT_RETENTION_DAYS = int(get_env(
    'VERIFICATION_REPLACED_DOCUMENT_RETENTION_DAYS', 90))
# What the pending card promises ("usually within 3 business days").
VERIFICATION_REVIEW_BUSINESS_DAYS = int(get_env('VERIFICATION_REVIEW_BUSINESS_DAYS', 3))

STORAGES = {
    # Only signs when Django itself serves the files. With an external backend the
    # signature would be meaningless — that provider issues (and secures) its own URLs.
    'default': {'BACKEND': (
        'training_platform.gcs_storage.SignedGoogleCloudStorage'
        if MEDIA_BACKEND == 'gcs'
        else 'training_platform.s3_storage.SignedS3Storage'
        if MEDIA_BACKEND == 's3'
        else 'django.core.files.storage.FileSystemStorage'
        if USE_EXTERNAL_MEDIA_STORAGE
        else 'training_platform.media_storage.SignedMediaStorage'
    )},
    # Identity documents and certificates (trainer_verification/storage.py). NEVER the
    # media storage above: a private root outside MEDIA_ROOT on the filesystem, a private
    # prefix in VERIFICATION_GS_BUCKET_NAME on GCS (VERIFICATION_S3_BUCKET_NAME on an S3
    # store), and five-minute signed URLs on all three.
    'verification_documents': {'BACKEND': (
        'training_platform.gcs_storage.PrivateDocumentGoogleCloudStorage'
        if MEDIA_BACKEND == 'gcs'
        else 'training_platform.s3_storage.PrivateDocumentS3Storage'
        if MEDIA_BACKEND == 's3'
        else 'trainer_verification.storage.PrivateDocumentStorage'
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

# How long an AI training copy is kept (`AITrainingData.retain_until`), and the most it
# may ever be. The contract (`POST /api/ai/training-consent/`) and the app's wording
# promise "deleted after 365 days at most" (the owner, 2026-09-24), so a value above the
# ceiling refuses to boot rather than silently break that promise. Shorter is allowed.
AI_TRAINING_RETENTION_MAX_DAYS = 365
AI_TRAINING_RETENTION_DAYS = get_int_env('AI_TRAINING_RETENTION_DAYS', 365)
if not 1 <= AI_TRAINING_RETENTION_DAYS <= AI_TRAINING_RETENTION_MAX_DAYS:
    from django.core.exceptions import ImproperlyConfigured as _ImproperlyConfigured

    raise _ImproperlyConfigured(
        f"AI_TRAINING_RETENTION_DAYS must be 1..{AI_TRAINING_RETENTION_MAX_DAYS}, got "
        f"{AI_TRAINING_RETENTION_DAYS}: the app promises training copies are deleted "
        f"after {AI_TRAINING_RETENTION_MAX_DAYS} days at most.")

# The server secret AI training copies are keyed with (`ai_assistant/anonymise.py`):
# `subject_key` = HMAC-SHA256(this, user id) is the ONLY link from a copy to its person,
# and it is what lets "help us improve" off, `DELETE /api/ai/data/` and erasure find the
# copies. Without it nobody can turn a key back into a user. Empty here; local settings
# give a dev value, production requires a real one (`get_secret`, >= 32 characters).
# NEVER rotate it while copies exist: a new key cannot find the old copies, so opting out
# could no longer delete them. Delete every copy first.
AI_TRAINING_PSEUDONYM_KEY = get_env('AI_TRAINING_PSEUDONYM_KEY', '')
