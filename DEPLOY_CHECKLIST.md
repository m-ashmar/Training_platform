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

These are read straight from the process environment. `get_secret()` goes to AWS
Secrets Manager only when `AWS_SECRET_NAME` is set; leave it unset on Fly. Either way a
missing name crashes startup rather than falling back to a default.

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
fly ssh console -C "python manage.py load_food_catalogue --apply --wipe"
fly ssh console -C "python manage.py update_translation_fields"   # after any bulk import
fly ssh console -C "python manage.py check_catalogue"
curl -fsS https://<app>.fly.dev/api/auth/health/
```

**`load_food_catalogue` is the whole seed and it was missing from this list.** It builds the
catalogue, then calls `seed_food_units`, `seed_recipes` and `curate_food_allergens` itself, in
that order, because each needs the one before it. Running `seed_recipes` on its own — which is
what this step used to say — prints 105 lines of `skip ...: catalogue has no [...]`, seeds
nothing, and **exits 0**. The deploy then reports success and the app comes up with an empty
food catalogue: every plan generation has nothing to plan from.

`--wipe` is correct here and only here: on a first deploy there is nothing to wipe, and after
launch it would delete what clients have eaten and every preference and learned weight they
have accumulated. **Never run `--wipe` against a live database.** To converge an existing
catalogue on the file, run `load_food_catalogue --apply` without it.

`check_catalogue` asserts the invariants the engine depends on and exits non-zero if any of
them is broken — most importantly that every food has a *verified* allergen composition,
because the planner refuses to serve a client with a declared allergy anything it cannot
positively clear, and an uncurated catalogue means those clients get no plan at all.

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
