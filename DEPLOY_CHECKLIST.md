# Deploy Checklist

The runbook for putting the API on Fly.io, written from the final configuration of
2026-09-28 (Django 5.2 LTS, three process groups, object-storage media, no volume). Read
it top to bottom the first time; later deploys are section 5 alone.

**The first deployment is NOT on Fly.io.** Fly.io, Google Cloud and the managed
database services bill through processors that refuse a Syrian company, so it runs on
one Hetzner server from `deploy/` (section 12), with an S3-compatible bucket for media.
Sections 3, 4, 6, 7, 8 and 9 hold for both; section 12 says what replaces the rest.

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
| 12 | One server instead of Fly.io (`deploy/`): what replaces sections 2, 5 and 10 |
| 13 | Coaching off on Google Play: the one row, if Google objects (Road to 10 R8) |

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
`gcs` or `s3`). This section is Google Cloud Storage; an S3-compatible store is section
12.2.

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
| `DJANGO_ALLOWED_HOSTS` | R | | | Comma separated, e.g. `api.yalagym.com`. No wildcard, no localhost |
| `CORS_ALLOWED_ORIGINS` | R | | | Comma separated full origins, e.g. `https://yalagym.com` (the web hub). No wildcard, no localhost |
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
| `MEDIA_BACKEND` | R | `local` (refused) | | `gcs` (Google Cloud Storage) or `s3` (any S3-compatible store) |
| `GS_BUCKET_NAME` | R* | | | With `gcs` |
| `GS_CREDENTIALS_JSON` | R* | | yes | With `gcs`. The service account key file, as JSON text |
| `S3_BUCKET_NAME` | R* | | | With `s3`. The bucket must be private |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | R* | | yes | With `s3`. The bucket's key pair; never the process's ambient AWS identity |
| `S3_ENDPOINT_URL` | | AWS | | With `s3`. The store's address, `https://` only (Hetzner: `https://<location>.your-objectstorage.com`) |
| `S3_REGION_NAME` | | | | With `s3`. Hetzner: the location code (`fsn1`, `nbg1`, `hel1`) |
| `S3_ADDRESSING_STYLE` | | `virtual` | | `virtual` (`bucket.host/key`) or `path` (`host/bucket/key`) |
| `S3_LOCATION` | | bucket root | | A prefix inside the bucket |
| `VERIFICATION_S3_BUCKET_NAME` | R* | the media bucket | | Required when `MEDIA_PUBLIC_APP_CONTENT=True` |
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
| `AI_API_KEY` | | `OPENAI_API_KEY` | yes | The AI coach's provider key. Unset (and no `OPENAI_API_KEY`): the app boots with the coach off, and every turn on `ws/ai/chat/` answers `config_error` |
| `OPENAI_API_KEY` | | | yes | The older name of the same key; read when `AI_API_KEY` is unset |
| `AI_BASE_URL` | | the client library's (OpenAI) | | The provider's address, ending in `/v1`. Any service or self-hosted server that speaks the OpenAI Chat Completions protocol |
| `AI_ASSISTANT_MODEL` | | `gpt-4o-mini` | | The model's name at that provider. The provider's name is never shown to users (owner, 2026-09-24) |
| `AI_EXTRA_BODY` | | | | A JSON object merged into every request, for a provider-specific switch such as turning a model's "thinking" off. Malformed: boot fails |
| `AI_PRICE_INPUT_PER_1M_USD`, `AI_PRICE_OUTPUT_PER_1M_USD` | | the price table's | | The model's price, both or neither; the daily cost ceiling counts on it. One without the other: boot fails |
| `AI_MAX_MESSAGES_PER_DAY` | | `10` | | The AI coach's messages per person per day, the same on every plan with the coach; the socket refuses the next one with `rate_limit`, and `usage/today/` draws it as `ai_messages.limit` (the owner, 2026-11-21: it was 50; the free Workers AI allowance is about 115 coach messages a day for everyone together). Not a whole number, or below 1: boot fails. Set as a line or left out, never empty |
| `AI_PROVIDER`, `OPENAI_MODEL` | | `openai`, `gpt-4o-mini` | | Read by nothing; kept so an existing environment still loads |
| `HUGGINGFACE_API_TOKEN` | | | yes | Only for the Hugging Face provider |
| `AI_TRAINING_RETENTION_DAYS` | | `365` | | Anonymous training copies are purged after this |

Before a provider or model goes live, compare it on the coach's own questions:
`python manage.py ai_coach_eval --user <a test account> --out report.md` (its docstring
says what it measures and what it writes to that account). A new provider also changes
`training_platform/privacy/disclosure.py` `ai_provider`, then the data inventory and
the privacy policy (`docs/store/privacy_labels.md`).

| Variable | | Default | Secret | What |
|---|---|---|---|---|
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
| `PUBLIC_PAGES_BASE_URL` | R | | Where the privacy policy, terms and account-deletion page are reached, e.g. `https://yalagym.com`. The capabilities answer hands these URLs to every build, and the store listings name the same pages. Boot refuses it unset, not https, or a local address (B-174) |
| `SUPPORT_EMAIL` | | `support@yalagym.com` | The Help page's address (`GET /api/v1/config/support/`) |
| `SUPPORT_WHATSAPP` | | empty | The Help page's WhatsApp number; empty hides the button (the owner gives the number) |
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
(192 MB), `IMAGE_DECODE_WAIT_SECONDS` (2), `IMAGE_DECODE_RETRY_AFTER_SECONDS` (5),
`CHALLENGE_HOST_LIMIT` (3, the owner's number: challenges one member hosts at once;
0 = no limit).
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
| `FIELD_ENCRYPTION_KEY` | Health data columns (injuries, notes) at rest, in the database and in every backup | That data is unreadable for ever, in production and in every backup | A database dump or backup becomes readable: rotate, then treat it as a data breach | Prepend the new key (`new,old`) and deploy: new writes use it, both read. Then `fly ssh console -C "python manage.py reencrypt_fields"` rewrites every encrypted column under the new key (`--dry-run` counts first). Drop the old key and deploy only after it ends with "Every encrypted value is under the first key.": it exits non-zero, naming the rows, while any value needs a key the list lacks (SIDE_BUGS S-233) or kept changing on every read (run it again). It is safe with traffic on: each value is written only if it still holds what was read, so an erasure or edit made while it runs is kept, then rotated (review R-2). A backup taken before the rotation still needs the old key: keep it in the vault as long as such a backup is kept |
| `AI_TRAINING_PSEUDONYM_KEY` | The link from an anonymous AI training copy to its person, used only to delete the copies when the person turns "help us improve" off | Existing copies can no longer be found to delete | Copies could be re-linked to people | NEVER while copies exist. Delete every copy first |
| `DJANGO_SECRET_KEY` | Sessions, sign-up and reset code hashes, the OTP email seal, the agents' API secrets (when `AGENT_APIKEY_ENC_KEY` is unset), the keyed digests of saved ShamCash payer and payout addresses | Pending codes and resets die (minutes). Agents' API secrets become unreadable. Saved address digests stop matching | Sessions and reset tokens forgeable: rotate at once | Put the old value in `DJANGO_OLD_SECRET_KEY_1`, set the new one, deploy. The OTP seal, agent secrets and the address digests honour the old key (SIDE_BUGS S-232). Before dropping `DJANGO_OLD_SECRET_KEY_1`, run `manage.py rekey_address_digests` once on the new key, so the saved address digests are rewritten under it |
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
  exercises, the platform's own templates (the two free first workouts and the eight
  starter templates for trainers, Road to 10 S2.4 and S2.10: written once by key, never
  rewritten, and not written while an exercise they name is missing, which its line
  says) and the food catalogue (`load_food_catalogue --apply`, NEVER `--wipe`).
  **Never run `load_food_catalogue --wipe` against a live database**: it deletes what
  clients have eaten and every preference they built.
- **A challenge to join on day one** (Road to 10 S3.4): after the first operator exists,
  `python manage.py create_starter_challenge --creator <that operator's username>`
  opens the platform's starter challenge for the whole community (12 training days in
  30, counted from the training people log, ranked because staff made it; Arabic and
  English). Run it again whenever no starter is running: it creates nothing while one
  is upcoming or running (`--days` and `--target` change the defaults; `--force` opens another).
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
filesystem backend, so this is the first time the object-storage path runs:
```bash
fly ssh console -C "python manage.py check_media_storage"
```
For the media storage and for the verification-document storage it uploads a probe,
reads it back through a signed URL, requires the SAME address without its signature to
be refused, and deletes it. It exits non-zero and names the step that failed. A bucket
that serves the unsigned read is public: fix that before anything is uploaded.

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
4. Clear the query cache BEFORE the app serves the restored database:
   `fly ssh console -C "python manage.py invalidate_cachalot"`. On one server (section
   12) stop the app first and run it while it is stopped:
   ```bash
   docker compose stop web worker beat          # before the restore
   # ... the restore ...
   docker compose run --rm --no-deps web python manage.py invalidate_cachalot
   docker compose up -d                         # migrate, then web, worker, beat
   ```
   Why: Redis is not part of the backup, and django-cachalot keeps query results there
   for up to 24 hours (`CACHALOT_TIMEOUT`). It drops them on every write made through
   Django, but a restore is a write from outside Django, so without this step the app
   can go on reading the rows of BEFORE the restore. The one that matters most is the
   policy table: the old build flags would stay in force for up to a day — what each
   build may sell, and the Google Play coaching switch (section 13). The command marks
   every table's cached results stale and touches nothing else. Do NOT flush the
   cache's Redis database instead: the same database (DB0, the `default` cache) holds
   the console's sessions (SIDE_BUGS S-292).
5. `fly deploy` (the release command's `migrate` is a no-op on a current dump), then
   `bootstrap_platform` (fills anything missing, changes nothing that exists).
6. Section 7, then compare the row counts of the ledger and the payments with the
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

## 12. One server instead of Fly.io (`deploy/`)

The same image and the same three processes, on one machine with Docker Compose:
Postgres and Redis beside the app, Caddy in front for HTTPS, media in an S3-compatible
bucket. What it does NOT give: a second machine (a server failure is an outage until it
is rebuilt), a managed database with point-in-time recovery, or zero-downtime deploys.
It is the right size for staging and a soft launch.

| File | What |
|---|---|
| `deploy/docker-compose.yml` | `db`, `redis`, `migrate` (runs once, before anything serves), `web`, `worker`, `beat`, `caddy`. Only Caddy publishes ports |
| `deploy/caddy/Caddyfile` | HTTPS for `API_DOMAIN`, certificates from Let's Encrypt ONLY (Caddy's default fallback, ZeroSSL, refuses Syria) |
| `deploy/setup.sh` | Asks for what only the owner knows, generates every key on the server, writes `deploy/.env` (mode 600). Never overwrites it |
| `deploy/env.example` | The shape of that file |

### 12.1 Before the server
- A DNS `A` record for the API host pointing at the server. In Cloudflare it must be
  **DNS only** (grey cloud): Let's Encrypt validates over port 80, and a proxy in
  front would also make `NUM_PROXIES` 2.
- The server's firewall allows TCP 22, 80 and 443 and nothing else. Postgres and Redis
  are not published, and a published Docker port bypasses `ufw`, which is why the
  compose file publishes only Caddy.
- The sending domain is verified at the email provider (section 2.5). Use port 587,
  the default: Hetzner blocks outgoing 25 and 465 on new accounts.

### 12.2 The bucket
Create it PRIVATE, in the same location as the server, and make one key pair for it
(Hetzner: the project's Security page, "S3 credentials"; the secret is shown once).
`MEDIA_BACKEND=s3` is set by the compose file. Nothing writes a public ACL, URLs are
SigV4 signatures that expire (`MEDIA_SIGNED_URL_TTL`), and uploads are sent without the
optional checksum trailer newer AWS SDKs add, which Hetzner's store refused in 2025
(`training_platform/s3_storage.py`). Section 7's `check_media_storage` is the proof.

### 12.3 Deploy
```bash
git clone --branch spec/payments-compliance <repository> yallagym && cd yallagym/deploy
./setup.sh                      # once. Then save a copy of deploy/.env in the vault
docker compose run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile
docker compose build web        # ~10 minutes the first time
docker compose up -d            # migrate, then web, worker, beat, caddy
docker compose ps -a            # migrate: Exited (0). Everything else: Up / healthy
```
Later deploys: `git pull && docker compose build web && docker compose up -d`. The
containers are replaced one after another, so expect a few seconds of refused
connections; a failed migration stops there with the old containers still running.

`setup.sh` never edits an existing `deploy/.env`, so a variable that became required
after a server was set up has to be added by hand before `docker compose up -d`. A file
written before 2026-11-21 lacks `PUBLIC_PAGES_BASE_URL`, which production requires since
BACKEND_BUGS B-174 (without it `migrate` refuses to boot and the old containers keep
serving), and the app's identifiers for the two association files. From `deploy/`,
with the server's own API host in place of `api.example.com`:
```bash
grep -q '^PUBLIC_PAGES_BASE_URL=' .env || echo "PUBLIC_PAGES_BASE_URL='https://api.example.com'" >> .env
grep -q '^APP_LINKS_ANDROID_PACKAGES=' .env || printf '%s\n' "APP_LINKS_ANDROID_PACKAGES='com.yallagym.app'" "APP_LINKS_IOS_BUNDLE_IDS='com.yallagym.app'" "APP_LINKS_PATHS='/app/*'" >> .env
```
`APP_LINKS_ANDROID_SHA256_FINGERPRINTS` (the release key's SHA-256) and
`APP_LINKS_IOS_TEAM_ID` are added the same way once known; until then
`/.well-known/assetlinks.json` and `apple-app-site-association` answer 404
`app_links_not_configured`.

Commands that sections 6 and 7 write as `fly ssh console -C "<command>"` are
`docker compose exec -u appuser web <command>` here (`-u appuser`: `exec` does not go
through the entrypoint, so without it the command would run as root):
```bash
docker compose exec -u appuser web python manage.py bootstrap_platform
docker compose exec -u appuser web python manage.py check_catalogue
docker compose exec -u appuser web python manage.py check_media_storage
docker compose exec -u appuser web python manage.py createsuperuser
docker compose logs -f --tail=100 web worker beat
```

**The store reviewers' accounts** (section 6's last line; ROAD_TO_10 R5; the review notes,
`docs/store/review_notes.md` section 3). The password is typed at a hidden prompt and
handed to the container by NAME (`-e VAR` with no value takes it from this shell), so it
is never on the command line, in the shell history or in the process list. Run it after
`bootstrap_platform`, and again shortly before each store submission:
```bash
read -rs REVIEW_ACCOUNTS_PASSWORD && export REVIEW_ACCOUNTS_PASSWORD
export REVIEW_ACCOUNTS_EMAIL='appreview+{role}@your-domain'
docker compose exec -u appuser -e REVIEW_ACCOUNTS_EMAIL -e REVIEW_ACCOUNTS_PASSWORD \
    web python manage.py seed_review_accounts
docker compose exec -u appuser web python manage.py seed_review_accounts --list
unset REVIEW_ACCOUNTS_PASSWORD
```
- It refuses, writing nothing, without both variables, with a weak password, on a
  database `bootstrap_platform` has not prepared, or when an address already belongs to
  an account that is not a review account. Use the SAME address pattern every time: a
  second run with another pattern is refused rather than making a second set.
- A second run soon after changes nothing (`review accounts ready: nothing to change`).
  A later one keeps the accounts alive: the Premium grant is extended below 30 days, a
  new 7-day meal plan is made when none covers today, each programme's end date is moved
  on below 14 days (a programme past its dates reads `finished` since D15), and a coached
  client whose month has run out, or ends within the reminder's 3 days, is topped up by
  the review agent and the month renewed — the agent stays suspended. A new password
  rotates it and signs out every session taken with the old one.
- Give the reviewers `client` and `trainer` only (the review notes say why).

### 12.4 What is different from Fly.io
| | Fly.io (sections 2, 5, 10) | One server |
|---|---|---|
| Secrets | `fly secrets set`, write-only | `deploy/.env`, mode 600, readable by root on the server. The vault copy is still the one that matters (section 4) |
| Database | Managed, PITR | The `pgdata` volume. Turn on the provider's server backups, AND take section 9's `pg_dump` off the server: `docker compose exec -T db pg_dump -U yallagym --format=custom yallagym > yallagym-$(date -u +%F).dump` |
| Redis | Managed, `noeviction` | The `redis` service, started with `--maxmemory-policy noeviction` and a password |
| TLS | Fly's edge | Caddy, Let's Encrypt only. The certificate lives in the `caddy_data` volume |
| Client IP | `NUM_PROXIES=1` (Fly's edge) | `NUM_PROXIES=1` (Caddy). 2 if a CDN is put in front |
| Staging and production | Two apps | Two servers, each with its OWN `deploy/.env`: never copy keys between them |

Not automated yet: the off-server database dump (a cron job on the host), and a
restore drill on this layout.

### 12.5 Updating a running server

Every update, from the `deploy/` directory of the server's checkout, wherever 12.3's clone
was made (on the live server it is `/opt/yallagym/deploy`, with its `.env` beside it, from
the owner's handover of 2026-11-21):
```bash
docker compose exec -T db pg_dump -U yallagym --format=custom yallagym > ~/yallagym-before-update-$(date -u +%F-%H%M).dump
git pull --ff-only
docker compose build web
docker compose up -d            # the migrate service runs first; web, worker, beat start only if it exits 0
docker compose ps -a            # migrate: Exited (0). Everything else: Up / healthy
docker compose logs --tail=50 migrate
```
Add any variable the update names below to `deploy/.env` BEFORE `docker compose up -d`
(`setup.sh` never edits an existing file). A failed migration stops there with the old
containers still serving; the dump is the way back. Then run the update's own commands.
`bootstrap_platform` is safe after every update: it writes only what is missing.

**The Road to 10 update** (2026-11-23: a server on `f77b3e1` or `f9f4cf1`, to
`spec/payments-compliance` at `8a52c6c` or later):

- Variables:
  - Required, only on a `deploy/.env` written before 2026-11-21: `PUBLIC_PAGES_BASE_URL`
    and the `APP_LINKS_*` lines, with the two commands in 12.3. Without the first,
    `migrate` refuses to boot.
  - Nothing became required after `f9f4cf1`.
  - Optional, add only to change the default:
    - `SUPPORT_WHATSAPP`: the Help page's number, e.g. `+9639…`. Empty hides the button;
      the owner gives the number.
    - `SUPPORT_EMAIL`: default `support@yalagym.com`.
    - `CHALLENGE_HOST_LIMIT`: default 3.
  - Push stays off until `FIREBASE_CREDENTIALS_JSON` is set (the owner's Firebase service
    account key, `base64 -w0`; section 3.7).
- Migrations the migrate service applies:

  | Server on | Migrations |
  |---|---|
  | `f77b3e1` (also) | routine 0033, 0034, 0035 · social 0013, 0014 · subscription 0019, 0020 · users 0047 · wallet 0022 |
  | `f77b3e1` or `f9f4cf1` | analytics 0006 (body measurements) · routine 0036 (platform templates, the free first workout), 0037 (a template's place) · subscription 0021 (a plan paid from the wallet) · users 0048 (the client's «do not renew»), 0049 (the build a device registered from) |

  users 0049 adds `DeviceToken.app_channel` (Road to 10 R1). This update's code on a
  database WITHOUT it:
  - registering a device (`POST /api/auth/device-token/`) answers 500;
  - every push fails, while the inbox row is still written;
  - deleting an account (`GET` and `DELETE /api/privacy/erase/`, and the console's
    "erase, then close") answers 500;
  - the data export's `devices` section reads "could not be exported";
  - the console's device-token pages answer 500.

  On the server there is no such window: the migrate service runs before `web`, `worker`
  and `beat` start, and they start only if it exits 0 (above).

- After `docker compose up -d`:
  ```bash
  docker compose exec -u appuser web python manage.py bootstrap_platform
  docker compose exec -u appuser web python manage.py create_starter_challenge --creator <an operator's username>
  docker compose exec -u appuser web python manage.py check_catalogue
  ```
  - The first run of `bootstrap_platform` shows 10 platform templates created; later runs
    show 0.
  - `create_starter_challenge` opens the community's starter challenge (section 6). It
    creates nothing while one is upcoming or running.
  - Then the store reviewers' accounts, as 12.3 says ("The store reviewers' accounts":
    the password at a hidden prompt, the same address pattern every time), and again
    shortly before each store submission:
    `docker compose exec -u appuser -e REVIEW_ACCOUNTS_EMAIL -e REVIEW_ACCOUNTS_PASSWORD web python manage.py seed_review_accounts`.
- Not for the server: `manage.py seed_walk_states` (Road to 10) is a DEVELOPMENT tool
  for the app's walk-throughs. It refuses any settings but `settings_local`, so under
  this server's `settings_production` it writes nothing; never run it here.
- If this server's database is ever RESTORED (from the update's dump above, or section
  9's), clear the query cache before the app serves again, as section 9's step 4 says:
  stop `web`, `worker` and `beat`, restore, run
  `docker compose run --rm --no-deps web python manage.py invalidate_cachalot`, then
  `docker compose up -d`. A restore is a write from outside Django, which the query
  cache does not see; without it the old build flags (what each build may sell, the
  Google Play coaching switch) can stay in force for up to 24 hours (SIDE_BUGS S-292).

## 13. Coaching off on Google Play (Road to 10 R8)
The `play` build sells one thing: a month with a personal trainer, paid from the wallet,
which the build treats as a service between two people. If Google reads it otherwise,
the answer is one change in the server's table. No release and no deploy are needed;
the change holds from the next request.

**The step.** In the operator console (`/dj-admin/`), open **Policy → Channel
capabilities** and filter on `play`. On **every** `play` row, untick
`can_pay_coaching_in_app` and `can_show_coaching_prices`, leave every other box as it
is, and save. A build gets the row with the highest `min_version` it has reached, so
unticking only the `0.0.0` row leaves every build at or above a newer row still selling
coaching. If the list shows no `play` row, the built-in defaults are in force (coaching
on). In that case run `python manage.py bootstrap_platform` first: it writes the missing
base rows and changes nothing that exists. Then untick the row it wrote. Each save is
recorded under **Capability change log** with the operator's name and the old and new
values. Ticking the two boxes again turns coaching back on the same way.

**Check it.**
```bash
curl -fsS -H 'X-App-Channel: play' -H 'X-App-Version: <the live Play version>' \
     https://api.your-domain/api/v1/config/capabilities
```
It must say `"can_pay_coaching_in_app": false` and `"can_show_coaching_prices": false`.
The same call with `apk`, `web` and `ios` answers exactly what it did before.

**What it does, and what it deliberately does not.** No coaching payment can START from
a `play` build. A paid request, accepting a paid invitation, `renew: true` and a
transfer to a coach each answer 403 `not_available_on_channel`, and nothing moves.
Every read the `play` build makes carries no coaching price. The wallet still lists
coaching money the person already paid, as it does on `ios`. Two things keep running
(the manager's ruling of 2026-11-23, the status quo; a stricter cut is the owner's
question):

- A paid request ALREADY SENT can still be approved by the trainer, from any build.
- A coaching already running keeps renewing from the wallet every month. The client
  stops it with `renew: false` (`POST /api/v1/coaching/renewal/`), which every build
  accepts. The monthly renewal runs with no request and so no build, and nothing in it
  may depend on anything but the build.

`tests/test_road10_r8_play_coaching_off.py` flips the row through this console form and
proves all of the above.
