# Deploy Pipeline — Training Platform

## Platform
**Fly.io** — containerized Django (ASGI/Daphne) via Docker.

- App name: `training-platform-api`
- Region: `ams` (Amsterdam)
- Config: `fly.toml`

---

## Tech Stack at Deployment

| Component | Technology |
|---|---|
| App server | Daphne (ASGI — WebSocket support) |
| Container | Docker (`python:3.12-slim`) |
| Database | PostgreSQL 15 (external, not in Docker) |
| Cache/Queue | Redis (external) — segmented DBs 0-5 |
| Background tasks | Celery (separate process/worker) |
| Static files | WhiteNoise (served from app) |
| Secrets | AWS Secrets Manager (`training_platform/production`) |

---

## Docker Build

**Dockerfile summary:**
```dockerfile
FROM python:3.12-slim

# System deps: psycopg2, Pillow, python-magic
RUN apt-get install -y gcc libpq-dev libmagic1 libmagic-dev libjpeg-dev zlib1g-dev

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir daphne   # explicit ASGI server

COPY . .

ENV DJANGO_SETTINGS_MODULE=training_platform.settings_production
RUN python manage.py collectstatic --noinput || true

RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && daphne -b 0.0.0.0 -p 8000 training_platform.asgi:application"]
```

**Key decisions:**
- `collectstatic` runs at **build time** (not startup) — WhiteNoise serves static files
- Migrations run at **container startup** (before Daphne starts)
- Non-root user `appuser` for security
- ASGI entry point: `training_platform.asgi:application`

---

## Required Environment Variables (Fly secrets)

All secrets injected via `fly secrets set` or Fly dashboard. These are also stored in AWS Secrets Manager under `training_platform/production`.

### Mandatory (app crashes without these)
```bash
DJANGO_SECRET_KEY          # Django secret key (RS256 key rotation aware)
DJANGO_OLD_SECRET_KEY_1    # (optional) Previous key for hitless rotation
JWT_PRIVATE_KEY            # PEM RSA private key (signs JWTs)
JWT_PUBLIC_KEY             # PEM RSA public key (verifies JWTs)
DB_PASSWORD                # PostgreSQL password
DJANGO_ALLOWED_HOSTS       # Comma-separated: "training-platform-api.fly.dev"
REDIS_URL                  # Full Redis URL: "redis://..."
DB_NAME                    # Database name
DB_USER                    # Database user
DB_HOST                    # Database host
EMAIL_HOST_USER            # SMTP email address
EMAIL_HOST_PASSWORD        # SMTP password
CORS_ALLOWED_ORIGINS       # Comma-separated CORS origins
CSRF_TRUSTED_ORIGINS       # Comma-separated CSRF origins
AWS_SECRET_NAME            # "training_platform/production"
AWS_REGION                 # "us-east-1"
```

### Optional but expected
```bash
OPENAI_API_KEY
HUGGINGFACE_API_TOKEN
EDAMAM_APP_ID
EDAMAM_APP_KEY
FIREBASE_CREDENTIALS_PATH
FIREBASE_PROJECT_ID
REDIS_HOST / REDIS_PORT    # (if not using REDIS_URL)
NUM_PROXIES               # Default: 1
CELERY_BROKER_URL
CELERY_RESULT_BACKEND
```

### Production Guards (hardcoded, NOT configurable via env)
```python
DEBUG = False               # Cannot be overridden
WALLET_DEV_MODE = False     # Cannot be overridden — env WALLET_DEV_MODE=True crashes startup
```

---

## Deploy Steps (Fly.io)

```bash
# 1. Authenticate
fly auth login

# 2. (First time) Create app
fly launch --name training-platform-api --region ams --no-deploy

# 3. Set secrets
fly secrets set DJANGO_SECRET_KEY="..." JWT_PRIVATE_KEY="..." JWT_PUBLIC_KEY="..." ...

# 4. Deploy
fly deploy

# 5. Verify
fly status
fly logs
```

**Health check endpoint:** `GET /api/auth/health/` (must return 200)

---

## Rollback

```bash
# List releases
fly releases

# Roll back to previous
fly deploy --image <previous-image-tag>

# Emergency: scale to zero (stop app)
fly scale count 0
```

---

## Migration Safety

- Migrations run inside the container before Daphne starts.
- If migration fails → Daphne never starts → Fly marks deployment as failed → **previous version stays live**.
- Always run `python manage.py migrate --check` in CI to catch unapplied migrations before deployment.
- For data migrations with large tables: run migration manually on a maintenance window, then deploy code.

---

## Celery Workers (Background Tasks)

Celery is **not in the main Dockerfile CMD**. It should be a separate Fly machine or a separate process:

```bash
# Celery worker
celery -A training_platform worker -l info

# Celery beat (scheduled tasks)
celery -A training_platform beat -l info
```

**Scheduled tasks (from `settings_base.py`):**
- `close-idle-ai-sessions` — every 10 min
- `compute-daily-ai-insights` — daily
- `check-ai-cost-alert` — hourly

---

## Redis DB Segmentation (Production)

```
REDIS_URL/0 → sessions (caches['default'])
REDIS_URL/1 → rate limiting (caches['ratelimit'])
REDIS_URL/2 → public cache (caches['public'])
REDIS_URL/3 → private user cache (caches['private'])
REDIS_URL/4 → Edamam API cache (caches['edamam'])
REDIS_URL/5 → WebSocket channel layer (channels_redis — NOT Django CACHES)
```

---

## Local Dev Compose

```bash
# Starts only PostgreSQL on port 5433
docker-compose up -d db

# Django (uses settings_local.py + LocMemCache)
export DJANGO_SETTINGS_MODULE=training_platform.settings_local
python manage.py runserver
```

---

## CI/CD Gates (GitHub Actions)

Runs on every PR to `main`/`develop` and every push to `main`:

1. **Bandit** — Python static security analysis (high severity blocks)
2. **Safety** — dependency vulnerability scan
3. **Semgrep** — pattern-based SAST
4. **TruffleHog** — secret scanning (only-verified mode)
5. **Hardcoded secret grep** — patterns like `sk-proj-`, `django-insecure`, HuggingFace tokens

All gates must pass before merge.

---

## Pre-commit Hooks

```bash
# Install once
pip install pre-commit && pre-commit install
```

Hooks: `detect-secrets`, `bandit`, private key detection.

---

## Production Safety Enforcement

`wsgi.py` and `asgi.py` call `enforce_production_safety()` at startup:
```python
# settings_production.py
def enforce_production_safety():
    if DEBUG:
        raise SystemExit("DEBUG is True in production")
    if os.environ.get("WALLET_DEV_MODE") == "True":
        raise SystemExit("WALLET_DEV_MODE is True in production environment")
```

If this check fails → app process exits immediately → zero traffic served.
