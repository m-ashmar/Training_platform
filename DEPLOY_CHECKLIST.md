# Deploy Checklist

`enforce_production_safety()` crashes the process on a misconfiguration — deliberately.
This is the list of what it needs, because a missing variable previously produced a boot
crash with no indication of which one.

## 1. Create the volume (once)
```bash
fly volumes create training_data --region ams --size 1
```
The volume is mounted at `/data` and is root-owned at that moment — the image's own
`chown` ran at build time, against the directory the mount then hides. That is why no
upload could be written on the deployed app, including the multipart spool.
`docker-entrypoint.sh` chowns the mount at start and then drops to `appuser`; nothing
needs doing here beyond creating the volume.

## 1b. Create the media bucket and its service account (once)

Media lives in Google Cloud Storage (the owner, 2026-09-19) — **production refuses to
boot on the local filesystem backend**, because a Fly volume attaches to one machine
while several serve traffic.

```bash
# In the Google Cloud project:
gcloud storage buckets create gs://<bucket> --location=europe-west4 \
    --uniform-bucket-level-access            # no public allUsers grant
gcloud iam service-accounts create yalla-gym-media
gcloud storage buckets add-iam-policy-binding gs://<bucket> \
    --member=serviceAccount:yalla-gym-media@<project>.iam.gserviceaccount.com \
    --role=roles/storage.objectAdmin         # read + write objects; NOT bucket admin
gcloud iam service-accounts keys create key.json \
    --iam-account=yalla-gym-media@<project>.iam.gserviceaccount.com
```

The **key itself** is what matters: a V4 signed URL is signed with the service account's
private key, so an access token from a metadata server is not enough. Hand the JSON over
as a secret in step 2 and delete the local copy.

Set a lifecycle rule on the bucket if you want old originals expired; nothing in the app
deletes them except a row's own deletion.

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

# Media. `MEDIA_BACKEND=gcs` is REQUIRED: boot refuses 'local' (and refuses an unset
# value, which defaults to local). GS_PROJECT_ID is optional when the key carries it.
fly secrets set \
  MEDIA_BACKEND="gcs" \
  GS_BUCKET_NAME="<bucket>" \
  GS_PROJECT_ID="<gcp-project>" \
  GS_CREDENTIALS_JSON="$(cat path/to/key.json)"

# Push notifications. A PATH cannot be delivered to a Fly machine: `fly secrets set`
# sets environment variables, and the service-account key is outside the repository and
# excluded from the image. Hand over the JSON itself, base64 encoded:
fly secrets set FIREBASE_CREDENTIALS_JSON="$(base64 -i path/to/service-account.json)"
```

Leave `FIREBASE_CREDENTIALS_JSON` unset only if you intend to launch with push
notifications disabled — the app boots and logs a warning. Setting it to something
unusable fails the boot on purpose.

`.env` is NEVER part of a deploy. `.dockerignore` keeps it (and `key.md`, the Firebase
key under `_excluded/`, `db.sqlite3` and the git worktrees) out of the image, and
`manage.py` does not read it when `DJANGO_SETTINGS_MODULE` ends in `settings_production`.
Anything the secrets above do not set is simply unset in production, which is what the
startup checks are there to catch.

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

## 5b. Prove the bucket actually works (first deploy only)

The test suite runs on the filesystem backend and never touches the network, so the
GCS path is exercised for the first time here. Do it before anyone uploads anything.

```bash
fly ssh console -C "python manage.py shell -c \"
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from training_platform.media_storage import media_url
name = default_storage.save('healthcheck/probe.txt', ContentFile(b'ok'))
print(type(default_storage).__name__)          # SignedGoogleCloudStorage
print(media_url(default_storage.open(name)))   # a URL with X-Goog-Signature=...
default_storage.delete(name)\""
```

Three things have to be true: the class name is the GCS one (not `SignedMediaStorage` —
that means `MEDIA_BACKEND` did not reach the process), the URL carries a signature (no
signature means the credentials are an access token rather than a key), and fetching
that URL returns the bytes while fetching it without the query string returns 403.

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
| `mediakit.sweep_pending_videos` | hourly | finishes a video whose metadata task the worker lost; without it the asset stays `pending` for ever |

**Optional, and a real decision:** the image does not install `ffmpeg`. Without it a
video still uploads, plays and reaches `ready` — it just has no poster frame and no
measured duration or dimensions. To turn posters on, add `ffmpeg` to the `apt-get
install` line in the Dockerfile (about 250 MB) and redeploy; `mediakit.tasks` picks it
up with no other change, and `mediakit.sweep_pending_videos` does not backfill posters
for assets already marked `ready`.

Verify after deploy — a beat entry naming an unregistered task is skipped silently:
```bash
fly ssh console -C "celery -A training_platform inspect registered" | grep -c refresh_food_weights
```
