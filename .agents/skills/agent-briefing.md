# Briefing for an agent born into this repo

Read this before your first edit. It is what earlier agents paid for in wasted runs — the
traps are not in the code's comments because they are about the HARNESS, not the code.
Project rules live in `CLAUDE.md`; conventions in `django-conventions.md` beside this
file. This one is about how work actually lands here without being thrown away.

## The gate decides, and it is slow

The full suite is ~35 minutes and ~850 tests. Never run it to find out whether your own
change works — run your own file, then the two or three suites nearest it. Only the
session that merges runs the whole thing.

**Never edit a `.py` file while any test run is in progress.** Several gate tests call
`inspect.getsource` and read the file from disk, so a mid-run edit produces a failure
that has nothing to do with your change. This has cost more than one full gate run.

`pytest -q` in this repo prints no summary line. Trust the **exit code**; count dots if
you want a number.

After adding a migration, the run needs `--create-db` or it dies with `DuplicateColumn`.

## Gate tests that will fail work that is otherwise correct

| It checks | What you must do |
|---|---|
| `API_CONTRACT.md` diffed against the URLconf | Every new route goes in that file **in the same commit**. A route not listed fails the suite |
| The privacy registry | A model holding personal data must be registered under `training_platform/privacy/` |
| The data inventory (`tests/test_store_docs.py`) | A registered source declares `purpose`, `recipients` (`()` = nobody) and `data_types`, keys of `training_platform/privacy/disclosure.py`. And `docs/store/data_inventory.md` must be `manage.py data_inventory`'s output byte for byte: a new source, **a new or renamed field on any registered model** (a migration on `CustomUser` included) or a changed purpose fails until you run `python manage.py data_inventory --settings=training_platform.settings_local` and commit the file. A new data type must also be answered in `docs/store/privacy_labels.md` and named in both `docs/legal/privacy_policy_*.md` |
| Every registered notification event is emitted | A key in `notifications/channels/fcm.py` `EVENT_CLASS_REGISTRY` needs an event class, **a listener**, and a LITERAL `event_type="..."` in non-test source. A test asserting `emit_event` was called does NOT prove delivery — assert the `Notification` row |
| Every push event is in the contract | A new `event_type` must also be a row of `API_CONTRACT.md` §4 (the push table), with its `data` keys (`test_s009_every_registered_push_event_is_in_the_contract`) |
| Ordering is total | A list view's `order_by` must end in a unique column (`'-created_at', '-id'`); a scan fails `order_by('created_at')` alone |
| Beat entries resolve | Every `CELERY_BEAT_SCHEDULE` entry must name a real registered task |
| The Arabic catalogue | **Every string you pass to `_()` — and every `{% trans %}` — must be IN `django.po` with Arabic, and in the compiled `django.mo`.** Not "every entry already in the file": the gate used to check only those, so a string that never reached the catalogue was invisible and shipped in English (SIDE_BUGS S-019 — 152 of them). None fuzzy. Add the entry, then `msgfmt --check-format -o locale/ar/LC_MESSAGES/django.mo locale/ar/LC_MESSAGES/django.po` and commit the `.mo` |
| One predicate decides what a trainer sees | A source scan forbids reading `assigned_trainer` directly — use the approved relation |
| The venv IS the lock (`tests/test_django_lts.py`) | Every package `requirements.lock` pins must be installed at exactly that version where the tests run, or the gate fails. A worktree's symlinked `.venv` is the main checkout's: if it predates the lock, run `.venv/bin/pip install --require-hashes -r requirements.lock` in the main checkout — only when no other agent is running tests on it. Never `pip install` a package by name to get past this; change `requirements.txt` and regenerate the lock (its header says how) |
| Nothing Django 6.0 removes | An AST scan fails `format_html()` with no arguments (pass the text as an argument, or use `format_html_join`) and `CheckConstraint(check=...)` (it is `condition=`); `manage.py check` must start with no deprecation warning and no issue |
| CI's `security-scan` job, and `tests/test_security_scan_job.py` beside it | Semgrep (on GitHub only, ERROR rules) reads every tracked text file: a socket address written with the plain ws scheme fails it even in a docstring or a `.md` (write `wss://<host>/ws/...`). A checked false positive gets its reason in a comment and a line `nosemgrep: <the full rule id>` just before it, never a directory exclusion. The secret grep fails a key-SHAPED string in any tracked file, tests included: build a test key at run time from a prefix and random characters, never as a literal |

Not a gate, and just as silent: **a new app with a `FileField` must be added to
`LOCAL_APPS_FOR_FILE_CLEANUP` in `training_platform/signals.py`**, or a deleted or
replaced row leaves its file in storage for ever — account erasure included. Nothing
fails; the bytes simply stay (found building `trainer_verification`, 2026-09-24).

**A token minted from a stale user object is refused** (2026-11-24). Every token
carries a digest of the password hash it was made under, and a token made under another
one answers 401 `password_changed` (`training_platform/jwt_sessions.py`). After a
password change or reset made THROUGH THE API, the test's `user` object still holds the
old hash, so `api(user)` mints a dead token: `user.refresh_from_db()` first.

Two more that fail silently (found building `payouts`, 2026-09-24):

- **`Wallet.owner_type` is not the owner's role.** It is written once, when the wallet is
  created, from `user_type` AT THAT MOMENT — and `make_user(..., user_type="trainer")`
  creates the user as a client first, so every test trainer's wallet says `"client"`.
  Branch on `wallet.owner.user_type`, or on the ledger, never on `owner_type`.
- **An admin form field named like a read-only model field vanishes.** `ModelAdmin.
  get_form` removes a declared form field whose name is in `readonly_fields`, so the
  POSTed value never reaches `cleaned_data` and the page still answers 302. Name console
  inputs apart from the model's columns (`transfer_id`, not `shamcash_transaction_id`).
- **The ledger refuses UPDATE and DELETE at the database** (`wallet/migrations/0021`,
  2026-09-27), like the audit log and the agent receivable. `Transaction.objects.filter(
  ...).update(created_at=...)` in a test now fails with "append-only"; back-date a row
  with `tests/ledger_rows.py backdate(pk, when)`, and never write production code that
  edits a row — a correction is a new row (`reverses`). A wallet that has ledger rows
  can no longer be deleted either.
- **A payment's status moves in ONE line, and that line writes an event**
  (`PaymentService._move`, 2026-09-27). `subscription.PaymentEvent` refuses UPDATE and
  DELETE at the database (migration `subscription/0016`), like the ledger, and a source
  scan (`tests/test_payment_events.py`) fails on any other `payment.status = ...` or a
  `Payment.objects...update(status=...)` outside the service. A new service method that
  moves a state calls `_move(payment, to, by=..., actor=..., evidence=...)`; a new
  caller passes `by` (`PaymentEvent.By`: who or what it is). A test that writes
  `Payment.objects.create(status=...)` directly gets a payment with NO events — fine for
  what it tests, but not a payment the app would ever see.
- **A payment can have NO subscription** (`build/hub-checkout`, 2026-09-27). A routine
  product bought through the web hub's checkout (`api/v1/checkout/sessions/`) is a
  `Payment` with `buyer` and `routine_product` set and `subscription` NULL
  (`payment_pays_for_one_thing`). Code that reads `payment.subscription.user` crashes on
  one: read `payment.payer` / `payment.payer_id`, and filter "the caller's payments" with
  `subscription.models.paid_by(user)`. `PaymentService._lock_subscription_and_payment`
  answers `(None, payment)` for one. `payment_completed` is NOT sent for it (its program
  and ledger rows are written inside the completion, `routine.products.grant_checkout`).
- **A periodic task over every user takes a page and hands the rest on**
  (`training_platform/batching.py`, 2026-09-27). A new beat task that loops over users
  uses `Batch` the same way, and its per-row work must be idempotent: a retried page
  runs again. A test that wants to see the pages patches the task's `apply_async` (see
  `tests/test_prod_data.py _drain`); under eager Celery the hand-on runs nested.
- **Stored files leave storage through a task after the commit**
  (`training_platform/storage_cleanup.py`). Code that deletes rows holding files calls
  `delete_after_commit(field_label(field), names)`; never `storage.delete()` inline in
  a request (identity documents are the one deliberate exception).
- **A `DietPlan` can be a trainer's unsent draft** (`is_draft`, BACKEND_BUGS B-132,
  2026-09-25): inactive, empty, and not the client's yet. A read that filters
  `is_active=True` or goes through `DietPlan.governing` already skips it; a CLIENT read
  that lists a user's plans or fetches one by id must exclude `is_draft=True` itself
  (`MyDietPlansView` and the two plan reads do). A write that retires plans must
  filter `is_active=True`, never "not mine", or it will retire a draft nobody follows.

Two about who owns a routine or a template (Road to 10 S2.4, 2026-11-23):

- **A template can have NO author.** `RoutineTemplate.created_by` is NULL on a template
  the PLATFORM wrote (`platform_key` set: the free first workouts, the starter templates;
  `routine/data/platform_templates.py`, written by `bootstrap_platform`). Code that reads
  `template.created_by.<anything>` crashes on one: test `created_by_id is None` first.
- **A client can hold a routine nobody assigned.** Their copy of the free first workout is
  `created_by` the client with `trial=True` and is NEVER in `assigned_to` (an assignment
  is what makes a routine a programme). `readable_routines` lets its author read it; the
  `routines/` list, the schedule, home's hero and the streak (`outcomes._counted`) leave
  it out. A new "the person's programme" read filters `assigned_to`, as every one does.

One about user-written text (found building `build/ugc-filter`, 2026-09-27):

- **The banned-terms list is live in every test that writes through the API.**
  Migration `moderation/0004` seeds ~60 terms (`wa.me/`, `t.me/`, `shit`, `fuck`, Arabic
  obscenities and slurs, "للتواصل واتس"...), and a post, comment, challenge, profile
  name or bio, or sign-up username holding one answers 400 `content_not_allowed`
  (`moderation/ugc_filter.py`). Test text that needs a link or a curse fails for that
  reason, not yours. A test about the filter builds its own list (`tests/
  test_ugc_filter.py` deletes the rows and calls `ugc_filter.invalidate()` before and
  after): the compiled list is cached per process and keyed to a version in the
  shared cache, which OUTLIVES a test's rolled-back transaction.

Three more about tests of background work (found building `fx/prod-services`, 2026-09-27):

- **No Celery retry ever runs in the suite.** Tasks run eagerly with
  `CELERY_TASK_EAGER_PROPAGATES = True`, and then Celery hands a task's `Retry` back to
  the CALLER instead of re-running it. To test a retry, set
  `settings.CELERY_TASK_EAGER_PROPAGATES = False` (the Celery app reads the Django
  setting on every lookup; assigning `app.conf.task_eager_propagates` does NOTHING).
- **`caplog` never sees the `users`, `diet`, `routine`, `subscription` or `challenges`
  loggers**: `settings_base` LOGGING sets `propagate: False` on them. Add
  `caplog.handler` to the logger itself (`tests/test_prod_services.py` `users_log`).
- **A production boot test needs every required variable**: the shared environments are
  `tests/test_media_storage.py` `_production_boot` and `tests/test_fx_media.py`
  `_prod_env`. A new `require_env` in `settings_production.py` goes into both, with the
  reason, or every "boots" assertion fails for a reason that is not theirs.

One about the merchant account (found building `build/hub-signals`, 2026-09-27):

- **Every read of ShamCash now writes `subscription.GatewayHealth`** (through
  `PaymentGatewayService.fetch_payment_status` / `list_transactions`). A test that makes
  `ShamCashGateway._get` raise leaves failures counted, and two in a row over 60 s turn
  `gateway_state` on every payment read to `degraded`. It decides nothing — never expire,
  fail or complete a payment on it — and a transfer's `sender` matching a customer's
  saved `PayerAddress` is evidence for the review queue, never a confirmation.

One about counting queries (found building `build/chat-cards-posters`, 2026-09-27):

- **django-cachalot answers a repeated read from its cache**, so one request costs N
  queries the first time, N-1 when it repeats a cached read, and N again after any write
  to a table it read. A test comparing two query counts wraps each measurement in
  `cachalot.api.cachalot_disabled()` (`tests/test_chat_cards_posters.py`,
  `tests/test_backend_bugs_app_0923d.py`) or counts only its own tables
  (`tests/test_chat.py`), or it fails for a reason that is not the code's.

One about email (found building `build/email-channel`, 2026-09-27):

- **Notices now send email too** (`notifications/channels/email.py`). A test that runs
  on-commit callbacks around a payment completion, a withdrawal step or the plan
  reminders now finds a notification email in `mail.outbox` beside any account email
  (every `make_user` account has a verified address), and an `EmailDelivery` row. Count
  the outbox by recipient or subject, not by length. A receiver added to
  `payment_completed` must never raise: the payout receiver shares that signal.

Two about the environment (found building `deploy/`, 2026-11-20):

- **An EMPTY variable is not an unset one.** `get_env("X", "default")` answers `""` for
  a line `X=` in an env file, and the default is gone (an empty `AI_ASSISTANT_MODEL`
  was an empty model name on every request). A setting whose empty value is nonsense
  reads `get_env("X", "").strip() or "default"`; `deploy/setup.sh` writes an optional
  variable only when it has a value.
- **`.dockerignore` patterns are anchored at the root.** `.env` there does not match
  `deploy/.env`; a secrets file in a new directory needs its own line (or `**/`), or
  `COPY . .` bakes it into the image (`tests/test_single_server_deploy.py`).

Three more about a public HTML page (found building `legal/`, 2026-09-27):

- **`Content-Language` is written by `LanguageResolutionMiddleware` AFTER the view**, from
  whatever language is active then. A view that renders under `translation.override(...)`
  answers the caller's Accept-Language instead of the page's: `translation.activate()` the
  page's language (`legal/views.py _speak`). The same middleware adds `Vary:
  Accept-Language, Cookie` to every response.
- **Production CSP allows an inline `<style>`/`<script>` only with the request's nonce**
  (`nonce="{{ request.csp_nonce }}"`); DEBUG (tests) allows anything, so a missing nonce
  passes every test and renders unstyled in production. A cacheable page carrying a nonce
  must not send an ETag or Last-Modified: a 304 merges the new CSP header into the stored
  page and blocks its style.
- **A new public route fails the anonymous sweep** (`test_anonymous_gets_no_5xx_and_
  reaches_only_public_endpoints`) until it is in that test's allow-list — and the test
  client sends `X-App-Channel: apk` by default, so a route that refuses a missing channel
  is still reachable there.

## Working in a worktree

Worktrees are how several agents build at once. The setup is done for you; what bites is:

- Run tests with a **unique** `DB_NAME` (`DB_NAME=tp_<yourname> .venv/bin/pytest …`), or you
  will drop another agent's database mid-run.
- **Commit before you run anything long.** A watchdog kills an agent that shows no
  progress for ten minutes, and a long test run looks exactly like no progress. A commit
  survives that; a dirty worktree is only luck. Commit unfinished work, then run.
- **Run only your own test file.** One Postgres serves every worktree: five agents each
  running the wider suites at once starved all five until the watchdog cut them — an hour
  lost, on 2026-09-21. The session that merges runs the whole gate, once, at the end.
  If you need to know you have not broken a neighbour, say so in your report and let the
  merge prove it.
- The scratchpad is **shared**. Write logs to a filename nobody else would pick.
  Its path contains `-Git-`, and in a worktree-isolated agent a command that redirects
  into it (`pytest ... > <scratchpad>/x.log`) or chains `mkdir && git ...` can be refused
  as "names git in a form too complex to verify" (2026-09-27). Run the command plainly
  and pipe to `tail`, or run it with `run_in_background`, whose output file is kept for
  you; make the directory in a command of its own. `cd <worktree> && <anything>` is
  refused the same way (the worktree's path holds `-Git-` too), and so is a
  `cat >> file <<EOF` heredoc: every command already starts in the worktree, so drop the
  `cd`, put a multi-step run in a script under the scratchpad and run that, and append
  to a file with the Edit tool (2026-11-21).
- `seeded_catalogue` loads the food catalogue but does NOT give the test database
  access: a test that takes it and no `db` fails with "Database access not allowed".
- `.venv` in a worktree is a **symlink**. `.gitignore` now covers both spellings, but check
  `git status` before `git add -A` all the same. It points at the ONE venv the main
  checkout, every worktree and the app session's :8020 server share: never `pip install`
  into it while anything else runs from it. To test a dependency change, build a venv of
  your own from the lock and run pytest with that interpreter (2026-09-28, the Django 5.2
  gate).
- The Postgres on :5433 is ONE server for every worktree AND the app session's
  `yalla_app_dev`. On 2026-09-28 several `--create-db` runs at once exhausted the
  container's 64 MB shared memory, and the server segfaulted and restarted into recovery,
  taking every database down (SIDE_BUGS S-244). One `--create-db` at a time. A run that
  fails with "the database system is in recovery mode" is this, not your code: wait for
  `pg_isready` and run again.
- **The scratchpad is pruned.** It lives under `/private/tmp`, and macOS deletes its files
  after about three idle days (2026-09-28 -> 2026-10-07: every brief, log and old-code
  tree was gone, only the directories were left). Never trust an old-code tree you did
  not extract in this run: `rm -rf` it and `git archive` the base again.
- **A dependency upgrade can ship its own migrations.** The Django 5.2 upgrade brought
  `token_blacklist 0013` with djangorestframework-simplejwt, and the app session's
  `yalla_app_dev` ran without it for a week. After any change to the lock, run
  `manage.py showmigrations | grep "\[ \]"` against every development database, not only
  against the test database the gate builds.
- **Serving the API to a phone.** The desktop app's preview launcher cannot read the
  project's `.venv` under `~/Desktop` (macOS privacy), so it cannot start Daphne; run
  `.venv/bin/daphne -b 0.0.0.0 -p 8020 training_platform.asgi:application` from a shell
  instead. Port 8000 is taken by another macOS account's unrelated `runserver`. The app
  bakes the address in at build time (`--dart-define=API_BASE_URL=http://<mac LAN ip>:8020`),
  and only a DEBUG APK may use plain http (`usesCleartextTraffic` is in the debug manifest
  only). Sign-up codes go to the server's console log (`settings_local` email backend).
- `tests/security/*.py` used to hardcode the main checkout's path, so from a worktree they
  exercised the main tree and passed on a broken branch. If you see that pattern anywhere
  else, it is a bug, not a convention.
- Do not touch a file another agent is known to be working in. Say so in your report
  instead; the merging session will handle it.

## The merge, and what conflicts

**Never leave a merge staged in the main checkout.** The app sessions commit their
BACKEND_BUGS rows directly on `spec/payments-compliance` in the main checkout, and a
`git commit` there takes EVERYTHING staged: on 2026-09-24 a resolved merge sitting in
the index (MERGE_HEAD set, tests still running) was committed under an app session's
"docs(BACKEND_BUGS): B-135" message (`f03f9c2`). The content was right; the history is
misleading and nobody reviewed the moment it landed. Resolve and commit a merge in one
go, run its tests right after, and do longer fix work in a worktree of your own.

**Never `git add -A` (or `git add .`) in the main checkout.** Stage the paths you mean.
On 2026-09-20 one `git add -A` swept a stranger's working-tree state into a commit: it
deleted `SYSTEM_OVERVIEW.md` (which `CLAUDE.md` tells every session to read) and added
150 files of the abandoned `mobile/` app, and nobody noticed until an audit three days
later — after it had been pushed.

**Never pipe `git merge` through `tail` or `head`.** The CONFLICT lines come first and
you will not see them. After every merge, run `git diff --name-only --diff-filter=U`
and resolve each file it names; then `git grep -nE '^(<<<<<<<|>>>>>>>)( |$)'` must
print nothing before you commit. A `tail`-ed merge committed `SIDE_BUGS.md` full of
conflict markers, and the counts reported from it were wrong.

**`.po` files: merge with gettext, not by hand.** Take the two sides with
`git show :2:locale/ar/LC_MESSAGES/django.po > ours.po` and `:3:` for theirs, then
`msgcat --use-first -o django.po ours.po theirs.po`. A text union of conflict hunks
splits entries at the hunk boundary and leaves a `msgstr` with no `msgid`.

Every parallel build touches the same four places, so expect these and nothing else:

- `locale/ar/LC_MESSAGES/django.po` — both sides APPEND, so the resolution is "keep both,
  in order". **`django.mo` is binary and cannot be merged: regenerate it from the merged
  `.po` with `msgfmt`.** Never hand-resolve the `.mo`.
- `training_platform/settings_base.py` `INSTALLED_APPS`, `training_platform/urls.py`,
  `training_platform/privacy/registry.py` — one line each, keep both sides.
- `API_CONTRACT.md` — usually auto-merges. When two branches each add a "What wiring the
  X screens found" block before the same line, it conflicts: keep both blocks, in order.
- `BACKEND_BUGS.md` — every branch that fills rows conflicts with every other one, and with
  the app session appending new rows. Resolve BY ROW ID: the union of rows, and for a row
  on both sides the side whose Status / Backend note is filled. **Never write a bare `|`
  inside a cell**, not even inside backticks: markdown splits the row there and the note
  spills into phantom columns (three nutrition rows did, 2026-09-23). Write `/` instead.

**Proving a test fails on the old code.** Checking the base commit's files out into your
worktree, or `git archive`-ing it, can be refused by the permission classifier (three
refusals, 2026-09-23). Say in your report that the old-code run is still owed; the main
session runs it in a separate worktree at the base commit. What did work (2026-09-24,
routine products): `git archive <base> | tar -x -C <scratchpad>/<unique dir>`, symlink
`.venv` and `.env` into it, copy your test file in, and run pytest THERE with its own
`DB_NAME` and `--create-db` — your worktree is never touched, so it can run while you edit.
Read WHY each test fails there: a `monkeypatch.setattr(module, "name", ...)` of a function
your fix ADDS raises AttributeError on the old code, and an import of a new constant
raises ImportError, so the old-code run "fails" without ever reaching the bug. Pass
`raising=False`, and import only what the old code has (fx/w2-chat, 2026-11-21).

## Rules that outrank your judgement

- **A shipped `code` never changes.** The app branches on `code`, never on message text.
  Add codes freely; renaming one is a breaking change that needs the owner.
- **Never edit a test's assertion to make it pass.** A deliberate contract change updates
  the SHARED SETUP (a constant, a fixture, an allow-list) with the reason in a comment —
  and the test must still assert the thing it was written to protect.
- **Behaviour may depend on the BUILD** (`X-App-Channel`, `X-App-Version`) **and on nothing
  else** — never on user, country, IP, date or a "review mode". A response that differs by
  build must carry the channel and version in its cache key.
- **A mock is not a test.** Patching `training_refusal` and asserting the patch fired tests
  the patch. Seed a real `CoachingPeriod` with its `relation` set, the way
  `users.coaching.open_period` writes one.
- `on_commit` callbacks do not run under pytest without
  `django_capture_on_commit_callbacks(execute=True)`.
- modeltranslation makes `.values_list('name')` return the RAW column. Read instances when
  you need the translated name.
- Money: a payment's state changes only inside `PaymentService`; every `move_funds_atomic`
  names its `money_type`; lock order is subscription → payment, user rows → wallets by
  ascending id.

- **An app test reads the push registry and the push data keys from disk.** The
  app's gate test reads `notifications/channels/fcm.py` `EVENT_CLASS_REGISTRY` and
  API_CONTRACT.md's table «`data.type` | additional `data` keys», and fails when the
  app's table (a push's `data.type` -> the page it opens, with the keys it reads)
  differs, as its store-safety test reads `policy/defaults.py`. If you add, rename or
  remove an event type, or a push data key in that table, or move or rename either,
  SAY SO IN YOUR REPORT: the integrator lists every such change in the hand-off
  (2026-11-24, the manager and the app manager; the app covers all 48 types as of
  `9b286e7`).
- **A test run over about 2 minutes goes through the one lock.** `sh
  /Users/mac/Desktop/Git/t2/Training_platform/.claude/worktrees/.locks/with_lock.sh suite
  backend-<who> -- <command>`, the command the script starts. A list of files is ONE run.
  Only a single file that finishes in under 2 minutes runs unlocked, and never two backend
  runs at once outside the lock: the Mac (8 GB) reached load 256 with swap nearly full when
  two unlocked backend runs went beside a simulator build (2026-11-24).
  ONE ticket per run and one hold of at most about 30 minutes: a longer list is cut into
  pieces that each take their own place in the queue (the manager, 2026-11-24).
- **A `-k` subset of the regression gate is not the gate file.** Before a commit is
  reported as ready (a "merge" line), the WHOLE `tests/test_regression_gate.py` runs on it,
  through the queue as a short piece: several of its tests read SOURCE text and fail on a
  refactor that keeps behaviour (2026-11-24: `test_one_renewal_at_a_time_holds_under_concurrency`
  reads `start_renewal` for `select_for_update`, and a lock moved into a helper failed it; a
  worker's `-k` subset had left it out).

## Where the product is decided

- `CLAUDE.md` — the rules, and which file to start from.
- `PAYMENTS_COMPLIANCE_PLAN.md` — the 12 build steps and their state.
- `BACKEND_DELTA.md` — what the app's designs need from the backend, item by item.
- `BACKEND_BUGS.md` — the app session's notebook. Fix rows, fill only Status and Backend note.
- `SIDE_BUGS.md` — defects found while building something else. **Write the row and keep
  going**; do not derail your task to fix someone else's bug.
- `yalla_gym/design/yalla-gym-design/` — the owner's canvas, and the ONLY spec for the app.
  `docs/BuildGuide.md` is prose; `screens/**/*.png` are the screens. **Read it, never edit
  it.** If your task's shape is drawn there, the drawing wins over anything I tell you.

## The apps, and who owns what

`users` (accounts, trainer relations, the monthly coaching and its freeze) ·
`wallet` (the ledger; `move_funds_atomic`) · `subscription` (plans, payments,
`PaymentService`, the ShamCash gateway, the checkout poller) · `policy` (the four kinds of
money, and what each BUILD may show and sell) · `routine` (exercises, routines, sessions,
set logs, day outcomes, strength and progression) · `diet` · `chat` (text messages, 1:1,
trainer↔client) · `moderation` (reports, blocks, AI consent) · `mediakit` (media slots,
object storage, video) · `notifications` · `social` · `ai_assistant` · `achievements` ·
`analytics` · `challenges` · `admin_dashboard` (the operator console at `/dj-admin/`).

Never read or touch `mobile/`, `flutter/` or `_excluded/`. Never edit `yalla_gym/` from a
backend session.
