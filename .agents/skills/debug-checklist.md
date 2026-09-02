# Debug Checklist — Training Platform

Recurring issues that have been solved. Check this before investigating from scratch.

---

## 1. AWS Secrets Manager Auth Failure at Startup

**Symptom:** App crashes at startup with `Failed to fetch secrets from AWS Secrets Manager`.

**Root cause options:**
- Missing `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars
- Missing `AWS_REGION` (defaults to `us-east-1` — must match where your secret is stored)
- Missing `AWS_SECRET_NAME` (defaults to `training_platform/production`)
- IAM role not attached (if running on EC2/ECS/Fly with instance roles)
- `boto3` not installed (not in `requirements.txt` — install manually or add it)

**Fix:**
```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-east-1
export AWS_SECRET_NAME=training_platform/production
pip install boto3
```

**Local workaround:** Use `settings_local.py` — it skips AWS and reads from `.env` directly.

---

## 2. `ImproperlyConfigured` on Startup — Missing Secret

**Symptom:** `Required environment variable 'DJANGO_SECRET_KEY' is not set.`

**Root cause:** `get_secret()` / `require_env()` found no value.

**Fix:**
- For local: check `.env` file has the variable defined
- For prod: check `fly secrets list` or AWS Secrets Manager console
- The detection logic in `settings_secrets.py`:
  ```python
  is_prod = (
      os.environ.get("DJANGO_SETTINGS_MODULE") == "training_platform.settings_production"
      and os.environ.get("LOCAL_PROD_TEST") != "True"
  )
  ```
  Set `LOCAL_PROD_TEST=True` to test prod settings locally without AWS.

---

## 3. CORS Errors from Flutter/Frontend

**Symptom:** Browser or Flutter web reports CORS error.

**Root cause options:**
- Origin not in `CORS_ALLOWED_ORIGINS`
- Missing `CORS_ALLOW_CREDENTIALS = True` (it IS set — don't remove it)
- Preflight OPTIONS request blocked

**Fix:**
- Add origin to `CORS_ALLOWED_ORIGINS` env var (comma-separated)
- Local dev: `settings_local.py` already allows `localhost:3000` and `127.0.0.1:3000`
- Never add `CORS_ALLOW_ALL_ORIGINS = True` in production

**Note:** Native Flutter (Android/iOS) does NOT enforce CORS — this is browser-only.

---

## 4. OTP Not Working / Always Invalid

**Symptom:** `Invalid OTP code. Please check and try again.`

**Root cause options:**
- OTP expired (10-minute window)
- Multiple OTPs created — old ones are invalidated by `create_otp()` (sets `is_verified=True` on old ones)
- OTP stored as SHA-256 hash — never query by plaintext OTP in DB
- Attempt lockout: 5 failures → 15-minute lockout stored in `ratelimit_cache()` (Redis DB1)

**Debugging:**
```python
# In Django shell — check OTP record
from users.models import OTPVerification
OTPVerification.objects.filter(email='user@example.com', is_verified=False).order_by('-created_at')
```

**Check lockout:**
```python
from training_platform.cache import ratelimit_cache
rl = ratelimit_cache()
rl.get("otp_attempts:registration:user@example.com")  # None = not locked
```

---

## 5. JWT Auth Failures — 401 Unauthorized

**Symptom:** `detail: Authentication credentials were not provided` or `Token is invalid or expired`.

**Root cause options:**
- Access token expired (60-minute TTL) → need to refresh
- Using `Authorization: Bearer <token>` (correct) vs wrong header format
- RS256 key mismatch — `JWT_PRIVATE_KEY` signs, `JWT_PUBLIC_KEY` verifies; must be a matching RSA keypair
- Token blacklisted (refresh was rotated and old refresh was used)

**Fix for RS256 keypair generation:**
```bash
openssl genrsa -out jwt_private.pem 2048
openssl rsa -in jwt_private.pem -pubout -out jwt_public.pem
```
Store full PEM content (including `-----BEGIN...-----`) in secrets.

---

## 6. Migration Conflicts

**Symptom:** `CommandError: Conflicting migrations detected`.

**Fix:**
```bash
# Merge conflicting migrations
python manage.py makemigrations --merge

# Check which migrations are pending
python manage.py showmigrations

# If a migration was applied in prod but not in local (or vice versa)
python manage.py migrate --fake <app> <migration_number>
```

**Prevention:** Always run `python manage.py migrate --check` in CI.

---

## 7. Cachalot Returning Stale Data

**Symptom:** DB was updated but API still returns old data.

**Root cause:** `django-cachalot` transparently caches ORM queries and should auto-invalidate on write. If stale data appears:
- A query was made outside Django ORM (raw SQL, `cursor.execute()`) — cachalot can't detect this
- Cache version mismatch after schema change

**Fix:**
```python
from cachalot.api import invalidate_all
invalidate_all()   # nuclear option — clears all cachalot cache
```

Or invalidate per table:
```python
from cachalot.api import invalidate
invalidate('users_customuser')
```

**Note:** Financial/auth tables are in `CACHALOT_UNCACHABLE_TABLES` — they are NEVER cached.

---

## 8. Rate Limiting Blocking Legitimate Dev Requests

**Symptom:** 429 Too Many Requests during development.

**Root cause:** `RateLimitMiddleware` is always active. In DEBUG mode, limits are `10000/min` but they still exist.

**Fix for local dev:**
- Limits in `settings_local.py` are already relaxed to `1000/second` for DRF throttle
- Rate limit keys in Redis/LocMemCache expire; wait out the window OR:
```python
from training_platform.cache import ratelimit_cache
ratelimit_cache().clear()   # clears entire ratelimit cache
```

---

## 9. Push Notifications Not Arriving

**Symptom:** FCM notifications sent but not received.

**Root cause options:**
- `FIREBASE_CREDENTIALS_PATH` secret missing or wrong path → app crashes on `FirebaseNotificationService()` init
- FCM token not registered: `POST /api/auth/device-token/` must be called after login
- Multiple device tokens for same user — `send_multicast()` targets all
- Firebase project mismatch between credentials and `FIREBASE_PROJECT_ID`

**Debug:**
```python
from users.models import DeviceToken
DeviceToken.objects.filter(user_id=USER_ID)  # check token exists
```

---

## 10. Wallet / Financial Endpoint 403

**Symptom:** `You do not have permission to perform this action` on wallet endpoints.

**Root cause options:**
- `WALLET_DEV_MODE` was relied on — it's now removed from all business logic
- HMAC signature mismatch (wallet uses HMAC validation — must include correct signature)
- IP not in allowlist (wallet enforces IP allowlist)
- Agent API key invalid or not provided

**WALLET_DEV_MODE** is `False` everywhere in production — no bypass path exists. All validation is unconditional.

---

## 11. Admin Dashboard 404

**Symptom:** `/admin/` returns 404 or custom admin not loading.

**Fix:** The custom admin is at `/dj-admin/`. Django's built-in admin is also at `/admin/`.

---

## 12. Celery Tasks Not Running

**Symptom:** Background tasks queued but not executing.

**Root cause options:**
- `CELERY_TASK_ALWAYS_EAGER = True` in `settings_local.py` — tasks run synchronously in local dev (this is intentional, not a bug)
- Production: Celery worker not running (it's a separate process — not started by Daphne)
- Redis broker unreachable

**Check:**
```bash
# In prod
celery -A training_platform inspect active
celery -A training_platform inspect reserved
```

---

## 13. `preferred_language` / i18n Not Working

**Symptom:** API returns English despite `Accept-Language: ar` header, or user's language not applied in Celery tasks.

**Root cause options:**
- `LanguageResolutionMiddleware` resolves language from header → JWT → session → default. Order matters.
- In Celery workers: NO middleware runs — must use `LanguageContext.for_user_id(user_id)` explicitly
- `CACHE_VERSION` in `i18n.py` — if bumped, all language-keyed cache entries are automatically invalidated

**Fix:** In any cross-boundary code (Celery, signals, management commands):
```python
with LanguageContext.for_user_id(user_id):
    # all _() calls resolve in user's language here
    title = str(_("Notification title"))
```

---

## 14. WebSocket / Channels Not Connecting

**Symptom:** WebSocket connection refused or 400.

**Root cause options:**
- App running with `gunicorn` (WSGI) instead of `daphne` (ASGI) — WebSockets need ASGI
- Channel layer Redis on DB5 (`redis://host/5`) not reachable
- ASGI app entry point: must be `training_platform.asgi:application`

**Check:**
```python
# asgi.py
application = get_default_application()  # must include ProtocolTypeRouter
```
