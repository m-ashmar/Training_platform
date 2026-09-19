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
| 4 | Double-entry ledger, commission settings + snapshot, remove the 100% forward | NEXT | `wallet/signals.py:handle_payment_completed` still forwards 100% of a plan payment to the trainer. Rates decided — see Owner's decisions |
| 5 | Payment lifecycle: states, 60s poller, review queue, idempotency, notifications, PDF receipts, saved-language fix | TODO | `preferred_language` cannot be set by any endpoint yet (HIGH). Remove `auto_renew` from API/UI here |
| 6 | Trainer verification, then withdrawals | TODO | |
| 7 | Reports, blocking, moderation queue, AI consent, date of birth + minimum age 14 | PART DONE | Date of birth + minimum age 14 DONE with the design-v2 work (`BACKEND_DELTA.md` item 3: validated at sign-up and on every profile write, `MINIMUM_AGE_YEARS`; making it required at sign-up is an owner decision in that file). Left: reports, blocking, moderation queue, AI consent — store blockers B1, B2, B5 |
| 8 | Two-way trainer–client chat with report/block hooks | TODO | Makes the 1:1 coaching claim real-time in fact |
| 9 | Agent limits, receivable tracking, agent history/status | TODO | Defaults (200/day) are below the cheapest plan |
| 10 | Routine products | TODO | Refusal for `ROUTINE_PRODUCT` on store channels already exists in `policy/services.py` — call `require_purchase_allowed` first in the purchase endpoint |
| 11 | Web hub, deep links, APK hosting, `GET /api/v1/app/latest` | TODO | Next.js, separate deploy |
| 12 | Demo seeding command, review notes, privacy + data inventory docs | TODO | `training_platform/privacy/registry.py` already lists every personal-data model — generate the inventory from it |

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

Both rates are operator-editable settings, snapshotted onto the transaction when the price
is locked (spec §8); the numbers above are the defaults.

## Open

1. `yalla_gym/lib/core/config/env.dart` defaults `APP_CHANNEL` to `apk` and validates with
   `assert`, which release builds strip. An iOS or Play build made without the define would
   declare itself the direct APK and be offered purchasing. Owner/mobile session: no
   default (empty → the server's strictest behaviour) and a release-mode check.
2. Step 2 follow-up: the AI coach must decline "how do I pay" on `play`/`ios`.

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
