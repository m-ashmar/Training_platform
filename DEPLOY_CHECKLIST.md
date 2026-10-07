# Deploy Checklist

The runbook for putting the API on Fly.io, written from the final configuration of
2026-09-28 (Django 5.2 LTS, three process groups, object-storage media, no volume). Read
it top to bottom the first time; later deploys are section 5 alone.

`enforce_production_safety()` (`training_platform/settings_production.py`) stops the
process at boot when the configuration cannot work. It prints every problem at once, as
`PRODUCTION SAFETY CHECK FAILED:` followed by one line per problem. When a deploy fails,
read that block first: it names the variable.

| Section | What |
|---|---|
| 1 | What the owner chooses before anything else |
| 2 | One-time provisioning: apps, Postgres, Redis, buckets, email, keys |
| 3 | Every variable: required or not, default, what it does |
| 4 | Keys: how to make them, who holds them, what losing or leaking each costs |
| 5 | Deploy |
| 6 | First-run data |
| 7 | Verify |
| 8 | Monitoring and the scheduled jobs |
| 9 | Backup and restore |
| 10 | Staging |
| 11 | CI, the image, the lock |

## 1. What the owner chooses first

Nothing below can start until these exist. The code is provider-neutral for each one.

| Choice | Needs to offer | Feeds |
|---|---|---|
| Managed Postgres 15+ | TLS; `max_connections` for section 2.2's budget, or a pooler; daily snapshots and point-in-time recovery | `DB_*` |
| Managed Redis | `maxmemory-policy noeviction` on the broker's server; TLS if reached over the internet | `REDIS_URL` or the four role URLs |
| SMTP provider (SES, Postmark, SendGrid, Mailgun, Brevo, Resend, a Workspace mailbox) | a verified sending domain (SPF + DKIM) | `EMAIL_*`, `DEFAULT_FROM_EMAIL` |
| Google Cloud project | Cloud Storage and a service account key | `GS_*` |
| Firebase project | Cloud Messaging and a service account key | `FIREBASE_CREDENTIALS_JSON` |
| Sentry project (optional, strongly advised) | a DSN | `SENTRY_DSN` |
| Uptime monitor (any) | polls a URL every minute, alerts on non-200 | section 8 |
| Domains | `api.` for the API; the public pages / web hub host; optionally an ops console host | `DJANGO_ALLOWED_HOSTS`, `CORS_*`, `CSRF_*`, `PUBLIC_PAGES_BASE_URL`, `OPS_CONSOLE_BASE_URL` |
| ShamCash merchant account | account id, API token, webhook secret (after approval) | `SHAMCASH_*` |
| A password manager or vault the owner controls | a shared vault for the keys in section 4 | section 4 |

## 2. One-time provisioning

### 2.1 The Fly apps
```bash
fly apps create <app>                 # production; put the name in fly.toml `app =`
fly apps create <app>-staging         # section 10
```
There is NO volume: `fly.toml` has no `[mounts]`, and media lives in Cloud Storage. The
multipart upload spool is `/tmp/uploads` inside each machine (`FILE_UPLOAD_TEMP_DIR` in
`fly.toml`); `docker-entrypoint.sh` creates it for the app user and then drops root.

### 2.2 Postgres
- Reached over the internet: `DB_SSLMODE=require` (or `verify-full` with the provider's CA).
- The connection budget, kept by the operator: Postgres `max_connections` (or the
  pooler's client limit) >= web machines x 100 (`hard_limit` in `fly.toml`) + worker
  concurrency (2) x worker machines + about 5 (beat, the release migration, a console).
  Two web machines need about 210 without a pooler.
- Behind a pooler (PgBouncer, a provider's pooled endpoint): set `DB_POOLER` to
  `transaction` or `session`, and only then `DB_CONN_MAX_AGE` above 0. Under a
  transaction pooler the database's own timezone MUST be UTC
  (`ALTER DATABASE <name> SET timezone TO 'UTC'`), or Django's session-level
  `SET TIME ZONE` leaks through the pooler to other clients.
- Without a pooler leave `DB_CONN_MAX_AGE` unset (0). Boot refuses a kept connection
  with no pooler, because under Daphne a kept connection is never reused.
- Turn Postgres's JIT off for the app's database, once:
  `ALTER DATABASE <name> SET jit = off` (or `ALTER ROLE <user> SET jit = off` when the
  provider does not let the user alter the database). JIT is on by default, and the
  app's wide querysets cross its cost threshold: the routines list took 3 to 20 seconds
  for ONE routine, almost all of it JIT compilation, and 8 ms with JIT off
  (BACKEND_BUGS B-136). Set here rather than as a connection option, because a
  transaction pooler refuses or drops the `options` startup parameter. Check it with
  `SHOW jit;` from the app's user: `off`.

### 2.3 Redis
One server, or one per role; `training_platform/redis_layout.py` explains both.

| Layout | Set |
|---|---|
| One Redis with numbered databases | `REDIS_URL=redis://<host>:6379` (no database in it). Caches take /0 to /4, the channel layer /5, the broker /6, results /7 |
| A Redis per role (e.g. managed Redis that serves only database 0) | `REDIS_CACHE_URL`, `REDIS_CHANNELS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, each a full URL |

Boot refuses: any role on localhost; the broker in the same database as a cache; a
`rediss://` broker or result store without `?ssl_cert_reqs=required`; a `REDIS_URL` that
already names a database. The broker's server MUST be `maxmemory-policy noeviction`
(nothing in the app can check it): an evicting broker drops queued jobs silently.

### 2.4 Media buckets and the service account
Production refuses to boot on the local filesystem backend (`MEDIA_BACKEND` must be
`gcs`).

```bash
gcloud storage buckets create gs://<bucket> --location=europe-west4 \
    --uniform-bucket-level-access            # no public allUsers grant
gcloud iam service-accounts create yalla-gym-media
gcloud storage buckets add-iam-policy-binding gs://<bucket> \
    --member=serviceAccount:yalla-gym-media@<project>.iam.gserviceaccount.com \
    --role=roles/storage.objectAdmin         # objects only, NOT bucket admin
gcloud iam service-accounts keys create key.json \
    --iam-account=yalla-gym-media@<project>.iam.gserviceaccount.com
```

The KEY is what matters: a V4 signed URL is signed with the service account's private
key, so a metadata-server token cannot sign. Boot reads the key and refuses one without
`private_key` and `client_email`. Put `key.json` in the vault, set it in section 5, and
delete the local copy.

Trainer identity documents (`trainer_verification/`) are stored in the media bucket
unless `VERIFICATION_GS_BUCKET_NAME` names another. If `MEDIA_PUBLIC_APP_CONTENT=True`
makes the media bucket public, a separate private verification bucket is REQUIRED (boot
refuses otherwise): uniform access makes a public grant cover the whole bucket.

Retention settings on the buckets are in section 9.

### 2.5 Email
Verify the sending domain at the provider (SPF and DKIM records in DNS) BEFORE the first
deploy: the sign-up code is the only way to open an account. Boot refuses a From address
on a placeholder domain (`example.com`, `trainingplatform.com`, `.example`, `.invalid`,
`.localhost`), TLS and SSL both on or both off, port 465 without SSL and port 587 with
SSL. Account emails are sent by the worker, never by the request.

### 2.6 Keys
Generate every key in section 4 once, into the vault, before the first deploy.

## 3. Every variable

Legend: **R** = boot refuses without it. **R*** = required under a condition. Blank = optional,
the default is right for launch. "Secret" = set with `fly secrets set`; the rest may also
go in `fly.toml [env]`, but a secret there is readable in the repository.

### 3.1 Core
| Variable | | Default | Secret | What |
|---|---|---|---|---|
| `DJANGO_SETTINGS_MODULE` | | `training_platform.settings_production` | | Set by the image and `fly.toml`. Never change |
| `DJANGO_SECRET_KEY` | R | | yes | At least 32 characters, not a `django-insecure` placeholder. Section 4 |
| `DJANGO_OLD_SECRET_KEY_1` | | | yes | The previous secret key during a rotation (section 4) |
| `DJANGO_ALLOWED_HOSTS` | R | | | Comma separated, e.g. `api.yallagym.com`. No wildcard, no localhost |
| `CORS_ALLOWED_ORIGINS` | R | | | Comma separated full origins, e.g. `https://yallagym.com` (the web hub). No wildcard, no localhost |
| `CSRF_TRUSTED_ORIGINS` | R | | | Comma separated full origins that post forms: the API host (console) and the hub |
| `TIME_ZONE` | | `Asia/Damascus` | | Local dates (a training day, a coaching month). Scheduled jobs run on UTC (section 8) |
| `LOG_LEVEL` | | `INFO` | | One JSON line per record on stdout |
| `NUM_PROXIES` | | `1` | | Proxies of ours in front of Daphne (Fly's edge is one). Add one per CDN or load balancer; every client IP the app records depends on it |
| `TRUSTED_PROXY_IPS` | | | | Comma separated, when a known proxy's address must be trusted as well |
| `AWS_SECRET_NAME`, `AWS_REGION` | | | | Leave UNSET on Fly. When set, `get_secret()` reads secrets from AWS Secrets Manager instead of the environment |

### 3.2 Database
| Variable | | Default | Secret | What |
|---|---|---|---|---|
| `DB_HOST`, `DB_NAME`, `DB_USER` | R | | | |
| `DB_PASSWORD` | R | | yes | |
| `DB_PORT` | | `5432` | | |
| `DB_SSLMODE` | | libpq's default | | `require` for a managed Postgres over the internet |
| `DB_POOLER` | | | | `transaction` or `session` when a pooler fronts Postgres (section 2.2) |
| `DB_CONN_MAX_AGE` | | `0` | | Above 0 only with `DB_POOLER` |

### 3.3 Redis
| Variable | | Default | Secret | What |
|---|---|---|---|---|
| `REDIS_URL` | R* | | yes | Required while any role below is unset. Server only, no `/n` |
| `REDIS_CACHE_URL` | | `REDIS_URL/0..4` | yes | All five caches, kept apart by key prefix |
| `REDIS_CHANNELS_URL` | | `REDIS_URL/5` | yes | The WebSocket channel layer |
| `CELERY_BROKER_URL` | | `REDIS_URL/6` | yes | The job queue. `noeviction` |
| `CELERY_RESULT_BACKEND` | | `REDIS_URL/7` | yes | |

### 3.4 Email
| Variable | | Default | Secret | What |
|---|---|---|---|---|
| `EMAIL_HOST` | R | | | The provider's SMTP host |
| `EMAIL_HOST_USER` | R | | | The SMTP user (an address, or a key id such as `apikey`) |
| `EMAIL_HOST_PASSWORD` | R | | yes | |
| `DEFAULT_FROM_EMAIL` | R | | | `Yalla Gym <no-reply@your-domain>` on the verified domain |
| `EMAIL_PORT` | | `587` (`465` with SSL) | | |
| `EMAIL_USE_TLS` / `EMAIL_USE_SSL` | | STARTTLS | | `EMAIL_USE_SSL=True` for 465 |
| `EMAIL_TIMEOUT` | | `10` | | Seconds; a stalled server must not hold a worker |
| `SERVER_EMAIL` | | `DEFAULT_FROM_EMAIL` | | |

### 3.5 Media
| Variable | | Default | Secret | What |
|---|---|---|---|---|
| `MEDIA_BACKEND` | R | `local` (refused) | | Must be `gcs` |
| `GS_BUCKET_NAME` | R | | | |
| `GS_CREDENTIALS_JSON` | R | | yes | The service account key file, as JSON text |
| `GS_PROJECT_ID` | | from the key | | |
| `GS_LOCATION` | | bucket root | | A prefix inside the bucket |
| `GS_CREDENTIALS_SECRET_NAME` | | `GS_CREDENTIALS_JSON` | | Only to read the key under another name |
| `VERIFICATION_GS_BUCKET_NAME` | R* | the media bucket | | Required when `MEDIA_PUBLIC_APP_CONTENT=True` |
| `MEDIA_PUBLIC_APP_CONTENT` | | `False` | | Serve app content from a public bucket instead of signed URLs |
| `MEDIA_URL_SIGNING`, `MEDIA_URL_TTL`, `MEDIA_SIGNED_URL_TTL` | | `True`, 86400, 3600 | | |
| `MEDIA_VIDEO_MAX_BYTES`, `MEDIA_VIDEO_MAX_SECONDS` | | 64 MB, 90 | | |
| `MEDIA_TRAINER_EXERCISE_MAX_ASSETS` | | `3` | | |
| `CHAT_MEDIA_DAILY_COUNT`, `CHAT_MEDIA_DAILY_BYTES`, `CHAT_VOICE_MAX_BYTES` | | 60, 512 MB, 10 MB | | Per person per day; an owner number |
| `VERIFICATION_DOCUMENT_MAX_BYTES`, `VERIFICATION_DOCUMENT_URL_TTL` | | 10 MB, 300 | | |
| `VERIFICATION_REPLACED_DOCUMENT_RETENTION_DAYS` | | `90` | | Replaced identity documents are purged after this |
| `VERIFICATION_REVIEW_BUSINESS_DAYS` | | `3` | | The review time the trainer is told |
| `EXERCISE_MEDIA_ALLOWED_HOSTS` | | | | Hosts exercise media URLs may point at |
| `MAX_REQUEST_BODY_BYTES` | | 15 MB | | Refused before Django buffers it |
| `FILE_UPLOAD_TEMP_DIR` | | `/tmp/uploads` (`fly.toml`) | | The multipart spool |
| `QUARANTINE_LOCATION` | | `quarantine` | | |
| `MEDIA_ROOT`, `PRIVATE_MEDIA_ROOT`, `USE_EXTERNAL_MEDIA_STORAGE` | | | | Development only; leave unset |

### 3.6 Keys (section 4 says what each protects)
| Variable | | Secret | Make it with |
|---|---|---|---|
| `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEY` | R | yes | `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt.pem` and `openssl rsa -in jwt.pem -pubout -out jwt.pub`; set as `"$(cat jwt.pem)"` |
| `FIELD_ENCRYPTION_KEY` | R | yes | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. A comma list during a rotation |
| `AI_TRAINING_PSEUDONYM_KEY` | R | yes | `openssl rand -hex 32`. At least 32 characters and not the secret key |
| `AGENT_APIKEY_ENC_KEY` | | yes | A Fernet key, as above. Without it the agents' API secrets are encrypted under a key derived from `DJANGO_SECRET_KEY` |

### 3.7 Push, AI, food data
| Variable | | Default | Secret | What |
|---|---|---|---|---|
| `FIREBASE_CREDENTIALS_JSON` | | | yes | `"$(base64 -i service-account.json)"`. Unset: the app boots with push off and logs a warning; set to something unusable: boot fails |
| `FIREBASE_PROJECT_ID` | | from the key | | |
| `FIREBASE_CREDENTIALS_PATH` | | | | Development only; a path cannot reach a Fly machine |
| `OPENAI_API_KEY` | | | yes | The AI features' provider key |
| `AI_PROVIDER`, `OPENAI_MODEL`, `AI_ASSISTANT_MODEL` | | `openai`, `gpt-4o-mini` | | The provider's name is never shown to users (owner, 2026-09-24) |
| `HUGGINGFACE_API_TOKEN` | | | yes | Only for the Hugging Face provider |
| `AI_TRAINING_RETENTION_DAYS` | | `365` | | Anonymous training copies are purged after this |
| `EDAMAM_APP_ID`, `EDAMAM_APP_KEY` | | | yes | Only with `DIET_EXTERNAL_FOOD_SEARCH=True` |
| `DIET_EXTERNAL_FOOD_SEARCH`, `DIET_PLANNER_STRICT` | | `False` | | |

### 3.8 Payments and money
| Variable | | Default | Secret | What |
|---|---|---|---|---|
| `SHAMCASH_API_TOKEN` | | | yes | Unset before ShamCash approval: boot proceeds, and checkout uses the manual-transfer step |
| `SHAMCASH_ACCOUNT_ID` | R* | | | Required when the token is set |
| `SHAMCASH_WEBHOOK_SECRET` | | | yes | At least 16 characters when set |
| `SHAMCASH_API_URL`, `SHAMCASH_INITIATE_PATH`, `SHAMCASH_SIGNATURE_HEADER`, `SHAMCASH_TIMESTAMP_HEADER` | | | | From ShamCash after approval (hosted mode) |
| `GATEWAY_MODE` | | `production` | | Must be `production` when the token is set. `sandbox` only on staging (section 10) |
| `SHAMCASH_SANDBOX_*` | | | yes | Staging only |
| `PAYMENT_DEBUG` | | `False` | | Boot refuses `True` |
| `PAYMENT_GATEWAY_DEGRADED_AFTER_FAILURES`, `..._AFTER_SECONDS` | | 2, 60 | | When the app starts showing the gateway-down state |
| `PLATFORM_CURRENCY` | | `SYP` | | |
| `EXCHANGE_RATE_MAX_AGE_DAYS` | | `7` | | |
| `PAYOUT_HOLD_DAYS` | | `3` | | Owner number |
| `AGENT_DEFAULT_DAILY_LIMIT`, `..._MONTHLY_LIMIT`, `..._PER_TRANSACTION_LIMIT`, `..._RECEIVABLE_CEILING` | | 250000, 5000000, 50000, 600000 | | Owner numbers (SYP) |
| `PLATFORM_ESCROW_*`, `PLATFORM_REVENUE_*`, `PLATFORM_PAYOUT_CLEARING_*` (`_EMAIL`, `_USERNAME`) | | `platform_*@local` | | The platform's system wallets. Set once, before the first money moves; never change after |
| `WALLET_DEV_MODE` | | `False` | | Boot refuses `True` |

### 3.9 Public pages, app links, identity, console
| Variable | | Default | What |
|---|---|---|---|
| `PUBLIC_PAGES_BASE_URL` | R | | Where the privacy policy, terms and account-deletion page are reached, e.g. `https://yallagym.com`. The capabilities answer hands these URLs to every build, and the store listings name the same pages. Boot refuses it unset, not https, or a local address (B-174) |
| `APP_LINKS_ANDROID_PACKAGES` | | | Comma separated package names. Empty: `/.well-known/assetlinks.json` answers 404 `app_links_not_configured` |
| `APP_LINKS_ANDROID_SHA256_FINGERPRINTS` | | | The SHA-256 of every release-signing certificate (the direct APK's key, and Play's app-signing key once there is a Play build) |
| `APP_LINKS_IOS_TEAM_ID`, `APP_LINKS_IOS_BUNDLE_IDS` | | | For `/.well-known/apple-app-site-association` |
| `APP_LINKS_PATHS` | | | The hub paths the app opens, e.g. `/app/*` |
| `PLATFORM_NAME`, `PLATFORM_LEGAL_NAME`, `PLATFORM_CONTACT_EMAIL`, `PLATFORM_WEBSITE` | | | On receipts and emails. Unset lines are left off; `PLATFORM_NAME` unset means "يلا جيم" / "Yalla Gym" |
| `OPS_CONSOLE_BASE_URL` | | the API host | The console's own origin, when it has one |
| `OPS_QR_LOGIN_ENABLED` | | `False` | Signing in to the console with a link from the operator's phone. An owner decision; off answers `console_login_disabled` |

### 3.10 Monitoring
| Variable | | Default | What |
|---|---|---|---|
| `SENTRY_DSN` | | | Errors, plus a cron monitor per money job. Unset: a loud warning at every boot. Set but unusable: boot fails |
| `SENTRY_ENVIRONMENT` | | `production` | `staging` on staging |
| `SENTRY_TRACES_SAMPLE_RATE` | | `0.05` | |
| `BACKGROUND_HEARTBEAT_MAX_AGE_SECONDS` | | `180` | When the background health check turns 503 |

### 3.11 Tunables (leave at the default unless a measurement says otherwise)
`CELERY_TASK_SOFT_TIME_LIMIT` (300), `CELERY_TASK_TIME_LIMIT` (360),
`PERIODIC_BATCH_SIZE` (200), `PERIODIC_BATCH_SECONDS` (120, at most half the soft
limit), `PERIODIC_SCAN_SIZE` (5000), `PRIVACY_BATCH_ROWS` (1000),
`PRIVACY_EXPORT_SPOOL_BYTES` (8 MB, at most 64 MB), `STORAGE_DELETE_BATCH` (100),
`IMAGE_DECODE_SLOTS` (0 = sized from the machine's memory),
`IMAGE_DECODE_MEMORY_BYTES` (0 = read from the machine), `IMAGE_DECODE_RESERVED_BYTES`
(192 MB), `IMAGE_DECODE_WAIT_SECONDS` (2), `IMAGE_DECODE_RETRY_AFTER_SECONDS` (5).
Boot refuses combinations that cannot fit the machine (`heavy_work_problems`).

## 4. Keys: making, custody, loss, leak, rotation

**Custody.** Fly secrets are WRITE-ONLY: `fly secrets list` shows digests, never values.
The vault is therefore the only copy of every key. Two people the owner trusts hold
access to it. `FIELD_ENCRYPTION_KEY` and `AI_TRAINING_PSEUDONYM_KEY` also get an
offline copy (printed or on an encrypted drive, kept apart from any backup), because
losing either one destroys data that no backup brings back. A key is NEVER stored next
to a database dump: a dump plus `FIELD_ENCRYPTION_KEY` is plaintext health data.

| Key | Protects | If LOST | If LEAKED | Rotation |
|---|---|---|---|---|
| `FIELD_ENCRYPTION_KEY` | Health data columns (injuries, notes) at rest, in the database and in every backup | That data is unreadable for ever, in production and in every backup | A database dump or backup becomes readable: rotate, then treat it as a data breach | Prepend the new key (`new,old`): new writes use it, both read. Rows written under the old key keep needing it. No command yet re-saves every row, so NEVER drop an old key (SIDE_BUGS S-233) |
| `AI_TRAINING_PSEUDONYM_KEY` | The link from an anonymous AI training copy to its person, used only to delete the copies when the person turns "help us improve" off | Existing copies can no longer be found to delete | Copies could be re-linked to people | NEVER while copies exist. Delete every copy first |
| `DJANGO_SECRET_KEY` | Sessions, sign-up and reset code hashes, the OTP email seal, the agents' API secrets (when `AGENT_APIKEY_ENC_KEY` is unset), the keyed digests of saved ShamCash payer and payout addresses | Pending codes and resets die (minutes). Agents' API secrets become unreadable. Saved address digests stop matching | Sessions and reset tokens forgeable: rotate at once | Put the old value in `DJANGO_OLD_SECRET_KEY_1`, set the new one, deploy. The OTP seal and agent secrets honour the old key; the address digests do NOT (SIDE_BUGS S-232) |
| `AGENT_APIKEY_ENC_KEY` | Agents' API secrets at rest | Every agent needs a new API key | Rotate, then re-issue agents' keys | Changing it makes existing agent secrets unreadable: re-issue |
| `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` | Every access and refresh token | Everyone signs in again | Tokens forgeable: rotate at once | Rotation signs everyone out |
| `GS_CREDENTIALS_JSON` | Media storage and signed URLs | Media 500s until a new key is set | Revoke the key in IAM, make a new one | Any time: new key, set, deploy, revoke the old |
| `FIREBASE_CREDENTIALS_JSON` | Push | Push stops | Revoke in the Firebase console | Any time |
| `DB_PASSWORD`, `EMAIL_HOST_PASSWORD`, `SHAMCASH_*`, `OPENAI_API_KEY`, `SENTRY_DSN` | The provider account | Reset at the provider | Rotate at the provider | Any time: set, deploy |

## 5. Deploy
```bash
fly secrets set DJANGO_SECRET_KEY="…" JWT_PRIVATE_KEY="$(cat jwt.pem)" \
  JWT_PUBLIC_KEY="$(cat jwt.pub)" FIELD_ENCRYPTION_KEY="…" \
  AI_TRAINING_PSEUDONYM_KEY="…" AGENT_APIKEY_ENC_KEY="…" \
  DB_HOST="…" DB_NAME="…" DB_USER="…" DB_PASSWORD="…" DB_SSLMODE="require" \
  REDIS_URL="rediss://…" \
  EMAIL_HOST="…" EMAIL_HOST_USER="…" EMAIL_HOST_PASSWORD="…" \
  DEFAULT_FROM_EMAIL="Yalla Gym <no-reply@your-domain>" \
  DJANGO_ALLOWED_HOSTS="api.your-domain" \
  CORS_ALLOWED_ORIGINS="https://your-domain" \
  CSRF_TRUSTED_ORIGINS="https://api.your-domain,https://your-domain" \
  PUBLIC_PAGES_BASE_URL="https://your-domain" \
  MEDIA_BACKEND="gcs" GS_BUCKET_NAME="<bucket>" GS_CREDENTIALS_JSON="$(cat key.json)" \
  FIREBASE_CREDENTIALS_JSON="$(base64 -i service-account.json)" \
  SENTRY_DSN="…" OPENAI_API_KEY="…"
fly deploy
fly scale count web=1 worker=1 beat=1          # the first deploy only; a deploy keeps counts
```
- `fly deploy` builds the image and runs `python manage.py migrate --noinput` ONCE as
  the release command, before any machine serves the new code. A failed migration stops
  the deploy with the old machines still serving.
- Three process groups: `web` (Daphne: HTTP and WebSockets), `worker` (Celery, may be
  scaled out), `beat` (the clock). **`beat` is exactly one machine, always**: two beats
  send every job twice, including two payment polls a minute and two renewal sweeps.
- `.env` is never part of a deploy: `.dockerignore` keeps it and every key file out of
  the image, and `manage.py` does not read it under the production settings.

## 6. First-run data
```bash
fly ssh console -C "python manage.py bootstrap_platform"
fly ssh console -C "python manage.py check_catalogue"
fly ssh console                                   # then, interactively:
python manage.py createsuperuser                  # the first operator; the owner types the password
REVIEW_ACCOUNTS_EMAIL='appreview+{role}@your-domain' REVIEW_ACCOUNTS_PASSWORD='…' \
    python manage.py seed_review_accounts         # the store reviewers' accounts (spec §10)
```
- `bootstrap_platform` brings an empty database to a launchable state and is safe to
  run again at any time (a second run prints `created 0` on every line): channel
  capabilities, quota features, plans, diet templates, achievements, the starter
  exercises and the food catalogue (`load_food_catalogue --apply`, NEVER `--wipe`).
  **Never run `load_food_catalogue --wipe` against a live database**: it deletes what
  clients have eaten and every preference they built.
- `check_catalogue` exits non-zero when an invariant the planner needs is broken, most
  importantly a food without a verified allergen composition.
- After a bulk import outside the ORM: `python manage.py update_translation_fields`.
- The review accounts' password comes from the environment of that one command, never
  from a file. Put it in the store review notes (`docs/store/review_notes.md`).

## 7. Verify
```bash
curl -fsS https://api.your-domain/api/auth/health/                  # web: database + cache
curl -fsS https://api.your-domain/api/auth/health/background/       # worker + beat + money jobs
curl -fsS -H 'X-App-Channel: apk' -H 'X-App-Version: 1.0.0+1' \
     https://api.your-domain/api/v1/config/capabilities
fly logs | grep -i -E 'celery|PRODUCTION SAFETY'
fly ssh console -C "celery -A training_platform inspect registered" | grep -c poll_open_payments
```
A beat entry naming an unregistered task is skipped silently, so the last line must print
1 or more.

**Prove the bucket, first deploy only**, before anyone uploads. The suite runs on the
filesystem backend, so this is the first time the GCS path runs:
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
The class must be the GCS one, the URL must carry a signature, and fetching it must
return the bytes while the same URL without its query string answers 403.

**The journeys, on STAGING only** (it writes a checkout, a post):
```bash
SMOKE_BASE_URL=https://<app>-staging.fly.dev \
REVIEW_ACCOUNTS_EMAIL='appreview+{role}@your-domain' REVIEW_ACCOUNTS_PASSWORD='…' \
    .venv/bin/python tools/smoke_journeys.py
```
Every line must read `ok`; it exits 1 otherwise. Never point it at production.

## 8. Monitoring and the scheduled jobs
- **The background half.** Point the uptime monitor at
  `GET /api/auth/health/background/` every minute. It answers 503 when beat's
  heartbeat is older than `BACKGROUND_HEARTBEAT_MAX_AGE_SECONDS` or a money job is late.
  This is deliberately NOT the web machines' Fly check: a failing check restarts
  machines, and a late beat must never restart the machines serving users.
- **Sentry** (with `SENTRY_DSN`): errors from web and worker, and a cron monitor for each
  money job: `poll-open-payments`, `expire-lapsed-subscriptions`,
  `renew-coaching-periods`, `settle-owed-payouts`.
- **Logs**: one JSON object per line on stdout, for every process (`fly logs`). Nothing
  is written to files.

Every scheduled job. Times are **UTC** (no `CELERY_TIMEZONE` is set; Damascus is UTC+3).
Every entry is a crontab except `close-idle-ai-sessions`, so a beat restart loses no
daily job.

| Job | When (UTC) | Why |
|---|---|---|
| `subscription.poll_open_payments` | every minute | confirms open payments with the gateway. Money |
| `training_platform.background_heartbeat` | every minute | feeds the background health check |
| `subscription.poll_expired_checkouts` | every 10 min | closes abandoned checkouts |
| `subscription.settle_owed_payouts` | :05 every 10 min | pays trainers what is due. Money |
| `subscription.sweep_unmatched_transfers` | :05 every 10 min | matches manual transfers |
| `ai_assistant.tasks.close_idle_sessions` | every 600 s | closes abandoned AI chat sessions |
| `notifications.send_workout_reminders` | hourly :00 | |
| `subscription.expire_lapsed_subscriptions` | hourly :05 | freezes what ended. Money |
| `social.advance_challenge_phases` | hourly :10 | |
| `notifications.drain_dead_letter_queue` | hourly :20 | replays failed notifications |
| `users.renew_coaching_periods` | hourly :25 | renews coaching months (the 20% fee). Money |
| `ai_assistant.tasks.check_daily_cost` | hourly :40 | the AI budget kill-switch |
| `mediakit.sweep_pending_videos` | hourly :40 | finishes videos whose task was lost |
| `notifications.sweep_unsent_emails` | hourly :50 | publishes again every email whose message was lost; gives up the too-old ones |
| `users.refresh_ages_from_birth_dates` | 00:10 | |
| `routine.abandon_forgotten_sessions` | 01:05 | |
| `routine.close_open_training_days` | 01:15 | |
| `diet.planner.refresh_food_weights` | 02:30 | consumption into planner ranking |
| `training_platform.privacy.purge_expired_personal_data` | 03:15 | retention |
| `trainer_verification.purge_replaced_documents` | 03:25 | replaced identity documents |
| `ai_assistant.tasks.purge_expired_training_data` | 03:45 | retention of AI training copies |
| `training_platform.privacy.flush_expired_tokens` | 03:50 | JWT blacklist cleanup |
| `ai_assistant.tasks.compute_all_user_insights` | 04:00 | |
| `diet.tasks.generate_daily_advice` | 06:00 (09:00 Damascus) | the daily diet advice |
| `routine.send_weekly_progress` | Sunday 17:05 | |
| `subscription.send_renewal_reminders` | 17:35 (20:35 Damascus) | the 3-day reminder (owner's freeze rule) |

## 9. Backup and restore

**What holds value, and how each is kept**

| What | Backup | Notes |
|---|---|---|
| Postgres | The provider's daily snapshots with point-in-time recovery (at least 7 days), AND a weekly logical dump off the provider | Everything of value: accounts, the ledger, payments, plans |
| Media bucket | Object soft delete (GCS default 7 days); optionally versioning with noncurrent versions deleted after at most 30 days | Photos, videos, chat media |
| Verification documents | Soft delete only; NO long versioning | Identity documents must actually go when the policy says they go |
| Redis | None | Caches rebuild; queued jobs lost in a Redis failure are recovered by the sweeps (payments are polled, emails resent, videos swept, payouts settled from the ledger) |
| Keys | The vault (section 4) | Without `FIELD_ENCRYPTION_KEY` a restored database is partly unreadable |

Backups hold erased people's data until they expire, so the privacy policy states the
backup retention (the longest of the rows above). Keep retention short and fixed.

**The weekly logical dump**
```bash
pg_dump --format=custom --no-owner --no-privileges \
  "postgresql://<user>@<host>:5432/<db>?sslmode=require" > yallagym-$(date -u +%F).dump
```
Store it encrypted, in a place owned by a DIFFERENT account from the database (for
example a backup bucket with a retention lock), never next to the keys.

**Restore** (a lost database, or a drill)
1. Create an empty database at the provider (or restore the provider's snapshot, which
   replaces steps 2 and 3).
2. `pg_restore --no-owner --no-privileges -d "<new database URL>" yallagym-<date>.dump`.
   The restore creates the append-only triggers on the ledger, payment events,
   receivables, settlements and withdrawal events after loading the data; they block
   UPDATE and DELETE, never the restore itself.
3. Point `DB_*` at the new database (`fly secrets set …`). The keys must be the SAME ones
   in use when the dump was taken.
4. `fly deploy` (the release command's `migrate` is a no-op on a current dump), then
   `bootstrap_platform` (fills anything missing, changes nothing that exists).
5. Section 7, then compare the row counts of the ledger and the payments with the
   dump's.

Never repair a ledger or payment-event row by disabling its trigger: a correction is a
new row.

**The drill, before launch and then every quarter**: restore the latest dump into a
throw-away database, run `migrate` against it (no changes expected), compare row
counts of the main tables with production, read one encrypted field with the production
key in a one-off console, then delete the throw-away database. Staging never keeps a
copy of production data or production keys.

## 10. Staging
A second Fly app from the same image and `fly.toml`, with nothing shared with
production: its own Postgres database, Redis, bucket, SMTP credentials (a sandbox
inbox), and its OWN keys, generated for it.

```bash
fly deploy -a <app>-staging
fly scale count web=1 worker=1 beat=1 -a <app>-staging
```

| Variable | Staging value |
|---|---|
| `SENTRY_ENVIRONMENT` | `staging` |
| `GATEWAY_MODE` | `sandbox`, with `SHAMCASH_SANDBOX_*` set and `SHAMCASH_API_TOKEN` UNSET (boot refuses sandbox when the production token is present) |
| `DJANGO_ALLOWED_HOSTS` | `<app>-staging.fly.dev` |
| `PUBLIC_PAGES_BASE_URL` | `https://<app>-staging.fly.dev` (required: boot refuses it unset or not https) |
| `APP_LINKS_*` | unset: the association files answer 404, so staging never claims the real app's links |
| Everything else | as production, with staging's own values |

Then section 6 (`bootstrap_platform`, `seed_review_accounts`) and section 7, including
`tools/smoke_journeys.py`. Every production deploy goes to staging first.

## 11. CI, the image, the lock
- CI is `.github/workflows/security.yml` ("CI & Security"): the suite, the migration
  check and the security scans, on pushes and pull requests to `main`, `develop` and
  `spec/payments-compliance`. It installs only the lock.
- Dependencies: a version changes only in `requirements.txt`, then `requirements.lock`
  is regenerated as that file's header says; both are committed together. The image
  and CI install the lock with hashes (`pip install --require-hashes -r
  requirements.lock`); `tests/test_django_lts.py` fails when the lock is stale or the
  environment is not the lock. The line is Django 5.2 LTS.
- The image installs `ffmpeg` for video poster frames and measured durations. Without
  it a video still uploads and plays, with no poster; `mediakit.sweep_pending_videos`
  does not backfill posters for videos already marked ready.
- `collectstatic` runs at build time under `settings_build`; nothing else in the build
  needs a secret.
