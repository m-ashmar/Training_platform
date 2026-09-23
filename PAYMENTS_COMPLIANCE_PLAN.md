# Payments & store-compliance — build status

Spec (owner's doc): https://claude.ai/artifact/2kBAo7VxSgCngChTm2KVaP
Branch: `spec/payments-compliance` (off `noti-system`). Update this file in the same commit
as the work; a new session starts here, not from the spec.

Rule zero: no hidden flows, no reviewer detection. Server behaviour depends on the build
(`X-App-Channel`, `X-App-Version`) and on nothing else.

A second workstream shares this branch: the backend changes the v2 app designs assume
(media slots, onboarding fields, trainer matching and proof, chat, home, ...). Its state
and order live in `BACKEND_DELTA.md`, not here.

## Build order

| # | Step | Status | Where / what is left |
|---|---|---|---|
| 1 | `money_type` on payments, ledger, checkouts; `source` on entitlements | DONE | `policy/money.py`; `wallet.Transaction.money_type` (0013 backfill, 0014 check); `subscription.Payment.money_type`, `Subscription.source` (0012). Checkout sessions get theirs when the model exists (step 5) |
| 2 | Capabilities endpoint, channel header, server-side refusal | DONE, 2 follow-ups | `policy/` app, `GET /api/v1/config/capabilities`, seeded per spec §4, change log, operator console. Left: (a) AI coach must decline "how do I pay" on `play`/`ios` — needs the channel on the WebSocket handshake; (b) `preferred_language`-independent: notification texts for `PLATFORM_SUB` states on store channels (arrives with step 5) |
| 3 | Merge the unmerged payments fix | DONE | `c9-fix-payments` merged here; full suite on a fresh DB is the gate |
| 4 | Double-entry ledger, commission settings + snapshot, remove the 100% forward | DONE | Coaching commission DONE with the monthly coaching (`users/coaching.py`, `BACKEND_DELTA.md` item 16): 20% of every month, rate copied onto each `CoachingPeriod`, to a platform revenue wallet. The 100% forward is GONE (2026-09-20): a plan payment is minted into the client's wallet and moved straight out to the platform's revenue wallet, whether or not anyone coaches them — `SUBSCRIPTION_TRAINER_SHARE` is kept at 0 so the owner can price a trainer's share later rather than the code having to grow one back. A client with no trainer used to get NO ledger rows at all, so the platform's largest revenue line was invisible in its own ledger; it is recorded now. Left for a later pass, neither blocking: the rates are settings rather than operator-editable rows, and escrow/revenue are still wallets rather than named ledger accounts |
| 5 | Payment lifecycle: states, 60s poller, review queue, idempotency, notifications, PDF receipts, saved-language fix | PART DONE | **Receipts + the money screens' fields: DONE on branch `build/money`, unmerged** (see below). Done earlier on this branch: the 60s poller (`subscription.poll_open_payments`), checkout TTL, idempotency, `auto_renew` read-only. STILL TODO: the six-state checkout-session model (`OPEN/MATCHING/NEEDS_REVIEW/CONFIRMED/REJECTED/EXPIRED`) with its event rows and the operator review queue, the notification matrix of spec §7 (including the receipt emailed on confirmation), and the saved-language fix. `preferred_language` is settable since BACKEND_BUGS B-005 (`POST /api/auth/user/update/`). Plan expiry notices EXIST (owner, 2026-09-19; `BACKEND_DELTA.md` item 18): `subscription_expiring` 3 days ahead and `subscription_expired` on days 0/3/7/14/30 from `subscription.send_renewal_reminders`, store-safe text, `access`/`renewal_due` on `subscriptions/current/`; extend these, never add parallel expiry notices |
| 6 | Trainer verification, then withdrawals | TODO | |
| 7 | Reports, blocking, moderation queue, AI consent, date of birth + minimum age 14 | DONE (branch `build/moderation`, unmerged) | Date of birth + minimum age 14 was already done with the design-v2 work (`BACKEND_DELTA.md` item 3). B1/B2/B5 are the new `moderation/` app: `Report`, `Block`, `AIConsent`; `POST/GET /api/v1/reports/`, `reports/mine/`, `GET/POST/DELETE /api/v1/blocks/`, `GET/POST /api/v1/ai-consent/`; the queue at `/dj-admin/moderation/report/` (operator-only, two closing actions). Enforcement: `moderation.services.blocked_ids` filters posts, comments, follows, profiles, notifications, the challenge leaderboard, available-trainers and trainer matches; `is_blocked` refuses sending AND accepting a trainer request; `require_ai_consent`/`HasAIConsent` gate the AI coach on REST and in `chat_service.chat_stream`. Tests: `tests/test_moderation.py`. Left for step 8: the report/block hooks on the two-way chat |
| 8 | Two-way trainer–client chat with report/block hooks | TODO | Makes the 1:1 coaching claim real-time in fact |
| 9 | Agent limits, receivable tracking, agent history/status | TODO | Defaults (200/day) are below the cheapest plan |
| 10 | Routine products | TODO | Refusal for `ROUTINE_PRODUCT` on store channels already exists in `policy/services.py` — call `require_purchase_allowed` first in the purchase endpoint. Make the buyer's copy with `routine/assignment.py` `routine_from_template` (the same private copy a trainer's generate writes); keep payment out of that function |
| 11 | Web hub, deep links, APK hosting, `GET /api/v1/app/latest` | TODO | Next.js, separate deploy |
| 12 | Demo seeding command, review notes, privacy + data inventory docs | TODO | `training_platform/privacy/registry.py` already lists every personal-data model — generate the inventory from it |

## Step 5, receipts slice — done on `build/money` (2026-09-20, unmerged)

The money screens are `yalla_gym/design/yalla-gym-design/screens/07-money/`. They are the
specification; `tests/test_receipts.py` names the screen each assertion comes from.

| What | Where |
|---|---|
| `GET subscription/v1/payments/{id}/receipt/`, and `?format=pdf` | `subscription/views.py` `PaymentReceiptView`, `subscription/receipts.py` |
| Invoice numbers for rows completed before the column existed | `PaymentService.ensure_invoice_number` — the ONE generator, still in the service |
| `Payment.completed_at` (the date on the receipt; `updated_at` moves on a refund) | migration `subscription/0013` |
| Payment list rows: `item`, `completed_at`, `reference`, `receipt_available` | `PaymentSerializer` |
| `subscriptions/current/`: `plan_duration_days`, `plan_currency`, `plan_features` | `UserSubscriptionSummarySerializer` |
| `GET subscription/v1/usage/today/` (diet builds + AI messages, used/limit) | `SubscriptionUsageViewSet.today`, `subscription.quota.snapshot` |
| Plans: `currency`, `is_featured` (the "الأكثر اختياراً" badge) | migration `subscription/0013`, both plan serializers |
| Wallet: `held`, `held_for`, `account_number` ("YG-204-781"), and that number accepted as `client_identifier` | `WalletSerializer`, `wallet/utils.py`, `wallet/views.py` `_client_named` |
| Activity rows: `kind`, `reason`, `direction`, `signed_amount`, `counterparty` | `wallet/activity.py` — the one place a ledger row is read as an activity |
| `GET wallet/transactions/{reference_id}/`, `GET wallet/agents/` | `wallet/views.py`; agent listing fields in migration `wallet/0015` |

**The PDF, and why three dependencies.** `fpdf2` + `arabic-reshaper` + `python-bidi`,
with Noto Naskh Arabic in `subscription/fonts/` (OFL). A PDF draws glyphs in the order it
is given, so Arabic needs letter joining AND the bidirectional algorithm applied before
the text reaches the page — and then a font that HAS the presentation forms joining
produces. Measured on this receipt's own strings: the app's brand font (Alexandria) is
missing 14 of them and Cairo the same 14, so either would have rendered boxes. `fpdf2` is
pure Python with no system libraries; WeasyPrint needs cairo and pango in the image.
`tests/test_receipts.py` reads the text back out of the PDF's own `/ToUnicode` map and
checks the font covers every character drawn, in both languages — "it returned a PDF"
does not prove the Arabic is readable.

**The store line, on everything in this slice.** A receipt of a payment already made is a
RECORD, not a sale, so none of it calls `require_purchase_allowed`, none of it takes
`X-App-Channel`, and two tests assert the response is byte-identical on `apk`, `play`,
`ios` and `web` (spec §3: a store build may show plan status, expiry and history). Prices
are the only thing still withheld: `plan_price` and `price`.

## How the channel policy works (read before touching payments)

- `policy.services.require_purchase_allowed(request, money_type)` is the FIRST line of any
  endpoint that opens a checkout or moves wallet money. `PLATFORM_SUB`/`ROUTINE_PRODUCT` on
  `play`/`ios` is refused in code before any row is read; everything else follows the row.
- No known `X-App-Channel` → strictest behaviour (`400 app_channel_required` on money
  endpoints, no prices). Tests default to `apk` (`tests/conftest.py`).
- A channel with no row falls back to `policy/defaults.py` (the spec's table), so losing the
  rows never stops the APK selling. Thread-based tests flush every table — never rely on
  seeded rows in a test; `tests/test_policy_channels.py::base_rows` puts them back.
- Store-locked flags cannot be saved true (`ChannelCapabilities.clean`) and are masked on
  read (`flags_for`). Enabling iOS coaching later = a new row with a higher `min_version`.
- The response cache keys on the build (`training_platform/middleware.py:_build_key`);
  any new per-channel response needs nothing more.
- Every production call to `move_funds_atomic` names `money_type`; `tests/test_policy_money.py`
  fails on one that does not.

## Acceptance checklist (spec §11)

| Item | Proven by |
|---|---|
| `PLATFORM_SUB` checkout on `ios`/`play` refused | `tests/test_policy_channels.py::test_a_plan_checkout_from_a_store_build_is_refused`, `::test_a_row_that_says_yes_cannot_switch_purchasing_on_in_a_store_build` |
| Flags identical for two users on one build | `::test_two_users_and_a_visitor_on_one_build_get_identical_flags` |
| No price strings reach store builds from the server | `::test_plan_prices_are_not_sent_to_a_build_that_may_not_show_them` (server side only; the binary scan is a mobile release check) |
| Same ShamCash transaction cannot confirm two sessions | existing `uniq_payment_transaction_id` + c9 tests; re-prove on the step 5 session model |
| Remaining eight items | not started — steps 4–12 |

## Owner's decisions (2026-09-19)

| # | Decision | Effect |
|---|---|---|
| 1 | The platform keeps **20% of every coaching fee** a client pays a trainer; the trainer gets 80% | Step 4: `COACHING_COMMISSION_RATE = 0.20`, also applied to routine-product sales (spec §8) |
| 2 | A plan payment stays **100% with the platform**, whether or not the client has a trainer | Step 4: `SUBSCRIPTION_TRAINER_SHARE = 0` — the 100% forward in `wallet/signals.py:handle_payment_completed` is removed, not reduced. Keep the setting so the owner can raise it later |
| 3 | Minimum age **14** | Step 7: date of birth at signup, enforced by the server |
| 4 | `yalla_gym/` is the mobile app, built by its own sessions | Backend sessions do not edit it. It already sends `X-App-Channel` / `X-App-Version` |
| 5 | Coaching is sold **by the month**, and the 20% comes off every renewal too | Built: `users/coaching.py` (`BACKEND_DELTA.md` item 16) |

Both rates are operator-editable settings, snapshotted onto the transaction when the price
is locked (spec §8); the numbers above are the defaults.

## Open

1. `yalla_gym/lib/core/config/env.dart` defaults `APP_CHANNEL` to `apk` and validates with
   `assert`, which release builds strip. An iOS or Play build made without the define would
   declare itself the direct APK and be offered purchasing. Owner/mobile session: no
   default (empty → the server's strictest behaviour) and a release-mode check.
2. Step 2 follow-up: the AI coach must decline "how do I pay" on `play`/`ios`.
3. **The money screens left five decisions** (the receipts slice, 2026-09-20). Each one
   is a field a screen draws that the backend cannot answer honestly without an answer.
   (a) `Plans.png` tells the client "مدربك يحصل على حصته من اشتراكك" — your trainer gets
   a share of your subscription — and the owner's decision 2 sets
   `SUBSCRIPTION_TRAINER_SHARE = 0`, so no trainer is paid anything for a plan. Either
   the line goes or the share is priced; shipping both is a promise the ledger does not
   keep. (b) The AI coach's daily allowance is ONE GLOBAL SETTING
   (`AI_ASSISTANT_CONFIG['MAX_MESSAGES_PER_DAY'] = 50`) applied to every plan that has
   `has_ai_advice`, while `Plans.png` prices three different allowances (Basic none,
   Premium 50 a day, Professional "بلا حدود"). `usage/today/` answers 50 to everyone
   until there is a per-plan field and the owner's numbers for it. (c) `Pay.png` counts
   down 72 hours ("الرمز صالح لمدة 71:42:10") and `PAYMENT_CHECKOUT_TTL_MINUTES` is 60
   MINUTES; `expires_at` now answers the server's real TTL, so the screen and the server
   agree — but one of the two numbers is wrong and only the owner can say which. (d) The
   receipt prints the platform's legal name (spec §7) from `PLATFORM_LEGAL_NAME`, which
   is empty: the registered company name is owed, and spec §10 says take legal advice on
   the entity first. (e) The account number a client reads out to an agent
   ("YG-204-781") is DERIVED from the user id, so it is stable with no migration but is
   enumerable. If the owner wants a code that cannot be guessed at, it becomes a stored
   column and a data migration.
4. Step 7 left three decisions for the owner. (a) The AI policy version is a bare string
   the app sends; nothing on the server declares the CURRENT version, so nothing can ask
   again after a policy change — a setting plus `policy_version_required` on
   `capabilities` would. (b) A report is anonymised, not deleted, when the reporter
   erases their account (the row and its outcome survive, their words go) — legal may
   want a retention window instead. (c) Blocking does not hide a challenge whose creator
   was blocked; only the leaderboard drops them, because hiding a shared activity would
   take the blocker's own progress with it.
5. **Changing plan mid-period** (BACKEND_BUGS B-047, 2026-09-23). `POST
   v1/subscriptions/{id}/change-plan/` moves a subscription only while no cover runs and
   no checkout is open; with cover running it answers `409 plan_change_while_active`.
   Owed: whether an upgrade mid-period is allowed, and how it is priced (pay the
   difference for the days left, or start the new plan when the old one ends). Also
   decided in code, reversible: a transfer that clears late against an expired checkout
   for the OLD plan grants the plan it paid for; if cover on another plan was paid in the
   meantime, the subscription holds the dearest plan among the paid periods still running
   (prices of record; a tie or two currencies keeps the current one) and the late period
   is added; a refund re-decides it (`PaymentService._dearest_running_plan`). A stacked
   period on a cheaper plan therefore runs on the dearer plan's features until it ends —
   per-period plans would need a schedule the model does not have.

## Traps this branch cost a session to find

- **Testing async code that touches the ORM.** Inside a `django_db` test, an ORM read
  made under `asyncio.run` gets a NEW connection and sees an EMPTY table — the row the
  test just wrote is invisible and the assertion passes for the wrong reason. Measured
  on `AIConsent`. `async_to_sync` is worse: `channels.db.database_sync_to_async` calls
  `close_old_connections` on the test's own connection and everything after it fails
  with "connection already closed". The way that works is to replace
  `database_sync_to_async` with an inline shim and step the coroutine by hand — see
  `tests/test_moderation.py::_chat`. `tests/test_c9_minor.py` reaches the same place by
  unwrapping the decorator with `.func`.
- **`reverse("admin:...")` from console code goes to `/admin/`, not `/dj-admin/`.** Both
  sites publish the app namespace `admin`; only `current_app=self.admin_site.name`
  picks the console (`moderation/targets.py` `console_link`).
- **`privacy/registry.py` `audit_coverage()` has its own `local` app set.** An app that
  is not in it is silently exempt and the coverage gate passes with the models
  unregistered. A new app goes in that set in the same commit as its first model.

## One step per session — what to paste

Open a NEW session for each step and paste the line. Effort: **max** only where money,
locking or idempotency is designed; **default** for the rest.

| Step | Effort | Paste this |
|---|---|---|
| 4 | max | `Read PAYMENTS_COMPLIANCE_PLAN.md and do step 4: double-entry ledger accounts, the two commission settings with snapshotting, and remove the 100% subscription forward. Done = new tests + full gate with --create-db, plan file updated, committed on the branch.` |
| 5 | max | `Read PAYMENTS_COMPLIANCE_PLAN.md and do step 5 (payment lifecycle). Split it: first the checkout session model + states + idempotency, stop and report; the poller, notifications and receipts are separate sessions.` |
| 6 | max | `... do step 6: trainer verification flow, then withdrawals.` |
| 7 | default | `... do step 7: reports, blocking, moderation queue, AI consent, date of birth with minimum age 14.` |
| 8 | default | `... do step 8: two-way trainer–client chat with report and block hooks.` |
| 9 | high | `... do step 9: agent limits, receivable tracking, agent history and status.` |
| 10 | high | `... do step 10: routine products.` |
| 11 | default | `... do step 11: web hub API surface, deep-link files, APK version endpoint.` |
| 12 | default | `... do step 12: demo seeding command, review notes, data inventory from the privacy registry.` |
