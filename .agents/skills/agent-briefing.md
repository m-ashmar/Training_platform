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

## Six gate tests that will fail work that is otherwise correct

| It checks | What you must do |
|---|---|
| `API_CONTRACT.md` diffed against the URLconf | Every new route goes in that file **in the same commit**. A route not listed fails the suite |
| The privacy registry | A model holding personal data must be registered under `training_platform/privacy/` |
| Every registered notification event is emitted | A key in `notifications/channels/fcm.py` `EVENT_CLASS_REGISTRY` needs an event class, **a listener**, and a LITERAL `event_type="..."` in non-test source. A test asserting `emit_event` was called does NOT prove delivery — assert the `Notification` row |
| Beat entries resolve | Every `CELERY_BEAT_SCHEDULE` entry must name a real registered task |
| The Arabic catalogue | **Every string you pass to `_()` — and every `{% trans %}` — must be IN `django.po` with Arabic, and in the compiled `django.mo`.** Not "every entry already in the file": the gate used to check only those, so a string that never reached the catalogue was invisible and shipped in English (SIDE_BUGS S-019 — 152 of them). None fuzzy. Add the entry, then `msgfmt --check-format -o locale/ar/LC_MESSAGES/django.mo locale/ar/LC_MESSAGES/django.po` and commit the `.mo` |
| One predicate decides what a trainer sees | A source scan forbids reading `assigned_trainer` directly — use the approved relation |

## Working in a worktree

Worktrees are how several agents build at once. The setup is done for you; what bites is:

- Run tests with a **unique** `DB_NAME` (`DB_NAME=tp_<yourname> .venv/bin/pytest …`), or you
  will drop another agent's database mid-run.
- The scratchpad is **shared**. Write logs to a filename nobody else would pick.
- `.venv` in a worktree is a **symlink**. `.gitignore` now covers both spellings, but check
  `git status` before `git add -A` all the same.
- `tests/security/*.py` used to hardcode the main checkout's path, so from a worktree they
  exercised the main tree and passed on a broken branch. If you see that pattern anywhere
  else, it is a bug, not a convention.
- Do not touch a file another agent is known to be working in. Say so in your report
  instead; the merging session will handle it.

## The merge, and what conflicts

Every parallel build touches the same four places, so expect these and nothing else:

- `locale/ar/LC_MESSAGES/django.po` — both sides APPEND, so the resolution is "keep both,
  in order". **`django.mo` is binary and cannot be merged: regenerate it from the merged
  `.po` with `msgfmt`.** Never hand-resolve the `.mo`.
- `training_platform/settings_base.py` `INSTALLED_APPS`, `training_platform/urls.py`,
  `training_platform/privacy/registry.py` — one line each, keep both sides.
- `API_CONTRACT.md` — usually auto-merges.

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
