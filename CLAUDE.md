# Training Platform — Claude Code Project Context

## What This Project Is

Production Django 5.1 REST + WebSocket backend serving a Flutter mobile app.
Current state: **final pre-production audit phase — not yet in production.**

Read `SYSTEM_OVERVIEW.md` for the complete architecture map before doing anything else.

---

## Architecture at a Glance

```
Language:    Python 3.12
Framework:   Django 5.1.3 + Django REST Framework 3.15.2
ASGI Server: Daphne (WebSockets via Django Channels 4.x)
Database:    PostgreSQL (prod) / SQLite (dev: db.sqlite3)
Cache:       Redis — 6 segmented logical DBs (DB0–DB5)
Jobs:        Celery 5.5 + Redis broker
Auth:        RS256 JWT (simplejwt) + custom OTP flow
Deployment:  Docker → Fly.io (Amsterdam)
```

## User Roles
- `client` — end-user athlete
- `trainer` — manages client routines and diet plans
- `agent` — financial agent with wallet + API key access
- `admin` — platform superuser

## 12 Django Apps

| App | Key Files |
|---|---|
| `users` | models.py (CustomUser, OTP), views.py (29 routes), utils.py |
| `wallet` | models.py (Wallet, Transaction, AuditLog), views.py (12 routes), security.py |
| `diet` | views.py (54 routes, ~92KB), engine/ (10 rule modules), ai/ (LLM pipeline) |
| `routine` | views.py (10 routes), models.py, permissions.py |
| `subscription` | views.py, gateways/ (baraka, bemo, syriatel), Stripe |
| `social` | views.py, consumers.py (WebSocket), services.py |
| `ai_assistant` | consumers.py (WebSocket AI chat), tools/ (5 tools), services/ |
| `achievements` | engine.py, registry.py (signal-driven award system) |
| `analytics` | views.py (progress tracking) |
| `notifications` | channels/fcm.py (Firebase), domain/, listeners/ |
| `challenges` | basic fitness challenges |
| `admin_dashboard` | custom admin UI at /dj-admin/ |

## Settings Split
```
training_platform/
├── settings_secrets.py   ← require_env(), get_secret(), AWS Secrets Manager
├── settings_base.py      ← shared config, zero hardcoded secrets
├── settings_local.py     ← DEBUG=True, SQLite, LocMemCache
├── settings_production.py ← DEBUG=False hardcoded, enforce_production_safety() kill-switch
└── settings.py           ← environment router
```

## Security Posture
- JWT: RS256 asymmetric (private key signs, public key verifies)
- OTP: `secrets` module, hash-stored, constant-time compare, 5-attempt lockout
- Redis: 6 isolated DBs (sessions/ratelimit/public/private/edamam/channels)
- Wallet: atomic transfers, append-only audit log, WALLET_DEV_MODE hardcoded False in prod
- Rate limiting: 3-layer (CDN → NGINX → django-ratelimit on Redis DB1)
- Production kill-switch: `enforce_production_safety()` crashes process on misconfiguration
- All secrets: zero hardcoded — loaded via `require_env()` or AWS Secrets Manager

## Excluded Directories
The `_excluded/` folder contains files NOT part of the architecture:
- All test files and test directories
- All database migrations
- All fixture/datadump JSON files  
- Debug/verify/generate one-off scripts
- Firebase credential JSON
- Media, static, .venv

**Never read files from `_excluded/` — they are not part of the active codebase.**

## Key Files for Audit Priority
1. `training_platform/settings_*.py` — production config correctness
2. `training_platform/middleware.py` — 17-layer middleware stack
3. `users/views.py` + `users/utils.py` — auth, OTP, registration
4. `wallet/models.py` + `wallet/views.py` — financial logic
5. `diet/views.py` — largest file (54 endpoints, ~92KB)
6. `diet/engine/` — 10 rule-based planner modules
7. `subscription/gateways/` — 3 payment gateway integrations
8. `routine/permissions.py` — trainer/client boundary enforcement

## Project Skills (read these for platform-specific patterns)

These files document established conventions and known issues for this project.
Read them before auditing the relevant area — they contain project-specific rules that
Fable should use to detect deviations:

| File | Read When |
|---|---|
| `.agents/skills/django-conventions.md` | Auditing any Django app — OTP flow, cache keys, serializer rules, view patterns |
| `.agents/skills/debug-checklist.md` | Cross-checking known production bugs — verify they are fixed, not just documented |
| `.agents/skills/api-contract-sync.md` | Auditing users/views.py — verify actual endpoints match declared API contracts |
| `.agents/skills/deploy-pipeline.md` | Auditing settings_production.py, Dockerfile, fly.toml — verify all required env vars are covered |

**Skip entirely (not part of this audit):**
- `.agents/skills/flutter-conventions.md` — Flutter client, not Django backend
- `.agents/skills/nextjs-conventions.md` — Next.js does not exist in this project

---

## What to NEVER Do
- Never modify files in `_excluded/`
- Never run `python manage.py migrate` without confirming DB target
- Never commit `.env` or any credential file
- Never set `DEBUG=True` in settings_production.py
- Never bypass the OTP flow in users/views.py

---

## Output Format Rules (apply to ALL responses)

These rules are mandatory. They exist to minimize output token usage without losing quality.

1. **Findings only** — do NOT narrate what you checked or summarize clean sections. Silence = no issue.
2. **No code reproduction** — reference by `file.py:L42` format, never paste code blocks unless asked.
3. **Compact finding format** — use this exact structure per finding:
   ```
   [SEVERITY] file.py:L<line> — <issue in one line>
   Impact: <one line>
   Fix: <one line>
   ```
4. **No introductions or conclusions** — start directly with findings. No "I will now analyze..." or "In summary..."
5. **Skip LOW and INFO** unless explicitly asked — focus on CRITICAL, HIGH, MEDIUM only.
6. **Dead code** — list as a simple bullet list: `app/file.py:L<line> — <what is unused>`
7. **Tables over paragraphs** — if comparing multiple items, use a markdown table.
8. **No follow-up offers** — do not end with "Would you like me to..."
9. **Phase headers only** — use a single line `## PHASE N — TITLE` between phases, nothing else.
10. **If a phase has zero findings** — write `## PHASE N — TITLE: CLEAN` and move on.
