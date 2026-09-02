# Deploy Checklist

`enforce_production_safety()` crashes the process on a misconfiguration — deliberately.
This is the list of what it needs, because a missing variable previously produced a boot
crash with no indication of which one.

## 1. Create the volume (once)
```bash
fly volumes create training_data --region ams --size 1
```

## 2. Set the secrets
```bash
fly secrets set \
  DJANGO_SECRET_KEY="..." \
  DB_HOST="..." DB_NAME="..." DB_USER="..." DB_PASSWORD="..." \
  REDIS_URL="redis://<managed-redis>:6379" \
  CELERY_BROKER_URL="redis://<managed-redis>:6379/6" \
  CELERY_RESULT_BACKEND="redis://<managed-redis>:6379/7" \
  DJANGO_ALLOWED_HOSTS="api.example.com" \
  CORS_ALLOWED_ORIGINS="https://app.example.com" \
  CSRF_TRUSTED_ORIGINS="https://app.example.com" \
  EMAIL_HOST_USER="..." EMAIL_HOST_PASSWORD="..." \
  JWT_PRIVATE_KEY="..." JWT_PUBLIC_KEY="..." \
  FIELD_ENCRYPTION_KEY="..."
```

**The broker must not be on DB0–DB5.** Those are the six segmented caches (sessions,
rate limiting, public, private, Edamam, channels); boot refuses a broker that lands on
one of them, or one pointing at localhost.

## 3. Move the CI workflows into place (once)
```bash
git mv ci/ci.yml .github/workflows/ci.yml
git mv ci/security.yml .github/workflows/security.yml
```

## 4. Deploy
```bash
fly deploy
```
Two process groups start: `web` (scale-to-zero) and `worker` (`celery … --beat`, never
scales to zero). **Without the worker every `.delay()` is enqueued and never consumed** —
no notifications, no feed fan-out, no AI plans, no scheduled jobs.

## 5. Seed and verify
```bash
fly ssh console -C "python manage.py seed_recipes"
fly ssh console -C "python manage.py curate_food_allergens --apply"
fly ssh console -C "python manage.py update_translation_fields"   # after any bulk import
curl -fsS https://<app>.fly.dev/api/auth/health/
```

`update_translation_fields` matters: content imported outside the ORM fills only the base
column, and `.name` then resolves to `''`. That once left 542 of 554 exercises blank
through the API.

## 6. Confirm the background half is alive
```bash
fly logs -a <app> | grep -i celery          # worker + beat came up
fly ssh console -C "python manage.py retry_failed_notifications --dry-run"
```

## Scheduled jobs that must be running
| task | cadence | why it matters |
|---|---|---|
| `notifications.drain_dead_letter_queue` | hourly | replays failed notifications; the DLQ used to be write-only |
| `training_platform.privacy.purge_expired_personal_data` | daily | retention — analytics IP/user-agent at 180 days |
| `diet.planner.refresh_food_weights` | daily | turns consumption into planner ranking |
| `diet.tasks.generate_daily_advice` | 06:00 daily | the daily diet advice users receive |
| `ai_assistant.tasks.check_daily_cost` | hourly | the AI budget kill-switch's monitor |
| `ai_assistant.tasks.close_idle_sessions` | 10 min | closes abandoned chat sessions |
| `ai_assistant.tasks.compute_all_user_insights` | daily | user insight aggregation |

Verify after deploy — a beat entry naming an unregistered task is skipped silently:
```bash
fly ssh console -C "celery -A training_platform inspect registered" | grep -c refresh_food_weights
```
