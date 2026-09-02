# Production Readiness Plan — Phases 7→16 (Detailed Spec)

**Goal:** market-ready backend — no behavior drift, no bugs, no engineering errors,
no dead code, no weak approaches — before mobile development starts.

**Companion docs:** `SECURITY_AUDIT_FIXES.md` (Phases 1–6, done) ·
`AUDIT_NOTEBOOK.md` (working notebook / progress) ·
`DIET_ENHANCEMENT_SUGGESTIONS.md` (optional diet upgrades).

**Status:** ⬜ not started · 🟡 in progress · ✅ done · ⏸️ blocked

---

## Baseline (measured 2026-07-09)

| Metric | Value |
|---|---|
| Python files (excl. migrations/tests) | 221 |
| Lines of code | 39,346 |
| API routes | ~297 (+256 admin) |
| Migrations | 84 |
| Routes audited in Phases 1–6 | ~136 (46%) |

**Rule for every phase:** verify with a command, not from memory. Record the command
and its output in `AUDIT_NOTEBOOK.md`. If a finding contradicts an earlier phase,
correct the earlier record — never leave two conflicting statements.

---

# 🔴 B1 — BLOCKER: Health endpoint missing   ✅ DONE

**Objective:** make `GET /api/auth/health/` exist and return 200.

**Risk if skipped:** `fly.toml` polls this path every 30s. Phase 5 moved the check to
`[[http_service.checks]]` so it now actually binds — it will hit a 404, mark the machine
unhealthy, and **fail every deploy**. Before Phase 5 the check was inert, so this was
latent; it is now active.

**Evidence:** `resolve('/api/auth/health/')` → `Resolver404`. `fly.toml` lines under
`[[http_service.checks]]` reference that exact path.

**Tasks**
- [ ] Add `HealthCheckView` (`AllowAny`, no auth, no DB writes)
- [ ] Check DB connectivity (`SELECT 1`) and Redis ping, with short timeouts
- [ ] Return 200 `{status, db, cache, version}`; 503 if a hard dependency is down
- [ ] Route it at `users/urls.py` as `health/` (so it lands on `/api/auth/health/`)
- [ ] Ensure `RateLimitMiddleware._should_skip` exempts it (it currently skips `/health/`
      only — confirm the `/api/auth/health/` prefix is exempt too)
- [ ] Confirm it is NOT behind `HasDietAccess`/subscription permissions

**Verify**
```bash
DJANGO_SETTINGS_MODULE=training_platform.settings_local .venv/bin/python -c \
"import django;django.setup();from django.urls import resolve;print(resolve('/api/auth/health/'))"
```

**Done when:** the URL resolves, returns 200 unauthenticated, and is rate-limit exempt.

---

# PHASE 7 — Routine & Social API Security   ✅ DONE

**Objective:** close authorization holes across the 148 routes never audited.

**Risk if skipped:** this is the same bug class already found in Phases 1 and 3 (role
fall-through IDOR). Highest probability of a real, exploitable bug in the codebase.

**Scope**
- `routine/` — 88 routes, `views.py` 2,211 lines, 15 view classes:
  `RecentActivityProgressView, ExerciseViewSet, ExerciseCreateWithImageView, RoutineViewSet,
  RoutineExerciseViewSet, RoutineProgressViewSet, ExerciseSetLogViewSet, WorkoutSessionViewSet,
  AnalyticsViewSet, RoutineTemplateViewSet, RoutineTemplateExerciseViewSet,
  UserExerciseProgressViewSet, ExerciseImageUploadView, ExerciseAddMediaView,
  TrainerClientProgressViewSet`
- `social/` — 60 routes, 7 view classes: `UserFollowViewSet, PostViewSet, CommentViewSet,
  ChallengeViewSet, AchievementViewSet, NotificationViewSet, PublicUserProfileViewSet`
- `routine/permissions.py` (12 permission classes — already read in Phase 1, never audited
  against actual usage)

**Evidence already found**
- `routine/views.py:834` — `Routine.objects.get(id=routine_id)` with **no ownership check**;
  bulk set-log creation against any routine id.
- `social` — 7 `get_queryset`, only 4 filter by `request.user` → 3 unscoped.
- `routine` — 7 `get_queryset`, 6 scoped → 1 unscoped. `achievements` — 1, unscoped.
- Raw id lookups: `routine/views.py` 435, 540, 899, 962, 1088.

**Tasks**
- [ ] For every detail route: confirm object-level ownership (not just `permission_classes`)
- [ ] Map each of the 15 + 7 view classes → who may read / write / delete
- [ ] Fix `routine/views.py:834` ownership check
- [ ] Scope the 4 unscoped `get_queryset` (3 social, 1 routine, 1 achievements)
- [ ] Verify trainer↔client boundary on routine assignment, progress, set logs
      (reuse `IsTrainerOfApprovedClient` semantics from Phase 1)
- [ ] Check `PublicUserProfileViewSet` for field leakage (emails, phone, internal ids)
- [ ] Check `ExerciseSetLogViewSet` / `WorkoutSessionViewSet` cross-user writes
- [ ] Default-deny on role fall-through (same fix pattern as Phase 3)

**Verify**
```bash
grep -nE "get_object_or_404\(|\.objects\.get\((id|pk)=" routine/views.py social/views.py
grep -A6 "def get_queryset" routine/views.py social/views.py | grep -c request.user
```

**Done when:** every detail/list route provably scopes to the caller or an explicit
approved relationship; no role falls through to an unguarded branch.

**Gotchas:** don't break the trainer's legitimate access to approved clients (Phase 1
`invalidate_client_cache` behavior); `AnalyticsViewSet` in routine may intentionally
aggregate across users for trainers — confirm intent before locking it down.

---

# PHASE 8 — File Upload & Media Security   ✅ DONE

**Objective:** make uploads actually safe; wire the validator that already exists.

**Risk if skipped:** a polyglot/malicious file can be uploaded today by setting a fake
`Content-Type` header. The docs claim protection that does not exist.

**Evidence**
- `training_platform/file_security.py` — 14,721 bytes / 419 lines. Public API:
  `FileSecurityValidator` (L28), `SecureFileUploadMixin` (L349),
  `validate_uploaded_image` (L382), `validate_uploaded_document` (L390),
  `secure_file_upload_path` (L398). **Zero call sites anywhere in the codebase.**
- Real upload paths validate only `file.content_type` (attacker-controlled header) + size:
  `users/views.py:1504` (profile picture), `routine/views.py:193` (exercise image),
  `routine/views.py:213` (media photos), `routine/views.py:1719` (exercise image upload).
- `SYSTEM_OVERVIEW.md` §14 claims "Server-side MIME type validation via python-magic" and
  "Prevents polyglot/spoofed file uploads" — **not true today**.

**Tasks**
- [ ] Wire `validate_uploaded_image` into all 4+ upload paths (magic-byte sniffing)
- [ ] Extension allowlist in addition to detected MIME
- [ ] Re-encode images (strips embedded payloads + EXIF/GPS privacy)
- [ ] Use `secure_file_upload_path` (randomized names — avoid user-controlled paths)
- [ ] Per-type size caps; consistent limits (profile 2 MB vs exercise 5 MB — confirm intent)
- [ ] Confirm `MEDIA_ROOT` is never served with execute permissions; no `.py/.html` served
- [ ] Decide: keep `SecureFileUploadMixin` or delete if the function API is enough
- [ ] Update `SYSTEM_OVERVIEW.md` §14 to match reality

**Verify**
```bash
grep -rn "file_security\|validate_uploaded_image" --include="*.py" . | grep -v .venv | grep -v "file_security.py:"
```

**Done when:** every `request.FILES` path runs magic-byte validation; the grep above
returns a hit for each upload view; docs match code.

---

# PHASE 9 — AI Assistant & WebSocket Security   ✅ DONE

**Objective:** secure untrusted input reaching an LLM that holds tools and spends money.

**Scope:** `ai_assistant/` (29 files, 3,325 lines) — `consumers.py`, `tool_registry.py`,
`tools/` (`diet_tools, progress_tools, routine_tools, training_tools, user_tools`),
`services/` (`chat_service, context_compiler, cost_tracker, data_collector,
memory_service, security`), plus `social/consumers.py`.

**Evidence:** both consumers authenticate (`social/consumers.py:14` closes on
unauthenticated; `ai_assistant/consumers.py:60/64` closes with 4001/4003). Authorization,
throttling, and origin validation are unverified.

**Tasks**
- [ ] Per-room/per-session authorization (auth ≠ authz: can user A join user B's session?)
- [ ] WS message rate limiting and max payload size
- [ ] Origin validation (`AllowedHostsOriginValidator` in `asgi.py` — currently absent)
- [ ] Tool-call argument validation — every tool in `tools/` must re-check ownership
      server-side, never trust ids the model produced
- [ ] Prompt injection via stored user data (same class as Phase 3 fix)
- [ ] Cost caps **enforced**, not just alerted (`DAILY_COST_ALERT_USD` is an alert today)
- [ ] `MAX_MESSAGES_PER_DAY` / session timeout actually enforced
- [ ] Review `ai_assistant/services/security.py` — what does it guarantee?

**Verify**
```bash
grep -n "OriginValidator\|AllowedHosts" training_platform/asgi.py
grep -rn "user\b" ai_assistant/tools/*.py | grep -c "request.user\|self.user"
```

**Done when:** every tool independently authorizes; WS connections are origin-checked,
throttled, and per-session authorized; cost limits block rather than warn.

---

# PHASE 10 — Admin Dashboard & Privilege Boundaries   ⬜

**Objective:** audit the highest-privilege surface — 256 routes.

**Scope:** `admin_dashboard/` (`admin.py`, `views.py`, `urls.py`, `templates/`), plus
every app's `admin.py`, and the dual exposure of `/admin/` and `/dj-admin/`.

**Tasks**
- [ ] Who can reach `/dj-admin/` and `/admin/` — staff vs superuser vs `user_type='admin'`
- [ ] Which models are writable in admin, and can an edit bypass business rules
      (e.g. editing `Subscription.status` or `Wallet.balance` directly — Phase 2/4 built
      service-layer invariants that admin can sidestep)
- [ ] Admin actions with side effects — are they audited (`WalletAuditLog`)?
- [ ] `challenges/admin.py` proxy grouping still correct after Phase 6 changes
- [ ] Consider disabling `/admin/` in production (keep only `/dj-admin/`)
- [ ] Admin session hardening (timeout, 2FA consideration)

**Done when:** admin write paths either enforce the same invariants as the API or are
explicitly documented as break-glass with audit logging.

**Gotchas:** Phase 6 changed admin registrations (canonical Notification, dropped
`NotificationProxy`). Re-verify the dashboard renders and no model is double-registered.

---

# PHASE 11 — Error Handling & Resilience   ✅ DONE

**Objective:** end the silent-failure culture that hid the Phase 5/6 bugs.

**Risk if skipped:** this is *why* `cache_backends` (500 on every request) and the dead
metrics module went unnoticed. Every swallow is a place a future bug hides.

**Evidence — 88 × `except Exception: pass` + 3 × bare `except:`**

| File | count |
|---|---|
| `diet/services/rule_based_planner.py` | 26 |
| `diet/utils/portioning.py` | 14 |
| `diet/services/diet_persistence.py` | 8 |
| `challenges/admin.py` | 6 |
| `diet/experimental/staged_fill.py` | 4 |
| `diet/utils/nutrition.py` | 3 |
| `users/views.py`, `users/utils.py`, `training_platform/middleware.py`, `social/views.py`, `diet/services/macro_cap_enforcer.py`, `diet/services/macro_balancer.py` | 2 each |

**Bare `except:` — all three are in the AUTHORIZATION layer:**
`subscription/permissions.py:17, 52, 137`. They fail closed (return False), which is safe,
but they mask real errors — a DB blip looks identical to "no subscription".

**Tasks**
- [ ] Triage all 91: log-and-continue / re-raise / narrow the exception type
- [ ] `subscription/permissions.py` — narrow to `Subscription.DoesNotExist`, log the rest
- [ ] `acks_late=True` + idempotency on Celery tasks that matter (0 of 11 today)
- [ ] Task `time_limit`/`soft_time_limit`; retry policy per task
- [ ] Redis-outage behavior (closes **OD-2**)
- [ ] DB connection failure handling
- [ ] Write a graceful-degradation matrix: dependency → behavior when down

**Verify**
```bash
grep -rn -A1 "except Exception" --include="*.py" . | grep -v .venv | grep -c "pass"
grep -rn "acks_late" --include="*.py" . | grep -v .venv | wc -l
```

**Done when:** no `except: pass` in a path that can hide a functional failure; every
Celery task is idempotent or `acks_late`-safe; degradation matrix documented.

---

# PHASE 12 — Data Protection & Privacy   ⬜

**Objective:** stop leaking PII; make retention and audit guarantees real.

**Evidence — 18 PII log sites:** `users/utils.py` (10), `users/views.py` (6),
`social/firebase_service.py` (2). `social/firebase_service.py:118` logs a **full FCM
token**. `CLAUDE.md` explicitly states: "NO PII, tokens, or secrets in log messages."

**Tasks**
- [ ] Replace emails with user ids (or hashes) in all log statements
- [ ] Never log tokens — truncate or omit (`firebase_service.py:118` logs the whole token)
- [ ] Add a logging filter that redacts email/token/password patterns defensively
- [ ] Account deletion / data export path (GDPR-shaped, even if not legally required yet)
- [ ] Data retention policy for `WalletAuditLog`, `NotificationFailure`, `Notification`
- [ ] **DB-level append-only** for `WalletAuditLog` — Phase 5 noted the ORM guard does not
      stop direct SQL; needs revoked UPDATE/DELETE grants or a trigger
- [ ] Close **OD-1** (subscription payment → trainer payout), **OD-2**, **OD-3**

**Verify**
```bash
grep -rnE "logger\.[a-z]+\(f?\"[^\"]*\{[a-z_.]*\b(email|phone_number|token)" --include="*.py" . | grep -v .venv | wc -l
```

**Done when:** the count above is 0; audit table is append-only at the DB layer; all
three open decisions are resolved.

---

# PHASE 13 — Performance & Scale   ⬜

**Objective:** fit the 512 MB / 1 shared vCPU / scale-to-zero budget.

**Evidence:** 4 query-in-loop sites in `routine/views.py`, 2 in `achievements/views.py`.
`routine/views.py:840+` creates records in a nested loop (sets × exercises).

**Tasks**
- [ ] N+1 sweep — `select_related`/`prefetch_related`, `bulk_create` for the nested loops
- [ ] Index review against real query patterns (84 migrations exist; verify indexes match
      the filters actually used)
- [ ] Pagination on every list endpoint (some return unbounded lists)
- [ ] Cold-start time with `min_machines_running = 0` — first request latency
- [ ] `CONN_MAX_AGE=600` correctness with scale-to-zero + connection pool sizing
- [ ] Memory profile under load (512 MB with Daphne + Django + cachalot)
- [ ] Cache hit rates after the Phase 5 `CacheMiddleware` fix

**Done when:** no endpoint issues unbounded queries; p95 latency measured and documented;
app survives a load test within the VM budget.

---

# PHASE 14 — Test Suite & CI Gates   ⬜

**Objective:** protect every fix from Phases 1–13 against regression.

**Risk if skipped:** nothing currently prevents any fixed bug from returning.

**Evidence:** no `pytest.ini`/`setup.cfg`/`pyproject.toml`; no test files outside
`_excluded/`; `pytest`, `pytest-django`, `factory-boy`, `coverage` installed with
nothing to run.

**Tasks**
- [ ] `pytest.ini` with `DJANGO_SETTINGS_MODULE=training_platform.settings_local`
- [ ] `factory-boy` factories per the project convention (not `objects.create_*`)
- [ ] Regression tests — one per Phase 1–6 fix, minimum:
      - payment state machine + illegal transitions (P4)
      - fail-closed agent caps; `0` = no top-ups (P2)
      - OTP + login lockout at 5 attempts (P1)
      - `assigned_trainer` read-only (P1)
      - diet meal distribution normalizes to 1.0 for 1/2/3 meals (P3)
      - IDOR default-deny on diet nutrition/meal views (P3)
      - notification writers land in the canonical store (P6)
- [ ] **Clean-room install:** fresh venv + `pip install -r requirements.txt`
      (requirements now pins `boto3`, `langchain-core`, `pydantic`, `prometheus-client`
      that the Docker image has **never** installed)
- [ ] **Clean `docker build`** — proves `settings_build.py` + collectstatic work
- [ ] `pip-audit` / `safety` dependency scan
- [ ] Wire into CI (the pipeline described in `deploy-pipeline.md` claims gates that
      have never run)

**Done when:** `pytest` runs green from a clean checkout; `docker build` succeeds;
dependency scan is clean or triaged.

---

# PHASE 15 — Code Hygiene & Consistency   ⬜

**Objective:** remove noise and inconsistency that causes future drift.

**Evidence**
- ~45 leftover `print()` in production paths:
  `diet/services/rule_based_planner.py` (16), `diet/utils/portioning.py` (12),
  `ai_assistant/tools/diet_tools.py` (7), `diet/experimental/staged_fill.py` (4),
  `ai_assistant/tools/user_tools.py` (3), `ai_assistant/tools/routine_tools.py` (3)
- 11 `TODO/FIXME` — `routine/views.py` (4), `routine/models.py` (3), + 4 others
- Duplicate seeding: `social/management/commands/create_achievements.py` vs
  `achievements/management/commands/sync_achievements.py`

**Tasks**
- [ ] Replace every `print()` with the app logger at DEBUG
- [ ] Resolve or convert each TODO into a tracked item
- [ ] De-duplicate the two achievement seeding commands
- [x] Consistent error envelope across apps (`{"error": ...}` vs DRF default)
- [x] Consistent pagination shape (`api-contract-sync.md` notes some nest inside `results`)
- [ ] Decide on `DIET_ENHANCEMENT_SUGGESTIONS.md` items (owner picks)

**Done when:** zero `print()` in app code; no untracked TODOs; one seeding command.

---

# PHASE 16 — API Contract Freeze & Mobile Handoff   ✅ (2026-09-02 — see API_CONTRACT.md)

**Objective:** hand the mobile team a stable, accurate, documented contract. **This is
the gate to starting mobile development.**

**Tasks**
- [x] Generate OpenAPI/Swagger and diff it against the **actual** 297 routes
- [x] Reconcile `.agents/skills/api-contract-sync.md` with reality (it predates
      Phases 4 and 6 — payment endpoints and notification routes have changed)
- [x] Document the new/changed endpoints: `/api/notifications/preferences/`,
      `/api/subscription/v1/payments/{id}/reconcile/`, removed `confirm`,
      changed `renew` semantics, ShamCash payment flow
- [x] Consistent error contract + error codes the app can branch on
- [x] Consistent pagination contract
- [x] Auth/refresh flow doc incl. token revocation on password reset (Phase 5)
- [x] Push notification payload contract (event types + `data` keys from
      `EVENT_CLASS_REGISTRY`, now 17 entries)
- [x] API versioning + breaking-change policy
- [x] **Freeze** — after this, contract changes are additive or versioned

**Done when:** a mobile developer can build against the docs without reading Django code.

---

## Execution order

1. **B1** — health endpoint (deploy blocker)
2. **Phase 14 (partial)** — clean install + `docker build` + smoke (proves it boots from
   a clean checkout; requirements pins were never exercised)
3. **Phase 7** → **Phase 8** → **Phase 10**   *(highest risk surfaces)*
4. **Phase 9** → **Phase 11** → **Phase 12**  *(depth + hygiene of failure paths)*
5. **Phase 13** → **Phase 14 (full)** → **Phase 15**
6. **Phase 16** — freeze contract → begin mobile

## Phase status board

| Phase | Title | Status | Notes |
|---|---|---|---|
| B1 | Health endpoint | ✅ | done + verified vs real Postgres |
| 7 | Routine & Social API security | ✅ | 13 findings fixed, exploit-proven |
| 8 | File upload & media security | ⬜ | NEXT — validator unused |
| 9 | AI Assistant & WebSocket | ⬜ | |
| 10 | Admin dashboard | ⬜ | 256 routes |
| 11 | Error handling & resilience | ⬜ | 91 swallows |
| 12 | Data protection & privacy | ⬜ | 18 PII sites |
| 13 | Performance & scale | ⬜ | |
| 14 | Tests & CI gates | ⬜ | zero tests today |
| 15 | Code hygiene | ⬜ | ~45 prints |
| 16 | API contract freeze | ⬜ | mobile gate |


---

# COVERAGE AUDIT — what phases 1-8 actually reached
*(measured, not estimated: 241 active .py files / 40,718 lines, excluding `_excluded/`,
migrations and tests)*

## How each area was reached

| Evidence | What it proves |
|---|---|
| **Route sweeps** (`sweep_5xx`, `sweep_anon`, `detail_sweep`) | 987 routes answered GET without 5xx, for 3 roles + anonymous. Proves *reachability and crash-freedom*, not correctness. |
| **Targeted probes** (22 suites in `tests/security/`) | Specific behaviours: authz, uploads, caching, wallet concurrency, pagination, timezone, file lifecycle. |
| **Read + modified** | 176 .py files changed across phases 1-8. |

## What the sweeps structurally could NOT reach

| Surface | Size | Status |
|---|---|---|
| **WebSocket consumers** — `ai_assistant/consumers.py` (309), `social/consumers.py` (65) | 374 lines | **never executed once.** Every sweep was HTTP. |
| **Celery tasks** — diet (282), ai_assistant (129), social (100), notifications (47), routine (43) | 601 lines | **never executed.** Run out-of-band; a broken task fails silently in a worker. |
| **Management commands** | 11 commands | **never executed.** |
| **`admin_dashboard`** | 688 lines | **never audited.** Privileged UI at `/dj-admin/`. |
| **`diet` internals** — `engine/`, `ai/`, `services/rule_based_planner.py` (1,686) | ~3,049 lines | Views swept; the planner/LLM pipeline itself was only reviewed in Phase 3, before the current dive protocol. |
| **`ai_assistant` internals** — `tools/`, `services/` | ~1,696 lines | Not audited. |
| **Signal receivers** (6 modules) | — | Business logic outside the request path; only `training_platform/signals.py` was exercised. |
| **Write paths with real payloads** | — | Sweeps sent GET everywhere, POST only anonymously, DELETE only on detail routes. **PUT/PATCH with valid bodies were never swept.** |

**Honest read: roughly 60% of the active codebase has been meaningfully exercised.**
The request/response layer of `users`, `wallet`, `routine`, `social`, `subscription` is
well covered. The asynchronous half of the system — consumers, tasks, signals, commands —
is essentially unverified, and it is where failures are silent.

---

# PHASE 8.5 — Write-Path Sweep   ✅ DONE
**Why:** every sweep so far sent GET. A viewset can be perfectly safe on read and broken
on write; F-01 was exactly that and was only caught by a hand-written probe.
**Scope:** for all 987 routes, generate a valid payload per serializer and exercise
POST/PUT/PATCH as owner, non-owner, wrong role, and anonymous.
**Done when:** no 5xx on any write path, and no non-owner write returns 2xx.

# PHASE 9 — AI Assistant & WebSocket Security   ✅ DONE
Add, beyond the existing scope: consumers have **never been executed**. Needs a
`WebsocketCommunicator` harness — connect anonymously, connect as the wrong user, send
oversized frames, verify the `ai_chat_limit` rate limit actually holds, and confirm the
per-user cost tracker cannot be bypassed by reconnecting.

# PHASE 9.5 — Background Jobs & Signals   ✅ DONE
**Why:** 601 lines of Celery tasks and 6 signal modules run outside every test performed
so far. A task that raises leaves a queue entry and no user-visible error.
**Scope:** execute each task synchronously with real args; assert idempotency on retry,
correct behaviour on partial failure, and that no task assumes an active request or a
logged-in user. Verify the DLQ path and that signal receivers cannot deadlock or recurse.
**Done when:** every task has a proven success and failure path.

# PHASE 10 — Admin Dashboard & Privilege Boundaries   ✅ DONE
688 lines at `/dj-admin/` never audited. Note the sweeps skipped every `admin/` path.

# PHASE 10.5 — Diet Engine & AI Pipeline Correctness   ✅ DONE
**Why:** `diet` is the largest app (10,964 lines) and its planner is 1,686 lines of rule
logic. Phase 3 reviewed it before the dive protocol existed, so it was never probed the
way `routine` was — no real-life scenario tests, no DB-level checks, no concurrency.
**Scope:** macro arithmetic against hand-computed expected values; plan generation across
month/DST boundaries (now that `timezone.localdate()` is in use); allergy/exclusion rules
that must never be violated; LLM failure and timeout fallback; cost caps.
**Done when:** the planner has property tests for every rule module in `diet/engine/`.

*(Phases 11-16 unchanged.)*
