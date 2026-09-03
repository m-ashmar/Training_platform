Do a fresh, independent audit of this Django backend. I am preparing it for launch and
I want what previous audits did not see.

**Ground rules**

1. Derive everything from the code and from running it. Do not trust any `.md` file in
   this repo, including `BUG_REGISTRY.md`, `SECURITY_AUDIT_FIXES.md`, `API_CONTRACT.md`
   and `SYSTEM_OVERVIEW.md`. They were written by a previous audit and some of their
   claims were wrong. Treat them as leads, never as evidence.
2. Do not trust code comments either. Several described behaviour that did not exist —
   one said WhiteNoise served static files in production when WhiteNoise was in neither
   MIDDLEWARE nor STORAGES.
3. Report findings only. No summaries of what is clean.

**The database is Postgres on localhost:5433 (Docker). Use it.**
This matters more than anything else below — see why in the next section.

---

## The two worst bugs found so far, and what they have in common

- `OTPVerification.otp_code` was `varchar(6)` while holding a 64-character hash, so
  **every registration died** with `value too long for type character varying(6)`.
  SQLite ignores CharField width, so it only fails on Postgres.
- `/api/auth/login/` returned dj-rest-auth's `{"key": "<DRF Token>"}` while
  `DEFAULT_AUTHENTICATION_CLASSES` held `JWTAuthentication` only. The key authenticated
  nothing — **the primary entry point handed out a dead credential**.

Both were invisible to reading. Both survived fifteen phases of audit. Both were on
paths no test executed. Neither is subtle once you actually run the flow.

**So: execute, do not read.** Reading this codebase finds little that is left. Running
it finds things immediately. Prefer, in this order:

1. Drive real HTTP journeys end-to-end against real Postgres and assert on responses.
2. Compare what is *declared* against what is *wired* — this codebase's dominant defect
   is a thing that exists and is honoured by nothing. Confirmed instances: a scheduled
   Celery task never registered (beat skips unknown names silently, so the GDPR
   retention purge had never run once); `FIELD_ENCRYPTION_KEY` documented and read
   nowhere; `preferred_timezone` declared "for localized dates and times" and used
   nowhere; a `scheduled_date` column null on all 251 rows; notification templates with
   no producer. Assume more exist and go looking systematically.
3. Only then read code.

---

## Where to look, roughly in order of how little has been verified

- **WebSockets** — `ai_assistant/consumers.py`, `social/consumers.py`. Connection auth,
  group membership, whether one user can subscribe to another's stream, message
  authorization, what happens on disconnect mid-operation. Barely touched so far.
- **Payment gateways** — `subscription/gateways/` (baraka, bemo, syriatel) and Stripe.
  Webhook signature verification, replay protection, idempotency, what happens when a
  webhook arrives twice or out of order, whether a failed charge can leave a
  subscription active. Money, so the bar is highest here.
- **Concurrency** — run parallel requests, not sequential ones. Subscription
  activation, achievement awards, diet plan generation, wallet transfers, set logging.
  Look for lost updates, double-awards, and check-then-act races.
- **Celery reliability** — at-least-once delivery means every task can run twice. Which
  ones are not idempotent? What happens to a task whose user was deleted mid-flight?
  Poison messages, retry storms, tasks that swallow their own failures.
- **Cache correctness** — `django-cachalot` plus six segmented Redis DBs. Stale reads
  after writes, cross-tenant leakage between users, invalidation that does not fire.
- **i18n / Arabic** — the API serves `en` and `ar`. Content imported outside the ORM
  once left 542 of 554 exercise names blank through the API. Check that translated
  fields actually resolve, and that error messages, notification templates and
  serializer output are correct under `Accept-Language: ar`.
- **File uploads and media** — signed URLs, path traversal, content-type spoofing,
  what an authenticated user can read that belongs to someone else.
- **Permission boundaries beyond trainer/client** — the `agent` role has wallet and API
  key access. Map what each of the four roles can actually reach, by executing it.
- **Performance at real volume** — seed realistic data and look for N+1s and unbounded
  queries on the endpoints a mobile client hits on every screen.

---

## What is already covered — verify rather than repeat

There is a pytest gate at `tests/test_regression_gate.py` (51 tests, ~6s,
`python -m pytest -q`) and standalone probes in `tests/security/`. Route sweeps report
0 5xx across ~3,000 requests.

Run them first to confirm they pass and that they assert what they claim. Then go
somewhere they do not reach. If you find one of them is testing the wrong thing, that
is itself a finding — more valuable than a new bug.

---

## Output

Follow the format in `CLAUDE.md`. For each finding give me:

- the **failure scenario**: concrete inputs or state, and the wrong output or crash
- how you **verified** it — the command and its actual output, not reasoning
- severity, and whether it is **live** or **latent** (breaks only under some future
  condition). Be explicit about which; a latent issue reported as live wastes my time.

Tell me plainly when something is a guess. I would rather have five findings you proved
than thirty you inferred.
