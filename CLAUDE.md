# Training Platform (Yalla Gym) — Claude Code Project Context

## What this is, and where it stands

Django 5.1 REST + WebSocket backend for a Flutter mobile app. Arabic-first, Syrian market,
payments through ShamCash. **Phase: building to production** against the owner's payments
and store-compliance spec. Not yet deployed.

**Start every session here, in this order, and read nothing else until you need it:**

| File | What it gives you |
|---|---|
| `PAYMENTS_COMPLIANCE_PLAN.md` | The work state: 12 build steps, what is done, what is next, the owner's decisions, the ready-made prompt for each step |
| `API_CONTRACT.md` | Every route. A gate test diffs it against the URLconf — a new route that is not listed fails the suite |
| `SYSTEM_OVERVIEW.md` | Architecture map. Read the section you need, not the file |

## Architecture at a glance

```
Python 3.12 · Django 5.1.3 · DRF 3.15 · Channels 4 (Daphne) · Celery 5.5
PostgreSQL (prod, and tests: Docker on :5433) · Redis, 6 logical DBs · RS256 JWT + OTP
Docker → Fly.io (Amsterdam)
```

Roles: `client`, `trainer`, `agent` (cash top-ups), `admin`.

| App | What lives there |
|---|---|
| `users` | CustomUser, OTP, registration, trainer–client relations and the coaching hold (`trainer_relations.py`) |
| `wallet` | Wallet, append-only `Transaction` ledger, `move_funds_atomic`, audit log, agent top-ups |
| `subscription` | Plans, `Subscription` (the entitlement), `Payment`, `services/payment_service.py` (the ONLY code that moves a payment's state), `gateways/shamcash.py` |
| `policy` | The four kinds of money (`money.py`) and what each build may show and sell (`services.py`, `models.py`, `defaults.py`) |
| `diet` | 54 endpoints, rule engine in `engine/`, LLM pipeline in `ai/` |
| `routine`, `social`, `ai_assistant`, `achievements`, `analytics`, `notifications`, `challenges`, `admin_dashboard` | As named. Operator console is `admin_dashboard` at `/dj-admin/` |

Settings: `settings_base.py` (shared) · `settings_local.py` (tests, dev) ·
`settings_production.py` (kill-switch `enforce_production_safety()`) · secrets only through
`require_env()` / AWS Secrets Manager.

Mobile: `yalla_gym/` is the Flutter client, built by its own sessions from the design canvas
and from THIS SOURCE CODE — not from documents (owner, 2026-09-19). A backend session does
not edit it. `mobile/` and `flutter/` are abandoned attempts: never read or touch them.
`_excluded/` is not part of the codebase: never read or modify it.

## Commands

```bash
docker compose up -d db                                                    # Postgres 15 on :5433
.venv/bin/python manage.py check --settings=training_platform.settings_local
.venv/bin/python manage.py makemigrations --check --dry-run --settings=training_platform.settings_local
.venv/bin/python -m pytest tests/test_policy_channels.py -q -p no:cacheprovider      # one file: seconds
.venv/bin/python -m pytest --create-db -q -p no:cacheprovider > <scratchpad>/suite.log 2>&1   # full gate: ~35 min
```

Run the full gate in the background, to a log file, and read only the failures. **Never
edit a `.py` file while it runs** — several gate tests read source from disk and a mid-run
edit produces a false failure. After adding a migration, the gate is `--create-db`.

## Rules that always hold

**Money**
- Every payment and ledger row carries a `money_type`; every `move_funds_atomic(...)` call
  names it. A payment's state changes only inside `PaymentService`.
- Any endpoint that opens a checkout or moves wallet money calls
  `policy.services.require_purchase_allowed(request, money_type)` FIRST.
- Rule zero of the spec: no hidden flows, no reviewer detection. Behaviour may depend on the
  build (`X-App-Channel`, `X-App-Version`) and on nothing else — never on user, country, IP,
  date or a "review mode". Plan and routine-product purchasing on `play`/`ios` is locked off
  in code; do not add a way around it.
- Lock order is subscription → payment, user rows → wallets (ascending id). Money moves on
  commit, idempotently. Read the docstrings in `payment_service.py`, `wallet/models.py` and
  `users/trainer_relations.py` before changing any of them.
- Owner's numbers (2026-09-19): platform keeps 20% of every coaching fee; plan payments stay
  100% with the platform; minimum age 14.

**Tests**
- Never edit a test's assertion to match code, and never restore an old baseline to make a
  gate green. A deliberate contract change updates shared setup or the allow-list, with the
  reason in a comment.
- Thread-based tests (`test_zz_*`) flush every table: never rely on rows seeded by a migration.
- Diet engine changes are measured on `tests/diet_quality.py` before they land; the baseline
  is re-recorded only deliberately, with the reason written in it.

**API and docs**
- Contract changes are additive, or go behind a new version prefix. The app branches on
  `code`, never on message text, so a `code` value never changes.
- The mobile sessions read the views and serializers themselves: the docstring of a view is
  its contract. Say there what the endpoint needs (headers, body), every `code` it can
  answer, and what differs by build. New route → `API_CONTRACT.md` in the same commit (the
  gate enforces it). `FRONTEND_HANDOFF.md` is no longer read by anyone — do not maintain it.
- Every user-facing string goes through gettext; Arabic is the primary language.

**Never**
- Never run `manage.py migrate` against anything but the local test database without
  confirming the target. Never commit `.env` or a credential file. Never set `DEBUG=True` in
  production settings. Never bypass the OTP flow. Never push, and never merge into
  `noti-system` or `main`, unless the owner asks.

## How to work (keeps quality up and usage down)

1. One build step per session. The state lives in `PAYMENTS_COMPLIANCE_PLAN.md`, not in the
   conversation — finish by updating it, then the owner starts a fresh session.
2. Work on the branch the plan names. Commit when the step's tests and the full gate pass;
   say exactly what was run.
3. Build inline. Subagents only for independent parallel work, at most 10 per task.
4. When you learn something the next session needs (a trap, a convention, an owner
   decision), write it where it will be found: a rule here, state in the plan, a preference
   in memory. Do not leave it in the chat.
5. Project conventions and known bugs: `.agents/skills/django-conventions.md`,
   `debug-checklist.md`, `api-contract-sync.md`, `deploy-pipeline.md` — read the one that
   matches the area before changing it. Skip `flutter-conventions.md` and
   `nextjs-conventions.md`.

## Output rules (apply to every response)

1. No introductions, no conclusions, no follow-up offers. Silence means no issue.
2. No code reproduction — reference `file.py:L42`; paste code only when asked.
3. Tables over paragraphs when comparing items.
4. Build work ends with: what changed, what was run and its result, decisions the owner owes.
5. Audits and reviews use this exact shape per finding, CRITICAL/HIGH/MEDIUM only unless asked:
   ```
   [SEVERITY] file.py:L<line> — <issue in one line>
   Impact: <one line>
   Fix: <one line>
   ```
   Dead code is a bullet list: `app/file.py:L<line> — <what is unused>`. A phase with no
   findings is `## PHASE N — TITLE: CLEAN`.
