# Audit Notebook — working memory

My scratch/progress file. `PRODUCTION_READINESS_PLAN.md` is the plan;
`SECURITY_AUDIT_FIXES.md` is the record of completed work. **This file is where I write
what I learned, what surprised me, and what I must not forget.**

Read this file FIRST when resuming work.

---

## 🧭 Current position

*(updated 2026-09-02, after the diet re-engineering and the wiring round)*

- **Phases 1–15 complete.** ~120 findings fixed.
- **The diet planner has been re-engineered** into `diet/planner/` — eight subsystems
  replacing a 1,791-line monolith's control flow. It now produces named dishes, converges
  inside tolerance, reports its own deviation, and learns from what users actually eat.
  See `DIET_REENGINEERING_PLAN.md`.
- **Everything unwired is wired**: server-side analytics (so achievements can award), a
  drainable notification DLQ, and `AchievementProgress` finally written.
- **There is now a gate.** `pytest -q` runs **27 tests in ~6 s**, one per defect this
  audit found. `ci/ci.yml` and `ci/security.yml` are staged and need moving by hand —
  this environment blocks writes to `.github/workflows/`:
  `git mv ci/ci.yml .github/workflows/ci.yml`

### Standing verification commands
```
.venv/bin/python manage.py check --settings=training_platform.settings_local
.venv/bin/python manage.py makemigrations --check --dry-run --settings=training_platform.settings_local
.venv/bin/python tests/security/sweep_5xx.py      # 383 routes x 4 roles
.venv/bin/python tests/security/sweep_anon.py     # unauthenticated
.venv/bin/python tests/security/detail_sweep.py   # 604 detail routes
.venv/bin/python tests/security/dive2_write_sweep.py  # 800 write requests
```

- **Open decisions awaiting owner:** OD-1 (trainer payout on subscription payment),
  OD-2 (rate-limit fail-open on Redis outage), OD-3 (drop the two dead Notification
  tables — owner said drop them; not yet executed), plus: the 80 foods still in the
  allergen review queue, and `ai_training_consent` needing a UI toggle.

## ⚠️ Things I must not forget

1. **I activated the health check.** Phase 5 moved it to `[[http_service.checks]]` so it
   now binds — and the endpoint doesn't exist. This is my own change turning a latent
   problem into an active deploy blocker. Fix before any deploy.
2. **requirements.txt pins were never exercised.** I added `boto3`, `langchain-core`,
   `pydantic`, `prometheus-client`, restored `python-dotenv`. The Docker image has never
   installed these. A clean `pip install` + `docker build` must be run before trusting it.
3. **Never trust "it exists" = "it runs."** Burned three times:
   `enforce_production_safety()` (wsgi only, not asgi), `file_security.py` (419 lines,
   0 call sites), `notifications/metrics.py` (dep missing, silently no-op).
   → **Always grep for call sites, don't assume wiring.**
4. **`except Exception: pass` is where bugs hide.** 91 of them. Both Phase 5/6 runtime
   bugs lived behind silent swallows.
5. **`_excluded/` is off-limits** (CLAUDE.md). Tests live there — Phase 14 needs NEW tests
   in the project, not resurrection of those.
6. **Never run `migrate` without confirming the DB target** (CLAUDE.md).
7. **Migrations pending application:** `wallet/0004` (agent secret ciphertext),
   `subscription/0004` (payment idempotency + state fields). Not applied by me.
8. **`challenges` is NOT dead** — I wrongly reported it as dead in Phase 6. It's an
   admin-grouping proxy layer. Corrected in the record.
9. **Output rules (CLAUDE.md):** findings only, `file.py:L42` refs, compact severity
   format, no code blocks unless asked, skip LOW/INFO unless asked.

---

## 🔑 Verification commands that work

```bash
# System check
DJANGO_SETTINGS_MODULE=training_platform.settings_local .venv/bin/python manage.py check

# Import every project module (catches broken refs) — expect 166 OK / 0 failures
DJANGO_SETTINGS_MODULE=training_platform.settings_local .venv/bin/python -c "
import django,pkgutil,importlib; django.setup()
bad=[];n=0
for a in ['users','routine','diet','subscription','challenges','analytics','achievements','social','admin_dashboard','wallet','notifications','ai_assistant','training_platform']:
    p=importlib.import_module(a)
    for m in pkgutil.walk_packages(p.__path__,a+'.'):
        if 'migrations' in m.name or 'tests' in m.name: continue
        try: importlib.import_module(m.name); n+=1
        except Exception as e: bad.append((m.name,str(e)[:80]))
print('OK',n,'FAIL',len(bad)); [print(' x',x) for x in bad]"

# Route inventory
DJANGO_SETTINGS_MODULE=training_platform.settings_local .venv/bin/python -c "
import django; django.setup()
from django.urls import get_resolver
def w(p,pre=''):
    o=[]
    for e in p.url_patterns:
        o += w(e,pre+str(e.pattern)) if hasattr(e,'url_patterns') else [pre+str(e.pattern)]
    return o
r=w(get_resolver()); print('total',len(r))
for g in ['api/auth/','api/routine/','api/social/','api/diet/','api/subscription/','api/wallet/','api/ai/','api/notifications/']:
    print(g,len([x for x in r if x.startswith(g)]))"

# Pending migrations
DJANGO_SETTINGS_MODULE=training_platform.settings_local .venv/bin/python manage.py makemigrations --check --dry-run

# Event pipeline integrity (expect 12 events / 12 listeners / 17 templates / 0 unresolvable)
DJANGO_SETTINGS_MODULE=training_platform.settings_local .venv/bin/python -c "
import django; django.setup()
from notifications.domain.dispatcher import EventDispatcher
from notifications.channels.fcm import EVENT_CLASS_REGISTRY,_resolve_event_template
print('listeners',len(EventDispatcher._listeners),'registry',len(EVENT_CLASS_REGISTRY),
      'unresolvable',[k for k in EVENT_CLASS_REGISTRY if _resolve_event_template(k) is None])"
```

**Note:** local Postgres (port 5433) is usually **down** — `makemigrations --check` warns
about connection refused but still works. Firebase creds path is set but file is absent
locally → a CRITICAL log line on boot. Both are expected locally, not bugs.

---

## 📐 Architecture facts worth remembering

- **Runtime is ASGI/Daphne**, not WSGI. Anything that must run at startup belongs in
  `asgi.py` (wsgi.py alone is not enough).
- **Canonical notification path:**
  `emit_event(Event)` → Celery `process_event_task` → `EventDispatcher.dispatch` →
  listener → `NotificationService.create_and_send` → `notifications.Notification` → FCM.
  Read API is `/api/social/notifications/`. Preferences are `/api/notifications/preferences/`.
  `routine.Notification` and `social.Notification` are **DEPRECATED — do not write.**
- **Payment authority:** `PaymentService.complete_payment()` is the ONLY code that may
  complete a payment or activate a subscription. Webhook and reconcile are both just
  *callers*. Gateway is **ShamCash** (reconcile mode by default; hosted mode when
  `SHAMCASH_INITIATE_PATH` is set).
- **Redis segmentation:** DB0 sessions · DB1 ratelimit · DB2 public cache ·
  DB3 private cache · DB4 Edamam · DB5 channels. Use `training_platform.cache` helpers —
  **the module is `cache`, NOT `cache_backends`** (that wrong import caused two bugs).
- **Agent caps are fail-closed:** `0` means *no top-ups*, not unlimited. Default $200/day.

---

## 📊 Baseline metrics (2026-07-09)

| Metric | Value |
|---|---|
| Python files (excl. migrations/tests) | 221 |
| Lines of code | 39,346 |
| Modules importing cleanly | 166 / 166 |
| Total routes | 986 (≈297 API + 256 admin) |
| Migrations | 84 |
| `except Exception: pass` | 88 |
| bare `except:` | 3 (all in `subscription/permissions.py`) |
| `print()` in app code | ~45 |
| PII log sites | 18 |
| Tests | 0 |
| Celery tasks / with `acks_late` | 11 / 0 |

---

## 📝 Progress log

| Date | Phase | What happened |
|---|---|---|
| — | 1 | Auth & OTP — 5 findings fixed (trainer self-assign CRITICAL, agent self-reg, XFF spoofing, login lockout, cache segment) |
| — | 2 | Wallet — 6 fixed (fail-closed caps CRITICAL, encrypted agent secrets, dead HMAC, reversal guard, idempotency, audit chain) |
| — | 3 | Diet — 7 fixed (meal distribution HIGH, trainer calorie validation, IDOR default-deny, prompt sanitization, RNG, dead code) |
| — | 4 | Subscription — full architectural rebuild: `PaymentService` single authority, state machine, ShamCash, 4 CRITICAL free-access paths closed |
| — | 5 | Middleware/settings/deploy — 13 fixed (ASGI kill-switch CRITICAL, cachalot bypass, collectstatic, DB2/DB3 leak, token revocation) |
| — | 6 | Dead code & wiring — notification fragmentation repaired, 4 dep gaps found in fresh dive (incl. `boto3` = prod boot blocker), ~400 lines dead code removed |
| 2026-07-09 | — | Full re-scan; wrote `PRODUCTION_READINESS_PLAN.md` (Phases 7–16) + this notebook |
| 2026-07-09 | B1 | Health endpoint added (+ rate-limit & SSL-redirect exemptions). Verified 200 vs real Postgres. 2 pending migrations applied to local dev DB |
| 2026-07-09 | 7 | 13 findings fixed, all proven by exploit then re-proven blocked. Root-cause helper `can_access_user_data()`. 14/14 + 7/7 legitimate-access checks pass, 0 regressions. Probes saved to `tests/security/` |

---

## 🐛 Findings parked for their phase (do not lose)

| Finding | Phase |
|---|---|
| `/api/auth/health/` 404 while fly.toml polls it | B1 |
| `routine/views.py:834` routine fetched by id, no ownership check | 7 |
| 3 unscoped `get_queryset` in social, 1 routine, 1 achievements | 7 |
| `file_security.py` 419 lines, 0 call sites; uploads trust `Content-Type` header | 8 |
| No `OriginValidator` in `asgi.py` for WebSockets | 9 |
| Admin can edit `Subscription.status` / `Wallet.balance`, bypassing service invariants | 10 |
| 3 bare `except:` in `subscription/permissions.py` (authorization layer) | 11 |
| 0/11 Celery tasks use `acks_late` | 11 |
| `social/firebase_service.py:118` logs a FULL FCM token | 12 |
| `WalletAuditLog` append-only not enforced at DB layer | 12 |
| 4 query-in-loop sites `routine/views.py`, 2 `achievements/views.py` | 13 |
| `api-contract-sync.md` predates Phases 4 & 6 — stale | 16 |
| `Exercise.clean()` invariant (created_by ⇒ not global) never runs on `.create()` | 13/15 |
| `update_progress` shadows the `status` import (latent, not live) | 15 |
| `RoutineProgressViewSet` scopes trainers via `assigned_trainer`, not `TrainerClientRelation` (inconsistent with new helper) | 15 |
| Unpinned deps drift on rebuild: `openai>=1.0.0` resolved to **3.6.0** in a clean venv vs 2.30.0 locally | 14 |

---

## 🔁 Method lessons that changed how I dive (2026-09-02)

These cost me real findings. They are the reason later phases caught more.

1. **Sweeps that only send GET prove almost nothing.** `sweep_5xx` was green for two
   phases while `POST /api/social/follows/` 500'd on every call and any user could
   DELETE another user's post. Write paths need their own sweep with real payloads.
2. **`<pk>` routes were excluded from every sweep for eight phases** — 604 of 987 routes,
   61% of the surface, and every 5xx found in Phase 8's second dive lived there.
3. **Read `get_permissions()`, never `permission_classes`.** A viewset declared
   `IsAuthenticated` and overrode it to something that admitted anonymous users.
4. **Verify config by resolving it, not by reading it.** `static()` returns `[]` when
   `DEBUG=False`; I "fixed" media serving by moving the call and it stayed inert.
5. **A test that encodes the bug will pass forever.** `cache_behaviour` asserted that an
   entitlement-gated route SHOULD share a public cache key.
6. **Silence is not proof of safety.** 112 `except: pass` handlers are exactly why the
   diet engine's defects never appeared in a log.
7. **Check the formula before calling something a race.** My first "lost update" used
   weight x reps where the field is a sum of weights. The race was real; the number was
   not — sequential-vs-concurrent divergence is what actually proved it.
8. **A row lock does not fix a read-modify-write across two statements.** The recalc race
   survived `select_for_update`, then survived `on_commit`, and only died when the
   aggregate and the write became a single SQL statement.
9. **Bulk-editing source by class name needs a bounded window.** Twice I matched a class
   and edited past its end into the next one (`readonly_fields`, index insertion).
   Always slice to the next `\nclass ` before searching.
10. **Single-line `except X: pass` breaks line-based rewriting** — the handler header and
    body share a lineno.
