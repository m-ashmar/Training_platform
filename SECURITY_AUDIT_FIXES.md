# Pre-Production Security Audit — Fix Log

Full record of the pre-production audit remediation. One section per phase.
All changes verified with `manage.py check` (0 issues) under the project venv.

- **Phase 1 — Auth & OTP Security** (`users/`)
- **Phase 2 — Financial System** (`wallet/`)

---

# PHASE 1 — Auth & OTP Security

Scope: `users/` auth, OTP, trainer–client boundary, rate limiting.
Standard reference: `.agents/skills/django-conventions.md` (5-attempt lockout / 15-min cooldown).

| # | Severity | Area | File(s) | Status |
|---|----------|------|---------|--------|
| 1 | CRITICAL | Trainer self-assignment bypass | `users/serializers.py` | ✅ |
| 2 | HIGH | Privileged role self-registration (`agent`) | `users/serializers.py` | ✅ |
| 3 | HIGH | X-Forwarded-For spoofing defeats rate limiting | `training_platform/middleware.py`, `users/views.py` | ✅ |
| 4 | HIGH | No brute-force lockout on login/token | `users/utils.py`, `users/serializers.py`, `users/views.py` | ✅ |
| 5 | HIGH | Password-reset limiter on wrong cache segment | `users/views.py` | ✅ |

### 1. CRITICAL — Client could self-assign a trainer, bypassing approval + escrow
`UserDetailsSerializer` (`/api/auth/user/update/`) exposed `assigned_trainer` as writable; a client could `POST {"assigned_trainer": <id>}` and attach any trainer (or non-trainer) directly, skipping the request/approval + wallet-escrow flow.
**Fix.** Added `assigned_trainer` to `read_only_fields`. Assignment only via the request/approval endpoints.

### 2. HIGH — `agent` role could be self-provisioned at registration
`validate_user_type` gated only `admin`; `agent` (wallet + API-key role) was accepted for anonymous registration.
**Fix.** Both `admin` and `agent` now require an authenticated superuser. Self-service is limited to `client`/`trainer`.

### 3. HIGH — X-Forwarded-For spoofing bypassed rate limiting
Rate-limit IP used the leftmost (client-controlled) XFF entry, so an attacker rotated the key per request.
**Fix.** Added `get_trusted_client_ip()` (reads the entry `NUM_PROXIES` from the right, falls back to `REMOTE_ADDR`); used by `RateLimitMiddleware` and `ResendOTPView`.
> **Operational:** keep `NUM_PROXIES` equal to the real trusted-proxy hop count (currently `1` for the Fly.io edge). Bump it if a CDN/WAF hop is added.

### 4. HIGH — No per-account brute-force lockout on login/token
Login/token relied solely on the (spoofable) global IP limiter; no account-scoped throttle.
**Fix.** Added email-scoped lockout helpers in `users/utils.py` (hashed keys, DB1 `ratelimit_cache`, **5 failures → 15-min cooldown**, matching the OTP standard) wired into `CustomLoginSerializer` and `CustomTokenObtainPairSerializer`; counter clears on success.

### 5. HIGH — Password-reset limiter used the wrong cache segment
Used the default cache (DB0/session), not `ratelimit_cache` (DB1); counter was flush-prone and keyed on plaintext email.
**Fix.** Switched to `ratelimit_cache()` with a hashed key and atomic `incr` (consistent with the OTP paths).

**OTP core (`users/utils.py`) confirmed compliant:** CSPRNG, SHA-256 hash storage, `secrets.compare_digest`, 5-attempt/15-min lockout, 10-min expiry, anti-enumeration. No IDOR / privilege-escalation on role-scoped views.

---

# PHASE 2 — Financial System

Scope: `wallet/` — transfers, idempotency, agent auth, audit chain, limits.

**Launch decisions applied:**
- Launch as **trusted prepaid** (agent top-ups mint from the system; no agent-wallet debit yet — clean future add).
- Mobile agent flow stays **JWT-only** via the proxy (signing secret never leaves the server); client-side HMAC retained for server-to-server only.
- Default agent cap **$200/day** (monthly placeholder **$5000**), env-tunable, per-agent editable in admin.

| # | Severity | Issue | Files | Status |
|---|----------|-------|-------|--------|
| 1 | CRITICAL | `0` limit = unlimited → uncapped fund minting | `settings_base.py`, `wallet/models.py`, `wallet/views.py`, `wallet/signals.py` | ✅ |
| 2 | HIGH | Stored `hashed_key` was itself the HMAC signing secret | `wallet/security.py`, `wallet/models.py`, `wallet/views.py` + migration | ✅ |
| 3 | HIGH | Proxy top-up self-signed but never verified (dead HMAC) | `wallet/views.py` | ✅ |
| 4 | MEDIUM | Reversal had no "already-reversed" guard | `wallet/views.py` | ✅ |
| 5 | MEDIUM | Idempotency-key poisoning on pre-side-effect failures | `wallet/views.py`, `wallet/serializers.py` | ✅ |
| 6 | MEDIUM | Audit hash chain non-atomic, unverified, hashes hidden | `wallet/models.py`, `wallet/views.py` | ✅ |

### 1. CRITICAL — Fail-closed agent top-up caps
`if agent_profile.daily_limit and ...` — `Decimal(0)` is falsy, so the default `0` limit disabled the check; combined with minting from `None`, any agent could credit unlimited funds.
**Fix.** `AGENT_DEFAULT_DAILY_LIMIT` (200) / `AGENT_DEFAULT_MONTHLY_LIMIT` (5000) in settings; `ensure_agent_profile()` provisions every agent with those caps (replaces all 6 zero-default sites); `_topup_limit_error()` enforces caps **fail-closed** (`0` = no top-ups).
> **Operational:** pre-existing `AgentProfile` rows with `daily_limit=0` are now blocked until an admin sets a cap.

### 2. HIGH — Agent secret encrypted at rest
HMAC verified against the stored SHA-256 digest — the DB value was itself a bearer signer (DB leak → forgery).
**Fix.** New `AgentAPIKey.secret_ciphertext` (migration `0004`) holds the raw secret Fernet-encrypted; key from `AGENT_APIKEY_ENC_KEY` or derived from `SECRET_KEY` (never in DB). `security.py` gains `encrypt_secret`/`decrypt_secret`; `AgentTopUpView` verifies against the decrypted secret. `hashed_key` retained only as a non-signing lookup/compat digest.
> **Operational:** API keys issued before this change have `secret_ciphertext = NULL` and fail HMAC auth (401) on `AgentTopUpView` until re-issued. The mobile JWT proxy path is unaffected.

### 3. HIGH — Proxy top-up is JWT-only; dead self-signing removed
`AgentTopUpProxyView` computed a signature and never verified it.
**Fix.** Removed the signature computation and the pointless active-key requirement; JWT + `IsAgent` documented as the sole control; security rests on JWT + agent status + fail-closed caps + idempotency.

### 4. MEDIUM — Reversal idempotence
**Fix.** `AdminReversalView` rejects (409) if a `reversal` with `metadata.original_reference == reference_id` already exists.

### 5. MEDIUM — Idempotency-key poisoning (re-scoped)
Key was reserved before pre-side-effect validation; a 404/400/limit failure permanently `409`-ed safe retries.
**Fix.** Key reserved **only after** pre-side-effect validation; `min_value=0.01` on serializer amounts; transfer/reversal wrap `move_funds_atomic` in `try/except ValueError` and **release the key** when nothing moved. Reservation on post-move failures is preserved (correct).

### 6. MEDIUM — Tamper-evident audit chain hardened
`save()` read-then-wrote unlocked (concurrent forks); chain never verified; export hid hashes.
**Fix.** Chain computed in `transaction.atomic()` under a PostgreSQL advisory lock (degrades to atomic insert on SQLite); hash input expanded (`request_id`, `ip_address`, `path`); added `verify_chain()` + `AdminAuditExportView?verify=1` (returns `chain_valid` + `first_tampered_id`); export includes `prev_hash`/`entry_hash`; `delete()` blocks the ORM path.
> **Note:** true append-only must be enforced at the DB layer (revoked `DELETE`/`UPDATE` grants or a trigger); the ORM guard does not stop direct SQL.

**`WALLET_DEV_MODE`:** confirmed zero references under `wallet/` — no bypass path.

---

## Migrations to apply

`wallet/migrations/0004_agentapikey_secret_ciphertext.py` — nullable `secret_ciphertext` on `AgentAPIKey`. **Not applied by the audit** (per project rule on confirming DB target). Run `manage.py migrate wallet` against the intended database.

# PHASE 3 — Diet AI Pipeline

Scope: `diet/` — the rule-based planner (deep review), validation, permissions, AI prompt.
Enhancement ideas captured separately in `DIET_ENHANCEMENT_SUGGESTIONS.md`.

| # | Severity | Issue | File(s) | Status |
|---|----------|-------|---------|--------|
| 1 | HIGH | meal_count 1–2 under-delivers calories & macros (unnormalized split) | `diet/services/rule_based_planner.py` | ✅ |
| 2 | HIGH | Trainer `daily_calories` stored without bounds validation | `diet/trainer_services.py` | ✅ |
| 3 | MEDIUM | Read IDOR: role fall-through on nutrition/meal views | `diet/views.py` | ✅ |
| 4 | MEDIUM | Prompt injection via unsanitized user free-text | `diet/ai_services.py` | ✅ |
| 5 | MEDIUM | `_finalize_meal`/snack reported zero `total_nutrition` | `diet/services/rule_based_planner.py` | ✅ |
| 6 | MEDIUM | Global RNG reseeded mid-generation | `diet/services/rule_based_planner.py` | ✅ |
| 7 | LOW | Dead method `_rebalance_meal_accept` (~99 lines) | `diet/services/rule_based_planner.py` | ✅ removed |

### How the rule-based planner works (reference)
Per day it seeds a **local** RNG from `userid+date`, reserves 200 kcal for the snack,
splits remaining kcal across meals by a goal pattern (now normalized to sum to 1.0),
and derives per-meal macro targets = `daily_kcal × goal_ratio ÷ (4/4/9) × meal_share`.
Each meal is filled in stages (staged-fill → protein floor → carb floor → 100 g veg →
macro-priority density selection → dinner carb safeguard → 6-iteration ±9% rebalancer),
then fruits and a snack are appended. Recency blocks reusing a food id for 3 days.
Accuracy: ±9% per meal on convergence; daily totals track target for 3-meal plans.

### 1. HIGH — Normalized meal-calorie distribution
The goal split patterns are defined for 3 meals; with 1–2 meals the shares summed to
<1, so the whole day was built to ~33%/~70% of target. Added normalization
(`share /= sum(shares)`) after either allocation branch — fixes both kcal and macro
targets (macros are scaled by the same shares). Verified: 2-meal shares now sum to 1.0.

### 2. HIGH — Trainer calorie target validated
`create_diet_plan` now runs `DietInputValidator.validate_daily_calories` (bounds
1000–6000, type-coerced) before persisting, closing the trainer-supplied unbounded
target. `validate_macro_targets` remains available for the macro-input path.

### 3. MEDIUM — Read IDOR default-deny
`DietPlanNutritionView` and `MealComponentsView` now add `elif not is_admin: 403` so
only the owning trainer, owning client, or an admin can read a plan/meal — other roles
(e.g. `agent`) can no longer enumerate diet data by id.

### 4. MEDIUM — Prompt-injection sanitization
`DietGenerator._sanitize_prompt_text` collapses newlines, strips code fences/braces,
removes "ignore previous instructions"/`role:` patterns, and length-caps `user_name`,
`allergies`, `dietary_restrictions` before they enter the Jinja prompt.

### 5. MEDIUM — Real meal nutrition metadata
`_finalize_meal` and the snack now report actual `total_nutrition` from
`kcal_consumed`/`macro_consumed` (was hardcoded zeros). Persisted plans were already
correct (recomputed from components); this fixes the in-memory/log/summary metadata.

### 6. MEDIUM — Instance-local RNG
Replaced `random.seed(seed)` + module-level `random.*` calls with a per-instance
`self._rng = random.Random(seed)`, so generation no longer mutates the process-global RNG.

### Boundaries confirmed clean (no change)
Trainer↔client assignment (`_can_manage_client`), plan ownership (`created_by`), client
self-scope (`diet_plan.user == client`), and `permission_classes` on all 24 diet views.
`FoodItem.save()` normalizes every food to per-100g, so snack/fruit `/100` math is correct.

---

# PHASE 4 — Subscription & Payments (architectural rebuild)

Scope: `subscription/` — activation authority, payment state machine, gateway
integration. Gateway switched to **ShamCash**; the three bank placeholders removed.

## Findings closed (all four free-access paths)
| # | Severity | Issue | Resolution |
|---|----------|-------|-----------|
| 1 | CRITICAL | `PaymentViewSet.confirm` self-activates | Endpoint deleted; `PaymentViewSet` is now `ReadOnlyModelViewSet` |
| 2 | CRITICAL | `SubscriptionViewSet.renew` free renewal | `renew` creates a **pending** payment + initiates gateway; never mutates the subscription |
| 3 | CRITICAL | `PaymentStatusView` GET activates on placeholder success | GET is now read-only (stored status only); verified reconcile moved to `POST .../reconcile/` |
| 4 | CRITICAL | Placeholder gateways always "completed" | Bank placeholders removed; ShamCash verifies real transactions; completion only via `PaymentService` |
| 5 | HIGH | Default sandbox + hardcoded webhook secrets | Config is ShamCash-only, secrets from env with no usable defaults; `GATEWAY_MODE`/`PAYMENT_DEBUG` default to production/off |
| 6 | HIGH | No webhook replay/timestamp protection | Timestamp freshness window + `gateway_event_id` idempotency (unique) |
| 7 | HIGH | Stripe declared, unimplemented | Removed as default/active method (kept only as a legacy enum value for old rows) |

## Architecture adopted (your Batch B, points 1–11)
- **Single authority (1,6,9):** new `subscription/services/payment_service.py`. `PaymentService.complete_payment()` is the *only* code that completes a payment or activates a subscription. It runs a **state machine** (`PAYMENT_STATUS_TRANSITIONS` in models; illegal transitions raise `InvalidPaymentTransition`), verifies amount+currency (`PaymentVerificationError` on mismatch), is fully `transaction.atomic()` with `select_for_update`, generates the invoice number, activates/extends the subscription, and emits a `payment_completed` signal.
- **Payments not client-writable (2):** `PaymentSerializer` is fully read-only; `PaymentViewSet` is read-only (no create/update/confirm).
- **No confirm endpoint (3):** deleted.
- **Renewal = payment, not mutation (4):** `PaymentService.start_renewal` → pending payment → gateway redirect → completion via the same authority.
- **Poller verifies, never trusts (5):** `PaymentReconcileView` (POST) calls `fetch_payment_status`, which matches the real ShamCash transaction (amount+currency+reference+recency) before calling `complete_payment`.
- **Webhook is an authority *caller* (6):** `PaymentWebhookView` (now `AllowAny`, signature+timestamp verified) routes to `PaymentService`; polling/webhook both go through the one gate — surviving flaky bank webhooks without a second activation path.
- **Idempotency (7):** conditional-unique constraints on `gateway_event_id`, `gateway_transaction_reference`, `transaction_id`, `invoice_number`; webhook returns early on a seen event.
- **Atomic (8) & state machine (9):** as above.
- **Startup safety (10):** `enforce_production_safety()` now also fails boot on `PAYMENT_DEBUG=true`, and (when ShamCash is configured) on non-production `GATEWAY_MODE`, missing `SHAMCASH_ACCOUNT_ID`, or a too-short webhook secret. Pre-approval (no token) boots with payments simply disabled.
- **Gateway interface (11):** `subscription/gateways/base.py` `PaymentGateway` ABC (`initiate_payment`/`fetch_payment_status`/`verify_webhook`/`refund`); `ShamCashGateway` implements it; views/service depend only on the interface.

## ShamCash gateway
`subscription/gateways/shamcash.py` — Bearer auth, base `https://api.shamcash-api.com/v1`.
Two modes, config-selected, no code change:
- **reconcile** (default): user pays the merchant account with the payment reference in the note; we verify via `GET /transactions` matching amount+currency+reference within a lookback window.
- **hosted** (once ShamCash gives an initiation endpoint): set `SHAMCASH_INITIATE_PATH` and it POSTs + returns a hosted `payment_url`.
Webhooks are **fail-closed** until `SHAMCASH_WEBHOOK_SECRET` is set (HMAC-SHA256 over `"<ts>.<body>"`).

## Operational follow-ups (before charging real money)
1. Apply migration: `manage.py migrate subscription` (0004 — new fields + unique constraints).
2. Set env when the ShamCash account is approved: `SHAMCASH_API_TOKEN`, `SHAMCASH_ACCOUNT_ID`, `SHAMCASH_API_URL`, optionally `SHAMCASH_WEBHOOK_SECRET`, `SHAMCASH_INITIATE_PATH`, `GATEWAY_MODE=production`, `PAYMENT_DEBUG=False`.
3. **CONFIRM with ShamCash at onboarding** (marked `CONFIRM` in code): exact transaction-object field names (`note`/`amount`/`currency`/`id`), initiation endpoint + response fields, and webhook signature header/scheme. These are config/adapter-localized.
4. **Review (out of Phase-4 scope):** `wallet/signals.handle_payment_completed` still fires on any `Payment→completed` and transfers `amount` client→trainer. For subscription payments that may be unintended double-accounting — decide whether subscription payments should trigger a trainer payout.

# PHASE 7 — Routine & Social API Security

Scope: the 148 routes never audited in Phases 1–6, plus a dedicated deep dive on
`routine/` (the core app). **Every finding below was proven with an executable
exploit against a real test database, then re-proven blocked after the fix.**
Probes preserved in `tests/security/`.

## Read-side IDORs (all CONFIRMED with real leaked data)
| # | Sev | Finding | Proof (attacker id=2, victim id=1, no relationship) |
|---|-----|---------|------|
| 1 | CRITICAL | `AnalyticsViewSet` — 4 actions accept `?user_id=` with **zero** authorization (`summary`, `streaks`, `trends`); `completion` with no params returned **every** user's progress | `summary?user_id=1` → `{"week_volume":1125.0,"days_trained":1}` |
| 2 | CRITICAL | `WorkoutSessionViewSet` — no `get_queryset`, and `has_object_permission` returned `True` for all SAFE_METHODS; `filterset_fields` includes `user` so `?user=<id>` enumerates | list → `[{"user":1,"status":"completed"}]` |
| 3 | HIGH | `RoutineExerciseViewSet` — unscoped; leaked every trainer's routine composition | → `{"exercise":{"name":"SECRET-Squat"}}` |
| 4 | HIGH | `PublicUserProfileSerializer` **and** `UserMinimalSerializer` exposed `email`. The latter is embedded as author/creator in **9 places** (posts, comments, challenges, follows, notifications) | → `{"email":"victim@ex.com"}` |

## Write-side / privilege boundary (CONFIRMED)
| # | Sev | Finding | Proof |
|---|-----|---------|-------|
| 5 | HIGH | Any trainer could PATCH **any** client's workout session (no relationship check) — and it fired `session_completed` notifications to the victim and their real trainer | unrelated trainerB PATCH → **200** |
| 6 | HIGH | Any trainer could CREATE a session for **any** user | unrelated trainerB POST → **201** |

## Found during the routine deep dive (CONFIRMED)
| # | Sev | Finding | Proof |
|---|-----|---------|-------|
| 7 | HIGH | `ExerciseViewSet.get_queryset` logic **inverted** — trainers were restricted, but the `else` branch returned `base_qs` unfiltered, so **every client saw every trainer's private exercises** | client → sees `TRAINER-A-SECRET-LIFT` |
| 8 | HIGH | `IsTrainerOrReadOnly` had no object-level check — any trainer could **rename and DELETE** another trainer's public template | trainerB PATCH → 200 (`name='HIJACKED'`), DELETE → 204, row gone |
| 9 | HIGH | Exercise media guard `if not is_global and created_by != user` short-circuits to False for global exercises → **any user could modify the shared catalog**. Wrong in **4 places** | client add-media on global → passed the guard |
| 10 | HIGH | `ExerciseAddMediaView` referenced `ExerciseMedia` which was only imported *inside a different function* → `NameError` swallowed into a 207 response. **The add-media feature had never worked** | `errors:["name 'ExerciseMedia' is not defined"]`; after fix admin POST → 201, media row created |
| 11 | MEDIUM | 7 live `gettext` shadowing bugs: `_` rebound by tuple-unpack then `_()` called later → `TypeError: 'bool' object is not callable` on error paths (e.g. the wallet "insufficient balance" branch 500s instead of 402) | AST scan; 0 remaining after fix |
| 12 | MEDIUM | `CustomUserManager.create_user()` passed `phone_number=None`, overriding the NOT NULL model default → **any programmatic user creation crashed** | `NotNullViolation`; after fix `phone_number='0000000000'` |
| 13 | CRITICAL | **Regression I introduced in Phase 5:** `CacheMiddleware` used `private_cache()` but the module only imported `ratelimit_cache, public_cache` → `NameError` → **every cacheable path 500'd** (`/api/exercises/`, `/api/routine/templates/`, `/api/subscription/plans/`, `/api/food/`, `/api/achievements/`) | caught by my own positive test |

## Root-cause fix
Added `can_access_user_data(requester, target_user_id)` and `accessible_user_ids(requester)`
to `routine/permissions.py` — one source of truth for "may X read Y's training data"
(self / admin / approved `TrainerClientRelation`). Every endpoint accepting a
caller-supplied user id now gates on it, instead of 7 divergent ad-hoc checks.

## Verification
- **Attacks:** all 13 re-tested → blocked (403/404/empty).
- **Legitimate access preserved:** 14/14 checks pass (client reads own data, trainer
  reads/edits **their** clients, trainer copies public templates, admin unrestricted)
  plus 7/7 routine checks. **0 regressions.**
- `manage.py check` 0 issues · **167/167 modules import** · probes saved to `tests/security/`.

## Correction to an earlier phase
My own test caught #13 — a bug I introduced in Phase 5. It is recorded here rather
than silently patched, because the Phase 5 section claimed that refactor was verified.

---

# PHASE 5 — Middleware, Settings & Deployment

Scope: `training_platform/settings_*`, `middleware.py`, `Dockerfile`, `fly.toml`,
plus deployment-path code found in the deep dive.

## Root cause: Configuration Drift
Several findings share one cause — config written at different times against
different assumptions, never reconciled: `wsgi` had the safety call / `asgi` didn't;
Fly legacy `[[services.http_checks]]` vs modern `[http_service]`; docs said "optional"
/ code enforced mandatory; and (deep dive) the cache read/write halves and the
notifications app disagreed with the "optional Firebase" intent. Fixes below also
add a reconciliation point (build-settings + shared startup guard) so drift is
caught at build, not in an audit.

| # | Severity | Issue | Fix |
|---|----------|-------|-----|
| 1 | CRITICAL | `enforce_production_safety()` never called on the ASGI startup path (the prod runtime) | Wired into `asgi.py`, mirroring `wsgi.py` |
| 2 | HIGH | Financial/auth tables cacheable via cachalot | Added `subscription_*`, `wallet_idempotencykey/walletauditlog/agentprofile`, `token_blacklist_*`, `users_trainerclientrelation/otpverification/passwordresettoken` to the bypass set |
| 3 | HIGH | `collectstatic` ran under prod settings → AWS fetch fails at build → empty `staticfiles/` (masked by `\|\| true`) | New `settings_build.py` (placeholder secrets, no AWS); Dockerfile uses it and drops `\|\| true` |
| 4 | HIGH | "Optional" integrations loaded via `get_secret` (hard-crash if absent) | `OPENAI/HUGGINGFACE/EDAMAM/FIREBASE` now `get_env` (degrade); `notifications/apps.py` no longer crashes when Firebase is *unconfigured* (still fails if configured-but-missing) |
| 5 | HIGH (new) | Password reset didn't revoke existing refresh tokens | `PasswordResetConfirmView` blacklists all `OutstandingToken`s for the user |
| 6 | MEDIUM (corrected) | `CacheMiddleware` wrote **authenticated** data to DB2 — routing checked `":user:"` against the *hashed* key (never present) | Route by real identity (`_resolve_identity`); auth→DB3, anon→DB2; anon key now uses `get_trusted_client_ip` |
| 7 | MEDIUM | fly.toml health check under orphaned `[[services.http_checks]]` → never runs | Moved to `[[http_service.checks]]` |
| 8 | MEDIUM (new) | Subscription usage counter read-modify-write race | `F('usage_count') + increment` (atomic) |
| 9 | MEDIUM (new) | `RequestLoggingMiddleware` stored per-request timing on the shared instance → races under concurrent ASGI | Timing moved onto `request` |
| 10 | MEDIUM (new) | `SECRET_KEY` rotation orphaned encrypted agent secrets | `wallet/security.py` uses `MultiFernet` over `SECRET_KEY` + `SECRET_KEY_FALLBACKS` |
| 11 | MEDIUM | Redis outage → 500s (and rate-limit fails open) | `IGNORE_EXCEPTIONS` on non-sensitive caches (public/edamam); sessions/ratelimit/private stay strict; fail-open posture recorded as OD-2 |
| 12 | MEDIUM | `enforce_production_safety` missing invariants | Added checks: wildcard/localhost in ALLOWED_HOSTS/CORS/CSRF, weak/`django-insecure` SECRET_KEY |

### Correction to the Phase-5 findings report
My initial report called Redis DB2/DB3 isolation "clean". The deep dive proved
otherwise: because the private/public decision inspected the SHA-256-hashed key for
`":user:"` (which is inside the pre-hash string, never the final key), **every**
authenticated response was written to DB2. Keys embed the user id (so no cross-user
read), but the segmentation guarantee was broken. Now fixed (#6).

### Verified clean (Phase 5)
Middleware order matches the canonical list exactly (17 entries); no hardcoded secret
*values*; all deploy-pipeline "Mandatory" env vars are enforced; Dockerfile non-root
user precedes EXPOSE; `force_https = true`.

# PHASE 6 — Dead Code, Unused Features & Wiring

Scope: repo-wide. Two passes: (1) find/fix dead + unwired code, (2) a **fresh dive**
that re-verified everything from scratch and caught four additional defects,
including one production blocker and one regression introduced in pass 1.

## Runtime bugs fixed
| # | Issue | Impact | Fix |
|---|-------|--------|-----|
| 1 | `boto3` missing from requirements.txt | **Production could not boot** — `settings_secrets.get_secret()` needs boto3 for AWS Secrets Manager; every secret load raises `ImproperlyConfigured` | Added `boto3==1.42.83` |
| 2 | `routine/views.py:67` imported non-existent `training_platform.cache_backends` | `RecentProgressView.get()` raised ImportError → **500 on every request** | Corrected to `training_platform.cache` |
| 3 | `training_platform/signals.py:38` same wrong module | ImportError on every completed workout → `recent_progress` cache never busted | Corrected to `training_platform.cache` |
| 4 | `users/signals.py` never imported by any AppConfig | `UserFoodPreference` never auto-created | Added `UsersConfig.ready()`; handler rewritten (see note) |
| 5 | `langchain-core` + `pydantic` missing from requirements | `diet/ai_models.py` powers **both** AI and rule-based planners → diet generation dead on a clean build | Added both |
| 6 | `python-dotenv` removed in pass 1 but used by `manage.py` | Regression introduced by this audit | Restored |
| 7 | `prometheus-client` missing | All 7 notification metric call sites silently no-op'd via `except ImportError: pass` | Added; 9 counters now live |
| 8 | Device token re-registration never reactivated a soft-deleted token | A token marked invalid stayed dead forever — device silently lost push permanently | `FCMTokenView` always sets `is_active=True`; also captures `platform`/`app_version`/`device_id` |
| 9 | `users/utils.py` + `social/tasks.py` sent push to inactive tokens | Wasted FCM quota, guaranteed errors | Both filter `is_active=True` |

> **Note on #4:** the original handler also called `generate_ai_diet_plan.delay()` on
> every signup. At `post_save` a new user has no height/weight/age/gender, so
> `calculate_daily_calories()` raises `ValueError` → guaranteed failure + 3 retries,
> at OpenAI cost, for an account still inactive pending OTP. It now only creates
> `UserFoodPreference` (clients only); plan generation stays user-triggered.

## Notification system — fragmentation repaired
Three parallel `Notification` models existed. A complete event-driven pipeline was
already built (`emit_event → Celery → listener → NotificationService →
notifications.Notification → FCM`) and `/api/social/notifications/` reads it — but
three writers bypassed it, writing rows **no endpoint could ever read**:

| Writer | Was writing to | Now |
|---|---|---|
| `routine/tasks.py` | `routine.Notification` (invisible) | `NotificationService` (canonical) |
| `achievements/engine.py` | `social.Notification` (invisible) | emits `AchievementAwardedEvent` |
| `social/services.py` | `social.Notification` **duplicate** alongside a correct event | emits the event only |

Supporting fixes:
- Added `notifications/domain/routine_events.py` (5 templated events) and registered
  them in `EVENT_CLASS_REGISTRY`. Without templates, routine notifications routed to
  the canonical path would have rendered as a generic "You have a new notification."
  and **lost their message** — caught in the fresh dive.
- Legacy `routine.Notification` / `social.Notification` marked **DEPRECATED — DO NOT
  WRITE** with pointers to the correct path. Tables retained (historical rows);
  removal tracked as OD-3.
- Custom admin dashboard registered `social.Notification`, so admins saw a
  permanently empty table while real notifications lived elsewhere. Now registers the
  canonical model; the dead-table `NotificationProxy` is no longer registered.
- `social/serializers.py` imported `Notification` from *both* `social.models` and
  `notifications.models`, the second shadowing the first. Removed the dead import —
  a reorder would have silently rebound the serializer to the deprecated table.

## Features wired that were built but unreachable
- **Notification preferences API** — `UserNotificationPreference` was consulted at
  send time but had no endpoints (admin-only), so users could never manage
  notifications. Added `notifications/views.py` + `urls.py` →
  `/api/notifications/preferences/` (upsert-safe CRUD + `event_types/`), routed in
  root urls.
- **`django_filters`** — used by `routine/views.py` via `DjangoFilterBackend` but
  absent from `INSTALLED_APPS`. Added.
- **Sentry** — declared dependency, never initialized despite docs claiming
  monitoring. Now `sentry_sdk.init()` when `SENTRY_DSN` is set, `send_default_pii=False`.

## Removed (dead)
- 391 lines from `diet/tasks.py`: `export_training_dataset`,
  `analyze_diet_plan_effectiveness` + 8 helpers used only by them
- `social/tasks.py`: `send_bulk_notifications`, `dispatch_bulk_notifications`
- `users/views.py`: `DeviceTokenRegisterView` (imported, never routed; its
  `update_or_create` also never reactivated tokens)
- 5 one-off management commands with hardcoded usernames (`obadax12`, `bdfb`, `mmmm`)
  and a bug-repro script for an already-fixed bug
- Packages: `stripe`, `python-decouple`, `django-environ`, `dj-database-url`, `gunicorn`
- 3 orphaned imports; `analytics/apps.py` `ready()` importing a non-existent
  `analytics.signals` inside `except ImportError: pass` (permanent silent no-op)
- `social/apps.py` silent `except ImportError: pass` replaced with an explicit import —
  it would have hidden exactly the class of breakage found in #3

## Corrections to my own earlier findings
- **`challenges` app is NOT dead.** I initially reported it as unwired. It is an
  admin-grouping layer: proxy models that re-group social models under a "Challenges"
  heading in the custom admin. Kept and documented.
- **No model/serializer mismatch** in `NotificationViewSet` — the later import
  correctly rebinds to the canonical model (though the shadowing was fragile; fixed).

---

## Combined verification

- `manage.py check` (project venv, `settings_local`) — **0 issues** after all four phases.
- Phase 1: files compile; lockout counts aligned to 5.
- Phase 2: Fernet encrypt→decrypt round-trips; HMAC accepts the raw secret and rejects a wrong key; defaults resolve to 200/5000; `0`-limit blocks any positive amount.
- Phase 3: files compile; 1- and 2-meal distributions normalize to 1.0; sanitizer strips injection phrases/fences; dead method removed.
- Phase 4: files compile; migration `0004` generated; state machine verified (pending→completed allowed, cancelled→completed blocked, completed→refunded allowed, no-op idempotent); amount/currency verification rejects mismatch/missing; invoice numbers generate; no residual references to removed gateways or `get_payment_status`.
- Phase 6: **all 166 project modules import cleanly (0 failures)**; `manage.py check` 0 issues; `makemigrations --check` reports no pending changes; 986 routes resolve; all 28 external imports covered by requirements.txt; 12 domain events ↔ 12 listeners ↔ 17 template registry entries with 0 unresolvable; routine notification renders end-to-end ("Routine Assigned" + real message); 9 prometheus counters live; no stale references to any removed symbol.
- Phase 5: files compile; `settings_local` check 0 issues; `settings_build` boots with no real secrets/Firebase (collectstatic will succeed); `enforce_production_safety` catches weak SECRET_KEY + wildcard/localhost origins; `MultiFernet` agent-secret round-trip passes; Firebase-unconfigured no longer crashes boot.

---

## Open Decisions (awaiting owner — not yet actioned)

### OD-1 — Subscription payment triggers a trainer wallet payout (Phase 4)
**Status:** flagged, deliberately NOT changed. Owner to decide.
**Where:** `wallet/signals.py` → `handle_payment_completed` (fires on any `Payment` reaching `status='completed'`, now including subscription payments completed by `PaymentService`).
**Concern:** the signal transfers `payment.amount` from the client's wallet to their assigned trainer's wallet on *every* completed payment. For **subscription** payments (client paying the platform) this likely double-accounts — the money both settles the subscription and is moved to the trainer.
**Why left alone:** pre-existing behavior, outside Phase 4's scope; changing wallet economics silently would be wrong without your call.
**Options to choose from later:**
1. Scope the payout to non-subscription payments only (e.g. gate on `payment.metadata`/`payment_method` or an explicit `kind`).
2. Keep it, if a subscription is *meant* to pay the trainer.
3. Move trainer payouts out of the `post_save` signal into an explicit service call at the point trainer compensation is actually intended.
**Decision:** _pending._

### OD-3 — Drop the deprecated legacy Notification tables (Phase 6)
**Status:** models marked DEPRECATED and de-registered from admin; tables retained.
**Where:** `routine.Notification` (routine/models.py), `social.Notification` (social/models.py).
**Context:** neither model has any writer left; all notifications now go to the
canonical `notifications.Notification`. The tables may still hold historical rows
written before the fix (which were never user-visible, since no endpoint read them).
**Why not dropped now:** dropping tables is irreversible, and standard practice is to
stop writing in one release and drop in a later one, after confirming no regression.
**Options to choose from later:**
1. Drop both models + migration once you're satisfied nothing regressed (recommended).
2. Migrate historical rows into `notifications.Notification` first, then drop.
3. Keep indefinitely as an archive (costs nothing but invites future misuse).
**Decision:** _pending._

### OD-2 — Rate-limit / lockout fail-open on Redis outage (Phase 5)
**Status:** flagged, current behavior kept deliberately. Owner to decide.
**Where:** `training_platform/middleware.py` → `RateLimitMiddleware._is_rate_limited` (`except Exception: return False`), and the OTP/login lockout helpers on `ratelimit_cache` (DB1).
**Concern:** if Redis is unavailable, rate limiting and brute-force lockout silently **fail open** — throttling is disabled for the duration of the outage.
**Trade-off:** failing *closed* would instead lock out all users during a Redis blip (availability hit). Phase 5 kept fail-open and made only non-sensitive caches (public/edamam) ignore Redis errors; `default`/`ratelimit`/`private` stay strict.
**Options to choose from later:**
1. Keep fail-open (availability-first) — accept reduced throttling during outages.
2. Fail closed only on auth-sensitive paths (login/OTP/password-reset), fail-open elsewhere.
3. Add a conservative in-process fallback limiter for when Redis is down.
**Decision:** _pending._

---

# PHASE 8 — FILE UPLOADS & MEDIA STORAGE ✅

13 findings (P8-01…P8-13), all fixed. Detail in `BUG_REGISTRY.md`.

## Single entry point
`training_platform/file_security.py`
- `process_uploaded_image(file, max_bytes)` — size cap → magic-byte sniff → format
  allowlist → pixel/dimension caps → **re-encode** (strips EXIF/GPS, neutralises
  polyglots). Returns `(ContentFile, extension)`; the stored extension comes from the
  *detected* format, never the filename.
- `SecureImageField` — DRF `ImageField` subclass routing through the same function, for
  every serializer-based upload path.
- `delete_file_field(instance, field)` — storage cleanup helper.

Every upload now goes through one of those two. No `content_type` header check remains
anywhere in the codebase (grep-verified).

## Media persistence
`MEDIA_ROOT` env-driven; media served outside the `DEBUG` block (uploads used to 404 in
production); `USE_EXTERNAL_MEDIA_STORAGE` flag; Fly volume mounted at `/data` with
`MEDIA_ROOT=/data/media`; Dockerfile creates and chowns it.

## File lifecycle
Two generic receivers in `training_platform/signals.py` cover **all** models in our apps:
- `post_delete` → remove the stored file
- `pre_save`   → remove the file a FileField was repointed away from

Both defer through `transaction.on_commit()` and skip paths a sibling row still
references. The three inline pre-commit deletes they replace are gone.

## Authorization regression caught here
`routine/permissions.py` — 10 sites returned **500 instead of 401** to anonymous callers
(`AnonymousUser` is truthy and has no `is_trainer`). Fixed; an anonymous route sweep is
now a permanent suite because the authenticated sweep could never have caught it.

## Suites added
`tests/security/file_lifecycle.py` · `sweep_5xx.py` · `sweep_anon.py`

---

# PHASES 9 – 11 SUMMARY  ✅  *(2026-09-02)*

| phase | findings | headline |
|---|---|---|
| **9** AI assistant & WebSocket | 10 | stored prompt injection through profile fields; a cancelled subscription kept working on an open socket; health data retained for training with no consent |
| **8.5** Write paths | 3 | two create endpoints could never succeed (500 on every call); `POST /api/social/follows/` and `/api/routine/routine-progress/` |
| **9.5** Background jobs | 8 | **no Celery worker or beat was deployed** — every `.delay()` enqueued a job nothing consumed; lost-update race corrupting training volume |
| **10** Admin dashboard | 8 | **admin wrote passwords unhashed**; a bulk action reset accounts to `testpass123` |
| **10.5** Diet engine | 5 | planner was allergen-blind (85% of a meal's protein deleted post-balance); 15% of the catalogue had a guessed serving size |
| **11** Error handling | 6 | 112 silent `except: pass`; 45 broad handlers turning any error into a 500 |

**Cumulative: 84 findings fixed across 11 phases.** Every fix carries an executable proof
in `tests/security/` (37 suites).

## Infrastructure changes in this stretch
- `fly.toml` split into `web` + `worker` process groups; worker never scales to zero
- Celery broker moved to Redis DB6/DB7, off the six segmented cache DBs
- `acks_late`, `reject_on_worker_lost`, prefetch=1, soft/hard task time limits
- `enforce_production_safety()` now refuses to boot on a missing or misplaced broker
- 15 composite `(owner, -created_at, -id)` indexes replacing single-column ones
- Every model has a deterministic total order (42 updated)

## Still open (owner decisions)
1. Drop the two dead Notification tables (owner approved; not yet executed)
2. Consolidate `social` achievements into the live `achievements` app
3. 80 foods still in the allergen review queue — need a human or ingredient data
4. `ai_training_consent` needs a UI toggle or the training set stays empty
5. The architecture programme (outbox, object storage, domain events) — deferred by the
   owner to finish the phase plan first

---

# DIET RE-ENGINEERING + WIRING  ✅  *(2026-09-02)*

Two pieces of work beyond the phase plan, both requested directly.

## The diet planner is now a system, not a 1,791-line function
`diet/planner/` — `policy` · `targets` · `candidates` · `optimize` · `converge` ·
`recipes` · `learning` · `report`. The domain knowledge was kept; the control flow was
replaced.

**Measured on the same fixture:** a user with no food preferences went from a **233 kcal
plan (-90% of target), stored silently**, to **+0.1% of target**. A user with preferences
went from -22.2% (fat -37%) to **-4.6%, inside tolerance**. A full day is now four named
dishes at **+2.3%** of target instead of macro piles.

Six bugs were found *while building it*, each proven by a measurement — salmon filed as a
fat, every fruit filed as a vegetable, oats filed as a protein (hiding 66 g of carbs), an
objective that double-counted calories, a duplicated macro-ratio table, and a flat portion
floor that turned a 5 g oil correction into a 15 g jump.

## Everything unwired is wired
- **Analytics** is written by the server for the first time. It was read in 37 places and
  written in none, which meant every achievement computed from `UserActivity`,
  `PerformanceMetric` or `UserGoal` could never award.
- **The notification DLQ** is drainable and monitored, not write-only.
- **`AchievementProgress`** is written — a "3 of 5 workouts" screen that was fully plumbed
  and permanently empty now has data.
- **Three genuinely dead models removed**; `social.Leaderboard` deliberately kept as an
  unimplemented feature rather than dead scaffolding.

## The regression gate
`pytest -q` → **27 tests, ~6 seconds**, one assertion per defect found across all phases.
`ci/ci.yml` runs `check` → `makemigrations --check` → import sweep → gate on every PR,
and the four route sweeps on `main`.

⚠️ **Both workflow files are staged in `ci/` and must be moved by hand** — this
environment blocks writes to `.github/workflows/`.
