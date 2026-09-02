# Django Conventions — Training Platform (Backend)

## Project Identity
- **Framework**: Django 5.1.3 + Django REST Framework 3.15.2
- **Runtime**: Python 3.12 / Daphne (ASGI, WebSocket support via Channels)
- **DB**: PostgreSQL 15 (local port 5433 via Docker)
- **Auth**: JWT (RS256, asymmetric) via `djangorestframework-simplejwt`
- **Custom User**: `users.CustomUser` — `AUTH_USER_MODEL = 'users.CustomUser'`

---

## App Structure

```
training_platform/         ← Django project config package
  settings_base.py         ← Shared config (all envs)
  settings_local.py        ← Dev overrides (DEBUG=True, LocMemCache, console email)
  settings_production.py   ← Prod config (DEBUG=False hardcoded, full hardening)
  settings_secrets.py      ← Zero-trust secret fetchers (require_env / get_secret)
  settings.py              ← Thin shim for backward compat only
  urls.py                  ← Root URL config
  middleware.py            ← All custom middleware
  cache.py                 ← Named Redis DB helpers (public/private/ratelimit/edamam)
  i18n.py                  ← LanguageContext, CACHE_VERSION, LanguageAwareAPIView

users/                     ← Auth, registration, OTP, trainer-client relations
routine/                   ← Workout plans & exercises
diet/                      ← Meal plans, nutrition, Edamam integration
subscription/              ← Plans and user subscriptions
challenges/                ← Fitness challenges
achievements/              ← Badges and milestones
analytics/                 ← Progress tracking and reports
social/                    ← Social features, Firebase push notifications
wallet/                    ← Financial wallet (escrow, transactions, audit log)
notifications/             ← Event-driven notification system (domain/ + channels/)
ai_assistant/              ← OpenAI GPT integration
admin_dashboard/           ← Custom admin UI (replaces default /admin/)
```

---

## Settings / Env Pattern

**Three-file split — hard rule:**
| File | `DJANGO_SETTINGS_MODULE` | Purpose |
|---|---|---|
| `settings_local.py` | `training_platform.settings_local` | Local dev |
| `settings_production.py` | `training_platform.settings_production` | Production |
| `settings_base.py` | (never used directly) | Shared base |

**Secret loading via `settings_secrets.py`:**
```python
from .settings_secrets import require_env, get_env, get_int_env, get_secret

SECRET_KEY = get_secret("DJANGO_SECRET_KEY")   # crashes if missing in prod
DB_PASSWORD = get_secret("DB_PASSWORD")        # crashes if missing in prod
```
- `get_secret()` → AWS Secrets Manager in production, `os.environ` locally
- `require_env()` → crashes on missing (no defaults allowed for required vars)
- `get_env(name, default)` → safe optional env var
- **NEVER add hardcoded defaults to secrets** — prod fails-closed intentionally

**Production safety gate** — called at WSGI/ASGI startup:
```python
# settings_production.py
enforce_production_safety()   # crashes if DEBUG=True or WALLET_DEV_MODE=True
```

---

## MIDDLEWARE Order (must not change)

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",           # MUST be first
    "training_platform.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "training_platform.middleware.LanguageResolutionMiddleware",
    "training_platform.middleware.RateLimitMiddleware",
    "training_platform.middleware.RequestLoggingMiddleware",
    "training_platform.middleware.DatabaseQueryCountMiddleware",
    "training_platform.middleware.CacheMiddleware",
    "training_platform.middleware.APIVersionMiddleware",
    "training_platform.middleware.ErrorHandlingMiddleware",
]
```

---

## DRF Serializer Patterns

**Always set `read_only_fields` explicitly:**
```python
class TrainerProfileSerializer(serializers.ModelSerializer):
    client_count = serializers.SerializerMethodField()
    profile_picture = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ['id', 'username', 'email', ...]
        read_only_fields = ['id', 'username', 'email', 'trainer_is_verified', 'is_active']

    def get_client_count(self, obj):
        return obj.get_client_count()

    def get_profile_picture(self, obj):
        request = self.context.get('request')
        if obj.profile_picture:
            return request.build_absolute_uri(obj.profile_picture.url) if request else obj.profile_picture.url
        return None
```

**Registration serializer extends `dj-rest-auth`:**
```python
class CustomRegisterSerializer(RegisterSerializer):
    # Save sets user.is_active = False — OTP activation required
```

**Login serializer removes username field:**
```python
class CustomLoginSerializer(LoginSerializer):
    username = None  # email-only login
```

**Serializer validation pattern** (role-scoped field stripping):
```python
def validate(self, attrs):
    if self.instance and self.instance.user_type == 'trainer':
        attrs.pop('assigned_trainer', None)   # trainers can't have this field
    return attrs
```

---

## View Patterns

**All views use `APIView` or `viewsets.GenericViewSet`:**
```python
class TrainerProfileView(APIView):
    permission_classes = [IsAuthenticated]  # ALWAYS explicit — no implicit

    def get(self, request):
        if not request.user.is_trainer:
            return Response({'error': _('...')}, status=status.HTTP_403_FORBIDDEN)
        ...
```

**Role checks use model properties:**
```python
request.user.is_trainer    # user_type == 'trainer'
request.user.is_client     # user_type == 'client'
request.user.is_admin      # user_type == 'admin'
```

**Public endpoints declare `AllowAny` explicitly:**
```python
class PublicTrainersListView(APIView):
    permission_classes = [AllowAny]   # explicit, not default
```

**Pagination:**
```python
from routine.views import StandardResultsSetPagination

paginator = StandardResultsSetPagination()
page = paginator.paginate_queryset(queryset, request, view=self)
if page is not None:
    return paginator.get_paginated_response({...})
```

---

## URL Namespacing

```python
# training_platform/urls.py
path('api/auth/', include('users.urls', namespace='users')),
path('api/routine/', include('routine.urls', namespace='routine')),
path('api/subscription/', include('subscription.urls', namespace='subscription')),
path('api/diet/', include('diet.urls', namespace='diet')),
path('api/wallet/', include('wallet.urls', namespace='wallet')),
path('api/ai/', include('ai_assistant.urls', namespace='ai_assistant')),

# users/urls.py
app_name = 'users'
path('token/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
```

Swagger/Redoc only available in DEBUG mode, admin-only in prod.

---

## Cache Pattern

**Never use `from django.core.cache import cache` directly — use named helpers:**
```python
from training_platform.cache import public_cache, private_cache, ratelimit_cache, edamam_cache

# Redis DB segments:
# DB0 → caches['default']   → sessions
# DB1 → caches['ratelimit'] → rate limiting
# DB2 → caches['public']    → public API cache
# DB3 → caches['private']   → per-user private cache
# DB4 → caches['edamam']    → Edamam API responses (24h)
# DB5 → channels_redis      → WebSocket channel layer (not Django CACHES)
```

**Cache key pattern (language + user scoped):**
```python
from training_platform.i18n import LanguageContext, CACHE_VERSION

key = LanguageContext.cache_key("trainer", user_id, "clients")
# → "trainer:42:clients:ar:v1"
```

**Cachalot transparent ORM cache:**
- Enabled globally via `django-cachalot`
- **Bypassed for financial/auth tables** (configured in `CACHALOT_UNCACHABLE_TABLES`):
  ```python
  CACHALOT_UNCACHABLE_TABLES = frozenset([
      'wallet_agentapikey', 'wallet_wallet', 'wallet_transaction',
      'auth_user', 'users_customuser', 'django_session', ...
  ])
  ```

---

## OTP / Auth Security

**OTP flow:**
1. `POST /api/auth/register/` → creates inactive user (`is_active=False`) → sends OTP
2. `POST /api/auth/verify-otp/` → verifies hash, activates user, returns JWT
3. `POST /api/auth/resend-otp/` → rate-limited (3/hour per email+IP)

**OTP implementation rules:**
- Generated with `secrets.randbelow(900000) + 100000` (CSPRNG)
- Stored as **SHA-256 hash** — never plaintext
- Verified with `secrets.compare_digest()` — constant-time
- 10-minute expiry
- **5-attempt lockout**, 15-minute cooldown window
- Anti-enumeration: all error paths return identical generic messages

**JWT:**
- Algorithm: `RS256` (asymmetric — private key signs, public key verifies)
- Access token: 60 minutes
- Refresh token: 7 days, rotated on use, blacklisted after rotation

---

## i18n Pattern

**Two supported languages: `en`, `ar` (Arabic is RTL)**

**Language resolution order in middleware:**
1. `Accept-Language` header
2. JWT claim `preferred_language`
3. Session user preference
4. `settings.LANGUAGE_CODE` (default: `en`)

**Use `LanguageContext` for all cross-boundary language activation:**
```python
# In views:
with LanguageContext.for_user(user):
    title = str(_("Some title"))

# In Celery tasks (no user object — fetch from DB):
with LanguageContext.for_user_id(user_id):
    send_notification(...)

# Views with DRF auth already done — use mixin:
class MyView(LanguageAwareAPIView, APIView):
    ...
```

**Bump `CACHE_VERSION` in `i18n.py` when serializer structure changes.**

---

## Migration Workflow

```bash
# Create migration
python manage.py makemigrations <app_name>

# Apply
python manage.py migrate

# Check for issues
python manage.py migrate --check    # exits non-zero if unapplied migrations exist

# In Docker/prod
python manage.py migrate --noinput  # run before starting Daphne (in CMD)
```

---

## Testing

**Framework**: `pytest-django` + `factory-boy` + `coverage`

**Run tests:**
```bash
DJANGO_SETTINGS_MODULE=training_platform.settings_local pytest

# With coverage
DJANGO_SETTINGS_MODULE=training_platform.settings_local coverage run -m pytest
coverage report
```

**Key config:**
```ini
# pytest.ini or pyproject.toml
[pytest]
DJANGO_SETTINGS_MODULE = training_platform.settings_local
```

**Fixtures pattern**: use `factory-boy` factories, not raw `User.objects.create_*`.

---

## Logging

**Per-app loggers (no cross-contamination):**
```python
import logging
logger = logging.getLogger(__name__)   # → uses app name as logger name

# Registered loggers: django, diet, routine, users, subscription, challenges
# Log files: logs/django.log, logs/diet.log, logs/users.log, etc.
```

**Rule: NO PII, tokens, or secrets in log messages.**

---

## Running Locally

```bash
# Activate venv
source .venv/bin/activate

# Start PostgreSQL (Docker)
docker-compose up -d db

# Set env
export DJANGO_SETTINGS_MODULE=training_platform.settings_local

# Run dev server
python manage.py runserver

# Or with Daphne (WebSocket support)
daphne -b 0.0.0.0 -p 8000 training_platform.asgi:application
```
