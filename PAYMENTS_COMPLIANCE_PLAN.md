# Payments & store-compliance — build status

Spec (owner's doc): https://claude.ai/artifact/2kBAo7VxSgCngChTm2KVaP
Branch: `spec/payments-compliance` (off `noti-system`). Update this file in the same commit
as the work; a new session starts here, not from the spec.

Rule zero: no hidden flows, no reviewer detection. Server behaviour depends on the build
(`X-App-Channel`, `X-App-Version`) and on nothing else.

A second workstream shares this branch: the backend changes the v2 app designs assume
(media slots, onboarding fields, trainer matching and proof, chat, home, ...). Its state
and order live in `BACKEND_DELTA.md`, not here.

**Where things stand (2026-11-24):** Road to 10 is the work now: phase 0, batch A, phase 3,
S2.7, batch B, S2.13, the review round of every landed batch, Part 4's R4, R5, R8 and R1,
round 3 (B-199, the console and password fixes), round 4's fixes, the dev-only walk
seed, round 5 (the inbox's language, B-200) and the fix run's first resumed landing
(`fx/w2-diet`, `fx/w2-small`, `fx/w2-ci`: the fix-run section) are on this branch (full gate at `bd8016a` on a fresh database: 3425 passed, 0 failed, 0 skipped); next
are the later Road to 10 phases as the owner
orders (the Road to 10 section below has the table per batch). The fix run
resumed on 2026-11-24 (the owner's order): `fx/w2-chat` and `fx/w2-backups` are still on their
branches (its section).

## Build order

| # | Step | Status | Where / what is left |
|---|---|---|---|
| 1 | `money_type` on payments, ledger, checkouts; `source` on entitlements | DONE | `policy/money.py`; `wallet.Transaction.money_type` (0013 backfill, 0014 check); `subscription.Payment.money_type`, `Subscription.source` (0012). Checkout sessions get theirs when the model exists (step 5) |
| 2 | Capabilities endpoint, channel header, server-side refusal | DONE, 2 follow-ups | `policy/` app, `GET /api/v1/config/capabilities`, seeded per spec §4, change log, operator console. Left: (a) AI coach must decline "how do I pay" on `play`/`ios` — needs the channel on the WebSocket handshake; (b) `preferred_language`-independent: notification texts for `PLATFORM_SUB` states on store channels (arrives with step 5) |
| 3 | Merge the unmerged payments fix | DONE | `c9-fix-payments` merged here; full suite on a fresh DB is the gate |
| 4 | Double-entry ledger, commission settings + snapshot, remove the 100% forward | DONE | Coaching commission DONE with the monthly coaching (`users/coaching.py`, `BACKEND_DELTA.md` item 16): 20% of every month, rate copied onto each `CoachingPeriod`, to a platform revenue wallet. The 100% forward is GONE (2026-09-20): a plan payment is minted into the client's wallet and moved straight out to the platform's revenue wallet, whether or not anyone coaches them — `SUBSCRIPTION_TRAINER_SHARE` is kept at 0 so the owner can price a trainer's share later rather than the code having to grow one back. A client with no trainer used to get NO ledger rows at all, so the platform's largest revenue line was invisible in its own ledger; it is recorded now. Left for a later pass, neither blocking: the rates are settings rather than operator-editable rows, and escrow/revenue are still wallets rather than named ledger accounts |
| 5 | Payment lifecycle: states, 60s poller, review queue, idempotency, notifications, PDF receipts, saved-language fix | PART DONE | **Receipts + the money screens' fields: DONE on branch `build/money`, unmerged** (see below). Done earlier on this branch: the 60s poller (`subscription.poll_open_payments`), checkout TTL, idempotency, `auto_renew` read-only. DONE 2026-09-23 (BACKEND_BUGS B-051): the review queue — payment states `needs_review` and `rejected`, `subscription.TransferReview`, holds from the pollers, reconcile and the webhook, a ten-minute sweep for transfers no checkout claimed (`subscription.sweep_unmatched_transfers`), operator approve / reject / attach / dismiss in the console, and the `payment_needs_review` / `payment_rejected` pushes. STILL TODO: the checkout-session model as its own table (today the `Payment` row is the session) with an immutable event row for EVERY transition (today only the review decisions record who and when), the notification matrix of spec §7 (including the receipt emailed on confirmation), and the saved-language fix. `preferred_language` is settable since BACKEND_BUGS B-005 (`POST /api/auth/user/update/`). Plan expiry notices EXIST (owner, 2026-09-19; `BACKEND_DELTA.md` item 18): `subscription_expiring` 3 days ahead and `subscription_expired` on days 0/3/7/14/30 from `subscription.send_renewal_reminders`, store-safe text, `access`/`renewal_due` on `subscriptions/current/`; extend these, never add parallel expiry notices |
| 6 | Trainer verification, then withdrawals | DONE 2026-09-24 | Verification (BACKEND_BUGS B-090): the `trainer_verification` app — five document slots, rounds with immutable decisions, private storage with five-minute signed URLs, the console queue as the ONLY writer of `trainer_is_verified`, the two pushes, a 90-day purge of replaced documents. Withdrawals (B-097, B-098, B-125, B-126): the `payouts` app per spec §8 — payout methods (ShamCash address encrypted at rest, or cash at a named agent), only VERIFIED and active trainers withdraw, the money moves to a per-currency payout-clearing wallet AT THE REQUEST, REQUESTED → APPROVED → SENT → CONFIRMED / FAILED / CANCELLED with immutable events (a Postgres trigger refuses edits), operator-executed in the console, a 3-day settling hold on released coaching and product money (`PAYOUT_HOLD_DAYS`), support cannot reverse a withdrawal leg (`withdrawal_leg`). Owner to confirm: minimum 10,000 SYP / 100 USD, daily cap 500,000 SYP / 5,000 USD, the first-withdrawal test transfer (not built), email for withdrawals (no email channel), iOS trainers cannot withdraw while `can_transfer_wallet` is off |
| 7 | Reports, blocking, moderation queue, AI consent, date of birth + minimum age 14 | DONE (branch `build/moderation`, unmerged) | Date of birth + minimum age 14 was already done with the design-v2 work (`BACKEND_DELTA.md` item 3). B1/B2/B5 are the new `moderation/` app: `Report`, `Block`, `AIConsent`; `POST/GET /api/v1/reports/`, `reports/mine/`, `GET/POST/DELETE /api/v1/blocks/`, `GET/POST /api/v1/ai-consent/`; the queue at `/dj-admin/moderation/report/` (operator-only, two closing actions). Enforcement: `moderation.services.blocked_ids` filters posts, comments, follows, profiles, notifications, the challenge leaderboard, available-trainers and trainer matches; `is_blocked` refuses sending AND accepting a trainer request; `require_ai_consent`/`HasAIConsent` gate the AI coach on REST and in `chat_service.chat_stream`. Tests: `tests/test_moderation.py`. Left for step 8: the report/block hooks on the two-way chat |
| 8 | Two-way trainer–client chat with report/block hooks | PART DONE | Text chat (`chat/`), blocks enforced on every send, reports on a message (`moderation` target `coach_message`); photo, voice and video messages DONE 2026-09-24 (BACKEND_BUGS B-091: `POST /api/v1/chat/media/`, private signed URLs; a client's video naming an exercise is a form check in the trainer's queue `GET /api/v1/trainer/attention/`). Program cards and video poster frames DONE 2026-09-27 (ffmpeg is in the image, for the worker). Left: a voice note's transcript |
| 9 | Agent limits, receivable tracking, agent history/status | DONE 2026-09-24 | BACKEND_BUGS B-113..B-118, B-124 (`wallet/agents.py`): per-agent, per-currency caps per top-up / day / month (defaults raised: 50,000 / 250,000 / 5,000,000 SYP; migration raised agents still on 200/5,000), what each agent holds for the platform as an append-only `AgentReceivableEntry` record with operator settlements (`AgentSettlement`, database-immutable) and a ceiling that blocks top-ups (`receivable_ceiling_exceeded`), cap refusals carrying `limit`/`used`/`remaining`/`currency`, `GET /api/wallet/agent/me/`, the agent's ledger / receipt / settlement reads, the `wallet_topped_up` push to the client, key revoke + pairing metadata, no top-up of a suspended account. Owner to confirm: the default caps and ceiling, USD caps (0 today), an operator-typed next settlement date, keys still optional (only a revoked one stops top-ups) |
| 10 | Routine products | DONE, merged 2026-09-24 (BACKEND_BUGS B-099, B-100) | Models `routine.RoutineProduct` / `RoutinePurchase` (migration `routine/0030_routine_products`, renumbered at the merge). Service `routine/products.py` (submit by a verified trainer, operator approve / reject with reason, wallet sale buyer -> trainer 80% + platform 20% `COACHING_COMMISSION_RATE` snapshotted, all rows `ROUTINE_PRODUCT`, `require_purchase_allowed` first, buyer's copy via `routine_from_template`, operator refund reverses every leg and REVOKES the copy). Routes `/api/v1/trainer/products/` and `/api/v1/products/` (`routine/product_views.py`), console `routine/product_admin.py`, six `routine_product_*` pushes, `wallet/activity.py` kinds `routine_product_sale` / `routine_product_purchase`. A product sells only while published, its template unchanged since review, and its trainer verified. Seam for the hub: `RoutinePurchase.paid_with` + nullable unique `checkout_reference` (nothing writes `shamcash` yet — step 11). Tests `tests/test_routine_products.py`. Owner to confirm: revoke (not freeze) on refund, the 20% on product sales, preview 0-7 days, a buyer never named to the trainer |
| 11 | Web hub, deep links, APK hosting, `GET /api/v1/app/latest` | BACKEND DONE 2026-09-28 | Readiness run phases 3 and 8: APK releases + `GET /api/v1/app/latest`, checkout sessions for plans and routine products (`build/hub-checkout`), `/.well-known/` app-link files, payer address as review evidence, gateway-down state (`build/hub-signals`). Left: the SITE itself, an owner decision (the app's Flutter `web` build, or a separate Next.js site; the backend serves either) |
| 12 | Demo seeding command, review notes, privacy + data inventory docs | DONE 2026-09-27 | Readiness run phases 2 and 4: `bootstrap_platform`, `seed_review_accounts`, `manage.py data_inventory` (from the privacy registry) -> `docs/store/data_inventory.md`, `docs/store/privacy_labels.md`, `docs/store/review_notes.md`, DRAFT legal text `docs/legal/`. Open: SIDE_BUGS S-235, S-236 (the seed and bootstrap commands) |

## Production readiness run (started 2026-09-27) — resume here

The owner asked for everything the backend session can finish alone, in this order; the
owner chooses the providers (Postgres, Redis, email, Sentry, Firebase, ShamCash
credentials, domains) and does what only they can (store accounts, signing keys,
pushes, the Flutter repo's remote). Each phase: parallel worktree builders -> merge only
what passes -> full gate. The owner's rule (2026-09-27): bugs found by reviews or by the
final testing are LOGGED in SIDE_BUGS.md with evidence and fixed in a SEPARATE run — no
fix-and-rescan loops inside this run. The run ends with the full gate plus an end-to-end
smoke of the main app journeys on a fresh database (bootstrap + review accounts). The
Django 5.2 upgrade lands only with conservative, checked pins (django-allauth stays on
0.63.x unless it fails on 5.2; cachalot 2.9.x; DRF 3.16.x; Pillow 12 only if the
measured-memory tests pass) and a green full gate. The audit that set the
list: a read-only production-readiness audit of 2026-09-25 (backend code 8/10,
production readiness 5/10).

| Phase | Scope | Status |
|---|---|---|
| 1 | Hardening: Django 5.2 LTS + pinned deps; stdout logs; worker/beat split + heartbeat; Fly health check, capacity, no volume; client IP behind Fly; DB-enforced append-only ledger; bounded erasure/export; image-decode cap; batched daily jobs; Redis per role; email for any provider, sent async, in the recipient's language; CI on this branch | MERGED 2026-09-28: `fx/prod-platform` (`582fdb5`) and `fx/django-lts` (`66e809f`) after the owner's chown of `.github`; the main `.venv` is installed from `requirements.lock`. Earlier: `fx/prod-services` and `fx/prod-data` merged (`55cd2ba`, `dbc480a`). `fx/prod-platform` and `fx/django-lts` WAIT: both change `.github/workflows/security.yml`, and `.github/` is owned by another macOS user (`xx`), so git cannot write it — the owner runs `sudo chown -R mac:staff .github`, then merge both, then `.venv/bin/pip install --require-hashes -r requirements.lock` in the main venv (only when no other test run is active), then the full gate |
| 2 | Launch data: `bootstrap_platform`, `seed_review_accounts` (spec §10), starter exercise catalogue | MERGED (`cf17d77`) |
| 3 | Store review (spec §9): AI refuses payment instructions on play/ios; banned-terms filter; medical disclaimers; incomplete-allergen dishes out of allergic users' plans; APK hosting + `GET /api/v1/app/latest`; privacy + terms pages (draft text for the owner) with URLs via capabilities; web account-deletion request page | MERGED (`a24a148`, `f32da81`, `0af3831`, `b4a9397`) |
| 4 | Review documents: review notes, data inventory from the privacy registry, privacy-label / Data safety source, DRAFT privacy policy + terms (ar/en) | MERGED (`849851d`): `manage.py data_inventory` -> docs/store/data_inventory.md, docs/store/privacy_labels.md, docs/store/review_notes.md, DRAFT docs/legal/*.md |
| 5 | Step 5 remainder: an immutable event row per payment transition; email channel + receipt email; push on payment confirmed; trainer-fee pushes both sides | MERGED (`b434394`, `84caa8c`): PaymentEvent (immutable, trigger), no separate checkout-session table (the mapping is in payment_service.py), payment_confirmed + coaching-fee pushes, the §7 emails with delivery rows |
| 6 | Deploy runbook rewritten from the final config: every secret, backup/restore + key custody, staging config | DONE 2026-09-28: `DEPLOY_CHECKLIST.md` (every variable with required/default/secret, key custody and rotation, backup/restore + quarterly drill, staging, the 26 scheduled jobs in UTC, JIT off per B-136) |
| 7 | Features left: program cards in chat + video poster frames (`fc9b15c`); home day timeline, ready programs from products, voice trainer message, PRs on completion (`84c6d87`). NOT in this run: HLS/SVG (optional), BACKEND_DELTA item 20 gaps and all SIDE_BUGS (S-212..S-227 logged this run) — the owner's separate fix run | MERGED |
| 8 | Step 11 web hub — the BACKEND: checkout sessions for plans and routine products through ShamCash (`build/hub-checkout`), deep-link association files, the sender's saved ShamCash address as review evidence, the gateway-down state (`build/hub-signals`) | MERGED (`2c607f4`, `7332d97`). The SITE itself is an owner decision: the design ships the money screens in the Flutter app's `web` channel (so the hub can be the app's web build), while the spec names a separate Next.js site; the backend serves either |


**Run results (2026-09-28).** Final gate on `66e809f` (all merges, Django 5.2.17 from the
lock): 2,508 passed, 3 failed, all three failing on the old head as well:
`test_store_docs` x2 (order-dependent, SIDE_BUGS S-231) and the diet week timing test
(S-245, red since 2026-09-24). End-to-end smoke (`tools/smoke_journeys.py`) on a fresh
database under Django 5.2 (`migrate`, `bootstrap_platform` 250 rows,
`seed_review_accounts`, Daphne): 34 of 34 journeys answered as expected (anonymous,
client, trainer, agent; the plan checkout sold on apk, refused on ios). The log-only review (money, security/privacy, data) confirmed 11
findings (3 HIGH), logged with evidence as SIDE_BUGS S-232..S-243, nothing fixed; S-244
and S-245 came from the gate itself.

**The separate fix run (the owner schedules it) takes, in this order:**
1. The HIGH rows and the app's P1s, which break a user journey: S-234 (poller halted by a
   checkout purchase key), S-235 (review agent account), S-236 (bootstrap duplicates a
   renamed plan), S-212, S-213; B-172 (sign-up refused `content_not_allowed` on the
   username allauth derives from the e-mail), B-167 (a routine-product buyer gets 404 on
   `payments/{id}/` and the receipt: the views filter `subscription__user`, the payment
   has `buyer`; same root as S-240), B-164, B-175 (home's program card names no routine),
   B-182 (home sends two heroes to a programme buyer), and every P1 the app sessions file
   after these.
2. The MEDIUM and LOW rows S-214..S-245.
3. BACKEND_BUGS P2/P3 rows from B-136 on, and whatever the app sessions file after (B-136 the
   JIT slowdown, mitigated in production by the runbook; B-137 trainer macro split).
4. BACKEND_DELTA item 20 gaps.

## Road to 10, the server part (owner's order 2026-11-22) — resume here FIRST

The app session's plan `yalla_gym/app/ROAD_TO_10.md` (app repo, from commit `62c5b58`; read it, never
edit it) Part 2 lists 26 server items S0.1 to S3.8, each with what the server does today, what is
asked and what the app will read. The owner paused the fix run for it: phase 0 first, then the fix
run resumes (next section), then the later Road to 10 phases as the owner orders.

**Owner decisions (2026-11-22, in the backend session), on the decisions the app session drafted in
ROAD_TO_10 Part 3:**

| Decision | The owner's word | What the server does |
|---|---|---|
| D2, a request with a short wallet | **No**: a client cannot send a request with too little balance | S2.5 is NOT built; the 402 `insufficient_balance` refusal stays |
| D12, "do not renew after this month" | Yes | S2.7 (money: lock order and idempotency as `trainer_relations.py` demands) |
| D1, verified trainers only | **Not now**: unverified trainers stay listed | S2.6 is NOT built; the directory, matches and `featured_trainers` keep today's rule |
| D3 to D11, D13 to D16, D18 | Yes, as ROAD_TO_10 states them after `62c5b58` (D11: any member may host a challenge; D18: body measurements only, the two-currency wallet of 2026-09-21 stands) | as the items say |
| D17 | The owner's own (every account has its QR; only the agent reads it) | — |
| D19, a plan paid from the wallet | Yes (2026-11-22, confirmed in the backend session): "users can charge their wallets and use them to pay to subscribe in app features"; never inside a store build | S2.13 (phase 2): a plan and its renewal paid from the wallet on `apk` and `web` only, refused on `play`/`ios` by the same capability check (`can_purchase_platform_sub` is store-locked); 100% to the platform (2026-09-19); the gateway list names the wallet where it is a way to pay |
| D13, support contacts | `support@yalagym.com` (support), `admin@yalagym.com` (admin); the WhatsApp number comes before the launch | S2.12: `links.support_email` = support@yalagym.com; `links.support_whatsapp` empty until the owner gives the number |

**Owner decisions (2026-11-24, in the manager's session: «all as recommended»).** They close
the owed list below as it stood that morning. The ones marked «build» are in the next table.

| # | The owner's word | What the server does |
|---|---|---|
| 1 | iOS wallet moves stay off (B-199) | the flag stands: `can_transfer_wallet` off on the first `ios` build |
| 2 | An agent cannot delete the account while holding platform cash | build |
| 3 | The Play coaching switch stops new payments only | R8 as built: a paid request sent before the flip can still be approved, running coachings renew until the client sends `renew: false` |
| 4 | A plan upgrade's difference may be paid from the wallet, on `apk` and `web` only | build |
| 5 | The console must not approve a coaching with no consent and no charge | build (the `assigned_trainer` sync) |
| 6 | The 10-minute `already_paid` window stays | as built (`WALLET_REPEAT_WINDOW`) |
| 7 | Selling an unchanged starter copy stays allowed for now | as built (`copy/`, then a routine product) |
| 8 | A password change signs every device out at once | build (today access tokens live up to 60 minutes) |
| 9 | The stock `/admin/` is gated to operators (SIDE_BUGS S-274) | build |
| 10 | Build flags are read fresh: `policy_channelcapabilities` on `CACHALOT_UNCACHABLE_TABLES` (S-292) | build |
| 11 | Keep the 5-per-15-minutes password-change limit, and add an e-mail on a password change | build (the e-mail) |
| 12 | A trainer's request limit is 1,500 an hour | build (`training_platform/middleware.py`, from 1,000) |
| 13 | The trainer directory stays readable without sign-in | as built (`/api/v1/trainers/`, `trainers/public/`) |
| 14 | Accounts made outside sign-up default to Arabic (`users/models.py:297`) | build |
| 15 | A hidden challenge holds no hosting place | as built |
| 16 | Meal undo only on the same day; the rating and the badge stay | as built (S3.5) |
| 17 | The trial card's rules | as built |
| 18 | The store wording of `subscription_expired` | as built (R1) |
| 19 | An operator's trial grant is capped at the plan's length | as built (S-273) |
| 20 | Retiring the unused routes waits until after launch | S3.6 / S3.7 after launch |

**To build from them** (each a worker's item, through the integrator as every round):

| # | Item | Worker | State |
|---|---|---|---|
| 2 | An agent holding platform cash is refused account deletion, with a code | phase 3 worker | owed |
| 4 | A plan upgrade's difference paid from the wallet (`apk`, `web`; refused on `play`/`ios` by `can_purchase_platform_sub`) | money worker | owed |
| 5 | The console's `assigned_trainer` sync never approves a relation with no consent and no charge | money worker | owed |
| 8 | A password change ends every session at once (SIMPLE_JWT `CHECK_REVOKE_TOKEN` or equal; every device signs out once when it is deployed) | phase 3 worker | owed |
| 9 | The stock `/admin/` gated to console operators | phase 3 worker | owed |
| 10 | `policy_channelcapabilities` on `CACHALOT_UNCACHABLE_TABLES` | batch B worker | owed |
| 11 | An e-mail to the account on a password change (the 5-per-15-minutes limit kept) | phase 3 worker | owed |
| 12 | A trainer's request limit 1,500 an hour | batch B worker | owed |
| 14 | Accounts made outside sign-up default to Arabic (about 20 to 25 test files' setup) | batch B worker | owed |

**Pushed (2026-11-24, the owner's word, done by the manager):** `git push origin
spec/payments-compliance`, `f9f4cf1..9b286e7` (144 commits). The live server may be updated
to `9b286e7` (DEPLOY_CHECKLIST §12.5); anything landed after it reaches origin only at the
owner's next push.

**Phase 0** (the app does its own phase 0 in parallel; S0.1 first, the app deletes its own schedule
arithmetic when it lands). Each landed item: its route, fields and codes in the view docstring and
API_CONTRACT.md, the row's Status and Backend note when it is a BACKEND_BUGS row, and a message to
the app session ("Frontend handoff and app build") with the route, fields and codes.

| Item | Row | State |
|---|---|---|
| S0.1 one rule for today's programme day (D15: the next day not done; past its dates = `finished`) and a schedule read; `week_of` bounded | B-197 | DONE 2026-11-22: `routine/schedule.py` (the rule, in its docstring), `GET /api/routine/routines/{id}/schedule/`, the hero reads it (`cta.state` `finished` is new). Tests `tests/test_road10_s01_schedule.py`; `test_home.py` / `test_home_records.py` rest-day setups re-expressed under D15 with the reason |
| S0.2 the home hero's `counts.estimated_minutes` is that day's (the same number S0.1 answers) | B-196 | DONE 2026-11-22: `schedule.day_minutes`, the routine's own formula on the day's rows |
| S0.3 a trainer with an empty first name is not listed (directory, matches, `featured_trainers`) | B-198 | DONE 2026-11-22: `listed_trainers()`; the profile by id still answers; unverified stay listed (D1). Tests `tests/test_road10_s03_nameless_trainer.py` |
| S0.4 the account pages answer a trainer correctly (notification kinds, inbox, export, blocked, the delete-account refusal while coaching, no AI consent rows) | — | DONE 2026-11-22: checked inbox (`/api/social/notifications/`), export, blocks and erase (409 `trainer_has_active_clients`, approved relations only) — all answered a trainer already; FIXED `event_types/` (+ `role`, `role_event_types`, `role_email_event_types` from `notifications/audience.py`); AI consent offers a trainer nothing of its own (the coach is a client plan feature; the app hides the rows by role). Tests `tests/test_road10_s04_trainer_account.py` |
| S0.5 the gateway list says per gateway `available`, `kind` (`automatic` or `reviewed`) and the hours for `reviewed` (D5) | — | DONE 2026-11-22: `GET v1/gateways/` lists every registered gateway with `available`, `kind` (`gateway_config.gateway_kind`: token = automatic, account id alone = reviewed), `review_hours` (`PAYMENT_REVIEW_HOURS` = 24, **the owner confirms the number**), and `can_pay` + `reason` (`no_gateway`, `not_available_on_channel`). Tests `tests/test_road10_s05_gateways.py` |
| S0.6 home's cached page is dropped after the person's own action (plan built, request sent or answered, top-up credited, programme bought, subscription changed) | — | DONE 2026-11-22: new receivers on `diet.DietPlan`, `subscription.Subscription` and `wallet.Transaction` (the wallets' owners), and the trainer's draft send (`.update()`) retires the client's home itself; requests, purchases and a closed day already did; a meal confirmed retires it too (`diet.DailyProgress` save, asked by the app session). Tests `tests/test_road10_s06_home_fresh.py` |

**How the rest is built (owner, 2026-11-22: "watch the worker, keep managing it, start other workers
if needed, keep synchronised").** The backend session is the manager. Each worker builds a batch in
its own worktree and branch and reports every item to the manager (commit, the hand-off text,
migrations, settings, shared files). The manager reads the diff, merges on the branch
`road10/integration` (worktree `.claude/worktrees/rv-dec`, `fixrun/merge_one.sh` for the `.po`/`.mo`),
runs ONE full gate per merged batch there, fast-forwards `spec/payments-compliance` to it, and only
then sends the hand-off to the app session: the app reads the main checkout's source. One writer
per checkout: the main checkout is written by the manager only (the app session still commits its
BACKEND_BUGS rows there). Workers never edit this file, BACKEND_BUGS.md or SIDE_BUGS.md.

**The owner's numbers (2026-11-22):** a reviewed transfer is confirmed within 24 hours
(`PAYMENT_REVIEW_HOURS`); one member hosts at most 3 challenges at once (`CHALLENGE_HOST_LIMIT`).

**Batch A** (built in the main checkout by the phase-0 session, handed to the app item by item):

| Item | Commit | State |
|---|---|---|
| S2.8 password change while signed in (D14) | `11866ed` | DONE: `POST /api/auth/password/change/`; every other session ends, this one gets a fresh login |
| S2.12 support contacts (D13) | `4e5dea0` | DONE as its OWN route `GET /api/v1/config/support/` `{email, whatsapp}` (two tests pin the capabilities answer's exact contents); `whatsapp` empty until the owner gives the number |
| S2.1 home knows a request is open | `0b94e61` | DONE: the first-day hero says `waiting` / `invited` with the request and the trainer; with nothing open it keeps its old shape; the checklist's coach step reads `awaiting` |
| S2.2 a trainer's name and photo | `95c543e` | DONE: a trainer with a blank name is asked for it before the shell; the own-photo upload existed; the console can take a reported picture down |
| S2.3 the trainer's queue | `b18fb0b` | DONE: attention kinds for an unanswered message and a client awaiting a first programme; unread counts on the roster; the last session's id |

**Batch D, phase 3** (worker "Road to 10 phase 3", branch `claude/road10-phase3-632678`, merged on
`road10/integration` as `42e3109`; LANDED on this branch 2026-11-23 with S2.7, full gate at
`24170ca` on a fresh database: 3003 passed, 0 failed, 0 skipped):

| Item | Commit | State |
|---|---|---|
| S3.5 a meal confirmed today can be undone (D11) | `cd93730` | LANDED: `POST /api/diet/v1/client/meals/{id}/undo/`; new code `undo_window_closed` |
| S3.8 trainers in the community (D11) | `99c9032` | LANDED: no route refused a trainer; 7 tests now guard it, docstrings say so |
| S3.4 a starter challenge, and the hosting limit | `b266bcb` | LANDED: `manage.py create_starter_challenge --creator <operator>` (DEPLOY_CHECKLIST §6); the 4th hosted challenge answers 409 `challenge_host_limit`; a challenge an operator hid holds no place (the worker's choice, told to the owner) |
| S3.1 push payloads, code side | `72489fc` | LANDED: `request_id`, `client_id`, `trainer_id`, `session_id`, `routine_id` added where a page needs them (API_CONTRACT §4). The Firebase credentials on the server are the owner's step |

**Batch C, money** (worker "Road to 10 money", branch `claude/road10-money-bb9d57`):

| Item | Commit | State |
|---|---|---|
| S2.7 "do not renew after this month" (D12) | `7c8dc0a` | LANDED (merged as `b906639`; full gate at `24170ca`: 3003 passed, 0 failed): `POST /api/v1/coaching/renewal/` `{renew}`; `stop_renewal`, `ends_at`, `ending_reminder` on `coaching/current/`; push `coaching_ending_soon`; migration `users/0048`. Its own adversarial review found 2 MEDIUM, both fixed before the commit |
| S2.13 a plan paid from the wallet (D19) | `b65efbf` | LANDED (merged as `a15834f`; full gate at `12cd8df`: 3066 passed, 0 failed): `POST /api/subscription/v1/subscriptions/{id}/pay-from-wallet/` `{idempotency_key, amount, currency}` (all required), `require_purchase_allowed(PLATFORM_SUB)` first so `play`/`ios` refuse before the body is read; `PaymentService.pay_from_wallet` takes the price from the wallet to the platform (100%) and completes the payment in ONE transaction; the same key replays, another key within 10 minutes is 409 `already_paid`; `GET v1/gateways/` adds `can_pay_from_wallet` and `wallet` BESIDE `gateways`; the console's reversal refuses the payment's row (409 `plan_payment_leg`). Migration `subscription/0021`. The integrator checked the diff against the money rules (lock order, one transaction, no mint, refund at the leg's own amount); its own adversarial review found 4 MEDIUM, all fixed before the commit |

**Batch B** (worker "Road to 10 batch B", branch `claude/road10-batch-b-4a061f`, cut from `b18fb0b`;
merged at its tip `47207b0` as `3945875`; LANDED with S2.13, full gate at `12cd8df`: 3066 passed, 0 failed):

| Item | Commit | State |
|---|---|---|
| S2.11 body measurements (D18) | `4cd0dde` | LANDED: no new route; `POST /api/analytics/metrics/` takes `chest` 60-160, `arm` 20-60, `waist` 50-160, `thigh` 30-100, always in `cm` (new field code `unit_not_cm`); `GET metrics/?latest_for=<types>` answers the caller's newest row of each (400 `bad_metric_type`; not B-181's `latest_for`, which is on the exercise-progress list). Migration `analytics/0006` (choices only) |
| S2.4 the free first workout (D10) | `d9e73a5` + `47207b0` | LANDED: home section `trial_workout` and `GET, POST /api/routine/trial-workout/` (404 `trial_not_offered` + `reason`); the client's own copy is `trial`, never assigned, not listed, no schedule, and in none of the numbers (`routine/outcomes._counted`: the streak, the week, and since `47207b0`, after the integrator's review, the coach's roster, client file, digest, attention queue and console streak). Migration `routine/0036`; `bootstrap_platform` step "platform templates" |
| S2.10 platform starter templates (D16) | `0208f9e` | LANDED: eight platform templates (four goals x home/gym) in `public_templates/`, `by_platform`, `place` (field code `bad_place`), filters `?place=`, `?by_platform=`; a copy of a starter keeps its name; a trainer's own template cannot be made public through the API (400 `template_cannot_be_public`; the console still can). Migration `routine/0037`; the same `bootstrap_platform` step |

**The review round and Part 4** (2026-11-23/24: each worker reviewed another's landed batch,
and built R4, R5 and R8 of ROAD_TO_10 Part 4; merged on `road10/integration`, LANDED with the
full gate at `7c5f927` on a fresh database: 3162 passed, 2 failed, 0 skipped; the two failures are outside the round and pass alone or under a quiet load: SIDE_BUGS S-289, a test that fails in the hour after midnight, and S-290, S-245's wall-clock bound under a load average of 173):

| Item | Commits | State |
|---|---|---|
| Batch A review (S2.8, S2.1) | `b7ad726`, `f0b8b47`, `ace72c8`, `ca234f8` | LANDED: a new password is trimmed as sign-in trims; the five-guess limit counts each attempt before its check; a chat socket that only listens closes with 4001 at its token's expiry; a request to a trainer since suspended or blocked no longer blocks the next (never one holding money) |
| Routes review (S3.7 findings) | `384ed62`, `bb08e74`, `497cab0` | LANDED: S-269 the public-profile list lists only the search's set; S-270 a follow is never edited (400 `follow_not_editable`, NEW code); S-273 the operator's trial grant is bounded, logged and locked |
| Phase 0 review (S0.1, S0.3, S-262) | `3f491f0`, `2ccad4c`, `192c3ef`, `3eacd83`, `2bb99bb` | LANDED: a bought programme's `week` is its session block; both trainer lists use the directory's rule (`username` null, `client_count` from 5); a bought programme plans its sessions a week on Home and in the weekly goal |
| Phase 3 review (S3.5, S3.1, S3.8) | `3396570`, `5ea4b34`, `ec8977a`, `7b7014a`, `caf15fd`, `264bc10` | LANDED: every write to a meal and its foods locks the meal first; `client_request_approved` / `_rejected` carry `trainer_id` and `client_id` both ways (the trainer's `trainer_id` was the client's id); the S3.8 sweep calls the 19 routes it skipped |
| R4 account deletion per role | `4f3ec76`, `f89459b`, `43fd79a`, `41e5aec`, `4ec3fcf` | LANDED: an erased or suspended agent leaves the cash directory; `money` on GET/DELETE `privacy/erase/`; the console's "Erase the matching account, then close as erased" (operator + change permission; the account found by `find_by_email`, never `iexact`, two matches leave the request open: the integrator's HIGH, fixed in `4ec3fcf`; logs inside the transaction) |
| R5 the store reviewers' accounts stay alive | `3203bae` | LANDED: a later `seed_review_accounts` run moves a programme's end on and keeps a coached month paid through the agent top-up path and `renew`; DEPLOY_CHECKLIST §12 has the owner's step |
| R8 coaching off on Google Play with one row | `7973ea6` | LANDED: tests only, DEPLOY_CHECKLIST §13 has the console step |


**Round 3, round 4 and the walk seed** (2026-11-24, one landing: merged on
`road10/integration`, LANDED with the full gate at `7d02bcc` on a fresh database: 3333 passed, 0 failed, 0 skipped; round 3's own first gate, at `16e99dc`, failed on ONE test: tests/test_regression_gate.py `test_the_notification_list_does_not_query_per_notification`, 7 queries for a page of 20 against a budget of 6, caused by R1 (the build's capability rows, read once per request); fixed in `169a2c3` and re-gated with this landing, 3209 passed there otherwise):

| Item | Commits | State |
|---|---|---|
| R1 store-safe notification texts by build | `d5406b5`, `b3fb58a`, `169a2c3` | LANDED: `notifications/build_gate.py` picks a notice's text from policy's flags for the reading build (inbox, at read time) or the build a device token recorded (push, per device); `checkout_expired` hidden, `subscription_expired` and `coaching_payment_due` have a store text on a build without the flag; apk and web unchanged. Migration users 0049 (`DeviceToken.app_channel`), in DEPLOY_CHECKLIST §12.5 with what breaks without it. RATCHET RE-RECORDED, deliberately: the inbox's query budget 6 -> 7 (`169a2c3`, the reason at the assertion): the seventh query is the build's capability rows, read ONCE per request for the store-safe texts, not per row; not cached in policy (the manager's ruling: a stale flag on the path that decides what a build may sell is the worse risk); a page of 5 and a page of 20 now measured equal, and every inbox route held to one capability read |
| B-199 wallet money by build | `cef7ec3` | LANDED: docstrings, API_CONTRACT and 22 tests pin that every WALLET_MOVE path reads the moving request's build; no behaviour changed; the first ios build's agent job is the owner's question |
| Console actions declare their permission (MEDIUM, security) | `e826cbf` | LANDED: `change` or `add` on every action of both sites (67 had none); SIDE_BUGS S-279 |
| `password_not_set`; the console's reset ends every session | `3b126de`, `054f7b6` | LANDED: NEW code on the password change; `users/utils.py end_every_session` shared; SIDE_BUGS S-275 |
| S-289 the challenge test after midnight | `5b26d6e`, `9aea161` | LANDED: the test's setup dates its sets as the app does; a set that names its session and sends no date is dated on the session's start day (a sent date kept); S-291 keeps the time-zone case |
| Coaching money in the console (S-264, S-266, the phase 3 review) | `0054a5a`, `2a74aac`, `a64f924`, `2abfa88` | LANDED: a re-point keeps both ends of a running coaching; the hourly run wakes and ends, with no charge, a frozen coaching the trainer scheduled to end; a trainer change that would end another coaching is a field error with nothing written (was a 500, or a month left in escrow on a rejected row); the stock `/admin/` relation page is read-only and neither site deletes a relation |
| S-272 the manual achievement check | `bc5314a` | LANDED: 6 an hour per account, then 429 `throttled` |
| Phase 0 review, round 4 (S0.3, S2.1, S2.12, S-257/S-267) | `1e2d1a2`, `9ac47a8`, `1531e61`, `f480342` | LANDED: a name of invisible characters is no name, one rule (`users/names.py`) for `onboarding_missing` and the directory; the client's own request leads Home's hero; the support WhatsApp is `+` and digits; the test setup puts back the migration-seeded tables after a flush |
| The walk seed (development only) | `ebc6cab`, `507f733`, `b1e68bb`, `1bccaf3`, `b983516`, `604ea11`, `b5e2dae`, `1734e28`, `a0d2d05`, `4fee0ca`, `590e359`, `92aa64b`, `62aed3f`, `ec6581c`, `367ff18`, `a32b91c`, `fd5237a`, `781c8a5` | LANDED: `manage.py seed_walk_states` puts ordinary accounts `walk.<state>@yalla.test` into each state the app's walk-throughs need, through the platform's real routes and services, and writes `var/walk_states.json` (git-ignored, no password or token); refuses any settings but `settings_local`; and any database not on this machine; a reset retires the old accounts and deletes nothing, and closes their open withdrawals (`payouts.services.fail`) and gives back their held months (`release_hold`) so the operator's queues and escrow stay as they were; three sweeps take an optional scope for it (`expire_lapsed_subscriptions`, `send_renewal_reminders`, `remind_upcoming`; None, the beat's call, is every row as before); nothing at request time imports `users/walk` or reads those addresses (a scan test) |


**Round 5** (2026-11-24: merged on `road10/integration`, LANDED with the full gate at `bd087df` on a fresh database: 3359 passed, 0 failed, 0 skipped):

| Item | Commits | State |
|---|---|---|
| The inbox's language (notification language P1; F1, HIGH) | `d92f459` | LANDED: `routine_assignment`, `session_completed`, `progress_milestone` and `session_reminder` rows store their sentence by key (`variant`) with its params beside the `message`; the inbox renders the variant in the request's language, a push in the saved one; old rows and `custom` read their stored text. F1: the workout reminder was rendered with no language, English for everyone, in the inbox and the push. No migration, no new msgid. Content names inside a notice stay as written (SIDE_BUGS S-294) |
| BACKEND_BUGS B-200 Home's open request on the checklist | `6660e75` | LANDED: the checklist's `coach` step carries `direction`, `request_id` and `trainer {id, first_name}` while a request is open, from `home/first_day.py open_request_facts`, which the hero reads too; additive |
| The walk seed on the developers' database (SIDE_BUGS S-295) | `db33f72`, `994ecc4`, `7e90ea8` | LANDED: found on the app side's first real run after the last landing (a 'Pro (USD)' plan, no exchange rate: the 12th state stopped). A plan is chosen in the client's own wallet currency, never across currencies, and no rate is created; `refuse_unless_ready` names every gap once before anything is written; `--keep-going`; walk accounts saved as Arabic; tests/walk_fixtures.py builds the developers' shape. The traps section below has the lesson. S-296 (LOW) keeps the money states' duplicate helpers for a later round |

**S3.7, the routes the app never calls:** CLASSIFIED 2026-11-23, read-only (the table is the
manager's note `road10-s37-unused-routes.md` in the side-session notes folder). 93 of the 301
contract routes have no caller in the app: twin 43, dead 20, unbuilt 15, operator 6, machine 5,
web 3, agent 1. Recommendation: keep 23, retire 61 behind a v2 prefix, ask the owner about 9.
NOTHING is retired: that is the owner's decision. Its security findings are SIDE_BUGS S-269 to
S-274, each being verified by the worker named there.

**Not built:** S2.5 (D2: no), S2.6 (D1: not now), S2.9 (later), S3.2 (the ShamCash rebuild waits for
`sc-epay-doc.pdf`), S3.3 (the owner's server update: DEPLOY_CHECKLIST §12.5), S3.6 (after launch).

**Owed by the owner** (after his decisions of 2026-11-24 above): the ShamCash gateway: he
has an API key and named the documentation (https://shamcash-api.com/en/docs); the money
worker is reading it, and nothing is built on the gateway until his word on the plan; the
backup bucket (hours away: `fx/w2-backups` needs it for its first drill); the Firebase
credentials on the live server (`FIREBASE_CREDENTIALS_JSON` in `deploy/.env`, his step;
the app needs its own client files); the support WhatsApp number (it comes last); a review
of the CONTENT the batch B worker wrote, the two free first workouts and the eight starter
templates (`routine/data/platform_templates.py`). The server update (S3.3) is
DEPLOY_CHECKLIST §12.5, "The Road to 10 update": migrate through the compose `migrate`
service, then `bootstrap_platform` (writes the ten platform templates) and
`create_starter_challenge`; after it, and again before each store submission,
`seed_review_accounts` (DEPLOY_CHECKLIST §12, the password at a hidden prompt).

## The fix run (started 2026-09-28, owner's go-ahead in the backend session) — PAUSED 2026-11-22, RESUMED 2026-11-24 (the owner's order)

**Review first.** Every open BACKEND_BUGS row B-136..B-183 was verified against the code
(refute-first, probes on cloned databases), and every refutation or severity change was
audited: 35 confirmed, 11 partly right, B-141 stale, B-151 refuted (both closed with notes).
Only three stayed P1: B-136, B-167, B-172. B-172 turned out to be a REGRESSION of the
Django 5.2 upgrade (dj-rest-auth 7.2 made `username` required, refusing every app
sign-up); fixed first (`580b5e3`). B-183 fixed (`9924bfe`), S-244 fixed (`554c604`, the
Postgres container recreated with 512 MB shm), S-245 fixed on `fx/s245` (JIT off for the
local/test database), merged with wave 1.

**Owner decisions (2026-09-28, in the backend session):**

| Row | Decision |
|---|---|
| B-182 + home | A COACHED client also gets `ready_programs` (same build gating, owned rows `owned`/`can_buy: false`); an uncoached buyer gets the day hero only; a former client whose day is refused gets the find-a-trainer hero |
| B-163 | A direct client->trainer transfer is a coaching payment: platform 20% / trainer 80%. Optional note, max 140, erasable row, both sides see it |
| B-154 / B-164 / B-165 | Privacy-first: `buyers` shown from 5 buyers; follower lists private to their owner (no route); no trainer gender (verified badge + photo only) |
| B-153 / B-156 | Keep one repeated week. Goals a closed list (build_muscle, strength, fat_loss, rehab) + a `place` attribute (home / gym); catalogue filters goal, place, search |
| B-137 | Trainer macro split allowed: energy within ±10% of calories, protein within the engine's bounds, draft only, `bad_macro_split` |
| B-145 | ensure/ from another device refuses 409 `key_paired_elsewhere` |
| B-159 / B-160 | Leaving / removing reasons: the drawn codes, operators only, kept anonymised on erasure, feed nothing yet |
| B-161 | `exercise_reps` (review threshold 500/day) and `streak_days` (current run; a day without a credible set breaks it); «عدد التمارين» = workouts finished |
| B-162 | `audience` my_clients: hidden from and closed to non-clients; frozen clients count; ended clients stay in the standing; winner badge still needs 5 |
| B-172 | A derived username with a banned term or a reserved platform name becomes user/userN, never refused |
| B-174 | Production refuses to boot without an https `PUBLIC_PAGES_BASE_URL` |
| B-176 | A 12th home section `day_timeline` in the artboard's order; `nutrition_today.timeline` kept |
| Template goal (2026-10-07) | `POST/PATCH /api/routine/templates/` take `goal` only as one of the four codes (any case, the code stored); anything else 400 `bad_goal` (`8f15972` on `fx/w1-programs`) |
| Development data (2026-10-07) | Every row in the development database (programs, templates, users, everything) is throwaway: the owner drops it himself before launch, and production starts empty (`bootstrap_platform`). So a fix needs no data migration or compatibility for rows written during development |

**GitHub's line merged in (2026-11-21).** The deployed line (GitHub's `spec/payments-compliance`, PR #2 merge `f77b3e1`: Django 5.2.19, Pillow 13, `deploy/`, S3 media, the AI provider settings, security CI) and the fix run's 107 local commits were merged (`e452d21`); `ccd5da0` makes deploy/setup.sh write `PUBLIC_PAGES_BASE_URL` (required in production since B-174, which the live server's deploy/.env lacked) and the app-link identifiers, with a test that boots production on setup.sh's file and the compose environment alone. Full gate on `ccd5da0`: 2900 passed, 0 failed. Pushed. The server update needs one line added to deploy/.env first: DEPLOY_CHECKLIST.md §12.3.

**PAUSED 2026-11-22 (owner): Road to 10 phase 0 goes first, then the fix run resumes here.** The
owner's list of 2026-11-21 for this run: wave-2 rows B-142, B-143, B-149, B-150, B-168, B-177, then
B-190, B-195; the small items (Semgrep false positives and the secret-scan step that never matched,
the AI coach limit 50 -> 10, the nightly database copy with a restore drill, the deploy-key steps for
a private repository); then wave 3. Everything below is on local branches cut from `ccd5da0`, in the
worktree named, NOT merged and NOT pushed:

| Branch (worktree `.claude/worktrees/…`) | Holds | Left |
|---|---|---|
| `fx/w2-diet` (`wf_45b02467-91e-1`) | B-142, B-143 (docs naming the shipped statuses and verdicts), B-195 (`allergens`/`allergy_terms` on the client's own preferences read); report done | LANDED 2026-11-24 (merge `3d2c756`; B-142 also names S3.5's undo, 409), full gate at `bd8016a` on a fresh database: 3425 passed, 0 failed, 0 skipped |
| `fx/w2-chat` (`wf_45b02467-91e-2`) | B-150 (`frame_payload`), B-149 (the API envelope on media refusals), B-177 (Ogg/WebM length read on the request, 90 s cap, a worker fills a missing length); report done | review: B-177 parses untrusted bytes, fuzz it |
| `fx/w2-small` (`wf_45b02467-91e-3`) | B-168 (docs), B-190 (`utc_offset_minutes` on agent/me), coach limit 10 from one optional `AI_MAX_MESSAGES_PER_DAY`; report done | LANDED 2026-11-24 (merge `1e14003`, two conflicts against S0.4 `ad4214d` resolved by hand keeping both), full gate at `bd8016a` on a fresh database: 3425 passed, 0 failed, 0 skipped; the allowance drops to 10 on the first server update after the owner's next push |
| `fx/w2-ci` (`wf_45b02467-91e-4`) | wss:// in the docs, nosemgrep with reasons on the Dockerfile and the two raw queries, the secret step rewritten to key shapes over tracked files (`tests/test_security_scan_job.py`); the builder ran out of usage before its report | LANDED 2026-11-24 (merge `b1c6f7c`), full gate at `bd8016a` on a fresh database: 3425 passed, 0 failed, 0 skipped; its key-shape scan finds nothing on the merged tree (run by the integrator with `git grep`); the proof that is left is GitHub's security-scan run after the owner's next push |
| `fx/w2-backups` (`wf_45b02467-91e-5`) | WIP: `backups/` app (streamed authenticated encryption, store, retention, drill, health, model + migration 0001), `deploy/backup.sh`, `deploy/restore-drill.sh`, `deploy/cron/yallagym`, setup.sh writes `BACKUP_ENCRYPTION_KEY` | every test of its brief, DEPLOY_CHECKLIST §3/§9/§12 docs, the deploy-key steps, boot refusing a half-set configuration, then review |

**Landed 2026-11-24** (the integrator, `road10/integration`, LANDED with the full gate at `bd8016a` on a fresh database: 3425 passed, 0 failed, 0 skipped):
`fx/w2-diet`, `fx/w2-small` and `fx/w2-ci`, read diff by diff first (nobody else had
reviewed them), and the walk seed's polish (`36c94c6`: the up-front check reads the amounts
each state declares; the money states use the shared `plan_for` and `top_up`, S-296
closed). BACKEND_BUGS B-142, B-143, B-195, B-168 and B-190 filled. Still on their branches:
`fx/w2-chat` (the phase 3 worker fuzzes B-177's readers first) and `fx/w2-backups` (the
batch B worker is finishing it; the owner's bucket is hours away).

To resume: the briefs and the three finished reports are in the backend session's directory
(`~/.claude/projects/-Users-mac-Desktop-Git-t2-Training-platform/741ec8b2-f2c0-4b8b-a55f-bdc282a3d595/fx-w2/`,
`reports_w2_first.json`; wave-3 briefs in `fx-w3/`), with its last workflow script
(`workflows/scripts/fix-run-wave-2-resume-*.js`: finish backups, one adversarial reviewer per
branch, repairs in the same worktrees). Test databases `test_tp_w2_<area>` (and `test_tp_w2old_<area>`
for the old-code proof) exist; run Python with the private venv `741ec8b2-…/venv-merged` (the
merged lock). Then merge one branch at a time onto `spec/payments-compliance` (it has moved: the app
session commits BACKEND_BUGS rows there), fill Status and Backend note, ONE full gate with
`--create-db` (backups adds a migration), push on the owner's word, and give the server steps (the coach
limit drops to 10 by itself; `BACKUP_*` stays unset until the owner makes the bucket). New rows since:
B-196 and B-198 go into the next run (they are Road to 10 S0.2 and S0.3), B-197 is S0.1.

**Waves** (each: worktree builders from one base, tests proven on the old code, merged one
branch at a time, then ONE full gate + the smoke; a review of the merged diff after):

| Wave | Builders (rows) | State |
|---|---|---|
| 1 | payments (B-167, S-232, S-234b, S-238..S-241, B-171), programs (S-234a, B-153..B-156, B-169, B-170, B-180), routine-core (B-136, B-139, B-152, B-157, B-158, B-181 = S-164), diet (S-212, S-215, S-216, S-243, B-137, B-140), accounts (S-235, S-236, S-242, B-159, B-160, B-165, B-166, B-173), home (B-175, B-176, B-178, B-179, B-182, S-193), wallet (B-138, B-144..B-148, B-163, B-184, S-237), privacy (S-213, S-214, S-225, S-231, S-233, B-174), social (B-161, B-162, B-164, S-189, S-194, S-218, S-219) | MERGED 2026-10-07 (`merge/w1`): 57 rows fixed, B-164 and S-193 closed, each test proven on the old code. Six builders lost their login mid-run on 2026-09-28 and were resumed in their worktrees. Migrations renumbered at the merge: routine 0033 quick_log -> 0034 place -> 0035 goal choices, subscription 0019 catalogue_key -> 0020 payer proof. The merged-wave review (5 lenses, verified) confirmed 12 findings (2 HIGH: Arabic allergies written «للوز», a challenge streak broken after its window); all 12 fixed in the repair pass `fx/w1r-*` before landing. Full gate on `82ef697`: 2849 passed, 0 failed (JIT off: the diet week passes). Template goals closed (owner 2026-10-07) |
| 2 | payments2 (S-052, S-180, S-184, S-185, S-228..S-230), routine2 (S-114, S-165..S-168, S-176, S-177, S-183), diet2 (S-198, S-217, B-142, B-143), accounts2 (S-106, S-181, S-200..S-202, S-223), wallet2 (S-162, S-179, S-221, S-222), platform (S-054, S-178, S-186, S-197, S-199, S-203, S-220, S-224), chat-media (B-149, B-150, B-177, S-187, S-188, S-211, S-226), notifications (B-168, S-196, S-206..S-208), ai-achievements (S-190..S-192, S-195, S-204, S-205) | The owner's BACKEND_BUGS subset (B-142, B-143, B-149, B-150, B-168, B-177, B-190, B-195) is built on `fx/w2-*`, unmerged, PAUSED for Road to 10 (see above). The SIDE rows are not started; the older ones are re-verified before fixing |
| 3 | Re-verified 2026-11-24 against `9b286e7` (the note `fix-run-backlog-verified.md` and `fix-run-backlog-verdicts.jsonl` in the manager's notes folder): 66 buildable rows in 15 batches, 10 rows that are merges of branches already built, 24 owner rows. The table below | PLANNED |

**Wave 3, by batch** (2026-11-24; rows `open` are buildable now, `owner` wait for a decision;
no two batches share a source file unless the note says «after»). The PAY, SOC-1, DIET and
PLT-2 batches start only after the first resumed landing has landed (the paused branches edit
their files); each starts after its worker's current list:

| Batch | Area | Buildable | H / M / L | Owner rows | Holder |
|---|---|---:|---|---:|---|
| G5 first (SIDE_BUGS S-299, HIGH) | an empty or retired routine pays month 1: a guard before `settle_first_month` | 1 | 1 / 0 / 0 | 0 | money worker, first |
| PAY-1 Wallet console reversals and coaching holds | payments | 3 | 0 / 3 / 0 | 2 | money worker |
| PAY-2 Subscription checkout and gateway (S-263 after PAY-1) | payments | 7 | 0 / 3 / 4 | 2 | money worker |
| PAY-3 Small money leftovers | payments | 4 | 0 / 0 / 4 | 0 | money worker |
| RTN-1 Routine: assignment guard, exercise retire, limits, codes, day outcomes (without G5) | routine | 14 | 1 / 10 / 3 | 4 | no builder yet (brief `fx-w3/brief-rtn1.md`) |
| DIET-1 Impossible dates, Arabic refusals, small command and task fixes | diet | 4 | 0 / 2 / 2 | 0 | no builder yet |
| DIET-2 AI assistant: weakest-meal window, refund key | diet | 2 | 0 / 1 / 1 | 0 | no builder yet |
| DIET-3 Diet write lock order (before NOT-1's G8a, G8b) | diet | 1 | 0 / 0 / 1 | 4 | no builder yet |
| ACC-1 Profile pictures, blocks, names, the coach roster, the trainer queue | accounts | 6 | 0 / 2 / 4 | 1 | phase 3 worker |
| ACC-2 Erasure and export | accounts | 3 | 0 / 1 / 2 | 2 | phase 3 worker |
| SOC-1 Chat socket and history codes (after `fx/w2-chat` lands) | chat | 2 | 0 / 1 / 1 | 0 | phase 3 worker |
| SOC-2 Community, achievements, moderation console | social | 6 | 0 / 2 / 4 | 2 | no builder yet |
| NOT-1 Notices: unassign, diet plan written, trainer-scheduled end, worker tolerance (after RTN-1, DIET-3) | notifications | 5 | 0 / 2 / 3 | 2 | batch B worker, later |
| PLT-1 Middleware limiter, language header, the gate file (before S-274) | platform | 4 | 0 / 0 / 4 | 1 | batch B worker |
| PLT-2 Cache bumps, settings, catalogue | platform | 3 | 0 / 0 / 3 | 3 | batch B worker |
| TST-1 Probes and environment-dependent tests (S-224 before S-195) | tests | 2 | 0 / 0 / 2 | 1 | batch B worker |
| MERGE the paused branches, never rebuilt | various | 10 | 0 / 0 / 10 | 0 | the integrator |

Not builder work: nine SIDE rows were already fixed and three stale (closed in SIDE_BUGS with
their commits, 2026-11-24); BACKEND_DELTA item 20's G7 is closed and G15 settled by D15;
S-211 needs one run under the lock's Pillow first (the note, section 8); 24 rows wait for the
owner (the note, section 6), among them G11 (SIDE_BUGS S-300, HIGH): does a diet plan the
trainer sends pay month 1?

**Landing order** (the manager's ruling, 2026-11-24): (1) the first resumed landing
(`fx/w2-diet`, `fx/w2-small`, `fx/w2-ci`, the walk polish). (2a) phase 3's branch (the
agent-cash refusal `b45cdda` and what follows on it), the batch B worker's three items, the
money worker's G5 (committed alone, first), upgrade and console items: it lands when those
reports are in and its gate passes; a late item rides the next landing, and no money fix
waits for a straggler. (2b) `fx/w2-chat` after its fuzz review and `fx/w2-backups` finished:
the backups branch lands without a real drill once its tests and docs are in and its crypto
and scripts are reviewed (the docs say what the drill needs; the drill is the owner's day);
if only one is ready it rides the next wave 3 landing. Then the wave 3 batches as they
report. Every landing: one gate through the queue, the push-registry line in the hand-off,
the app manager's «go» before the fast-forward, no push (the owner is asked after each).

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
   (`AI_ASSISTANT_CONFIG['MAX_MESSAGES_PER_DAY']`, from the optional `AI_MAX_MESSAGES_PER_DAY`:
   10 since the owner's word of 2026-11-21, it was 50; `fx/w2-small`) applied to every plan
   that has `has_ai_advice`, while `Plans.png` prices three different allowances (Basic none,
   Premium 50 a day, Professional "بلا حدود"). `usage/today/` answers 10 to everyone
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
5. **Changing plan mid-period — DECIDED by the owner, 2026-09-23.** An UPGRADE is allowed
   while a plan runs; a DOWNGRADE is blocked until the period ends (then `change-plan`).
   Credit for what is left = the price paid × days left ÷ the period's length (days, not
   features used). The new plan's price minus that credit is what is left to pay, shown
   before paying; the new plan starts when that payment clears and runs its FULL length.
   Built as `GET`/`POST v1/subscriptions/{id}/upgrade/` (`PaymentService.quote_upgrade`,
   `start_upgrade`, `_apply_upgrade`). Decided in code after an adversarial review,
   reversible: the credit is per running payment at its own price; it is re-read when the
   money clears and the new period is what money + credit buy (full length when paid as
   quoted); every contributor's share of that period is recorded, and a refund takes
   back exactly the refunded money's share (refunding the upgrade leaves the credited
   days on the new plan). An upgrade whose credit covers the whole new price is refused
   (`upgrade_not_payable`) — the owner declined both extra days and wallet credit for
   leftover value.
6. **DECIDED by the owner, 2026-09-24** (on BACKEND_BUGS B-066..B-086). The principle:
   "don't patch the screen to hide an underlying contradiction; fix the model/contract".
   (1) P0: AI training records are ANONYMISED — no user id in the dataset; the only link
   kept is a keyed pseudonym, strictly for opt-out and erasure. (2) "Help us improve" off
   deletes the person's training copies. (3) Retention stays as a second safeguard:
   every copy expires and is purged automatically. (4) Achievements = finished workouts +
   days with logged sets. (5) Weekly workout goals are system-calculated — count AND
   target from the plan/profile; no conflicting manual number. (6) The migration that
   reopens wrongly completed goals stands; awarded badges are not revoked. (7) Admins,
   agents and unverified accounts stay out of people search. (8) P0: suspended accounts
   leave every public surface (leaderboards, search, lists); history stays internal.
   (9) P0: every refusal names its own `code` (API_CONTRACT §6 confirmed) — done now,
   before the Flutter integration goes deeper.
7. **Left by building the decisions of 2026-09-24** (recorded, reversible):
   (a) DEPLOY BLOCKER: set `AI_TRAINING_PSEUDONYM_KEY` in the fly secrets (e.g.
   `openssl rand -hex 32`) — production refuses to boot without it, and it must NEVER be
   rotated while training copies exist (a new key cannot find them, so opt-out could no
   longer delete them). (b) Training copies keep what the person typed minus names,
   e-mails, phones and links; a health detail typed in a message stays — the owner may
   want health terms scrubbed too. (c) Retention stays 365 days (the maximum the server
   accepts); lowering `AI_TRAINING_RETENTION_DAYS` needs the app wording to follow. (d)
   The account export includes the person's anonymous copies (content, without the keys).
   (e) A late 👍/👎 still labels its copy through a second keyed pseudonym of the reply.
   (f) A challenge's winner is settled `Challenge.REVIEW_HOURS` (72) after it ends (F20,
   merged from Opus's `build/anticheat`): a first place suspended BEFORE the settlement
   holds no place, like a disqualified one, and the next visible participant wins; after
   the settlement the stored ranks are the record and nothing moves them — not a
   suspension, not a later review decision (fixed in `67f829e`). (g) Login keeps the top-level `validation_error`
   (a gate test pins it) with the reason in `field_errors.non_field_errors[0].code`
   (`invalid_credentials`, `email_not_verified`, `credentials_required`). (h) Role
   refusals from permission classes stay `permission_denied` (§2 defines it). (i)
   `gateway_error` still covers three causes (gateway refused / amount out of bounds /
   account missing e-mail or phone). (j) Agent top-up caps answer 429 with
   `daily_limit_exceeded` / `monthly_limit_exceeded`.

8. **Left by the trainer shell, 2026-09-24** (BACKEND_BUGS B-089..B-095 and the reviews
   of each branch; built one way, reversible, the owner confirms or changes):
   (a) VERIFICATION vs decision 5: the design says verification unlocks the directory,
   matching and requests; BACKEND_DELTA decision 5 keeps unverified trainers matched
   (ranked below). Built: verification only sets `trainer_is_verified` (ranking, the
   paywall exemption, ranked challenges, withdrawals per spec §8), so the app's "what
   verification unlocks" card over-promises until the owner picks. (b) A pending trainer
   keeps routines and templates but not the diet tools (paywall) — should they get them?
   (c) The nutrition certificate "raises your ranking" in the design; nothing reads it.
   (d) Documents of an application never submitted, or rejected and never resubmitted,
   are kept until the account is deleted — pick a window. (e) HEIC is refused (the app
   sends JPEG). (f) Review reasons are free text shown to the trainer; no reason codes.
   (g) One trainer-certificate slot meets "at least one"; a second is an additive slot.
   (h) CHAT MEDIA: videos and voice notes use the media kit's caps (64 MB, 90 s); a voice
   note gets its own 10 MB cap and each sender a rolling-24h quota of 60 files / 512 MB
   (`media_quota_exceeded`) — numbers for the owner. (i) Only a video that names an
   exercise is a form check. (j) "Program ending" appears 7 days ahead (the app's own
   window; the 3-day reminder is the client's paid month). (k) A client whose coaching is
   frozen, unpaid or ended is left out of the attention queue. (l) A photo, voice note or
   video the sender takes back is kept in storage like taken-back text (for reports), never
   served again. (m) PRICES: coaching fees show on `apk` AND `play` (the existing rule
   hides them on `ios` and on requests without a channel) — confirm `play`. (n) A request
   to a trainer who set no price is quoted at 0 and stays free when approved (it used to
   charge the rate set later). (o) Height and weight are not on a pending request (the
   TrainerRequestDetail design shows them before acceptance). (p) DECIDED by the owner
   (2026-09-24, relayed by the app session that built it, merged `7831839`): the Challenge
   Winner badge needs at least 5 participants in the standing with logged progress
   (`social/standing.py MIN_PARTICIPANTS_FOR_WINNER`, counted at the settlement and
   recorded on `Challenge.settled_active_participants`; below 5 the challenge settles
   with no winner). The AI provider question (OpenAI not serving Syria, a possible move to
   Gemini) is POSTPONED by the owner: nothing was built. (2026-11-17: the provider is
   now configuration, not code — `AI_API_KEY`, `AI_BASE_URL`, `AI_ASSISTANT_MODEL`,
   `AI_EXTRA_BODY`; with no key the coach answers `config_error` instead of killing its
   socket; `manage.py ai_coach_eval` compares models on a fixed set of Arabic questions.
   The owner chose Gemma, an open model, on 2026-11-17, served by Cloudflare Workers AI
   on the first deployment; `privacy/disclosure.py` `ai_provider`, the inventory and the
   policy drafts say so. Another provider changes those three again.)
   HOSTING (2026-11-20): Fly.io, Google Cloud and the managed databases bill through
   processors that refuse a Syrian company, so the first deployment is ONE Hetzner
   server from `deploy/` (Docker Compose: Postgres, Redis, web, worker, beat, Caddy with
   Let's Encrypt only) and media is an S3-compatible bucket (`MEDIA_BACKEND=s3`,
   `training_platform/s3_storage.py`, Hetzner Object Storage). `DEPLOY_CHECKLIST.md`
   section 12 is its runbook; `fly.toml` and the GCS backend stay, unused for now.
   Still owed on that layout: an off-server database dump on a schedule, and a restore
   drill.

9. **Left by the builds of 2026-09-24 (evening)** (trainer money, agents, routine
   products, the program builder, trainer nutrition; each branch reviewed adversarially
   and its confirmed findings fixed with reproducing tests; built one way, reversible):
   (a) ROUTINE PRODUCTS: 20% commission on sales; a refund REVOKES the copy (not freezes);
   preview 0-7 days and always shorter than the template's week; price ceiling
   10,000,000; a product sells only while its trainer is verified; any template change
   takes it off sale until re-reviewed; only clients buy; the buyer is never named to the
   trainer; a sale's share settles 3 days like a coaching payout. (b) PROGRAM BUILDER:
   `copy_week` load factor 0.5-1.5 and it never replaces a filled week; the injury map is
   narrow and gives NO lighter-load number (a physio or the owner may want one). (c)
   TRAINER NUTRITION: a trainer plan created without meals is now a DRAFT until its first
   meals land (it used to retire the client's plan at once); a plan is never sent to
   begin before today; 500 meals / 364 days per plan; adherence is the calorie-based
   figure over ticked meals; the new trainer reads of a client's preferences, adherence
   and automatic plan refuse while the coaching is frozen (older trainer reads do not —
   decide which rule is right) and refuse admins; saved meals capped at 300 x 30 foods.
   (d) The console QR sign-in (B-120) is built and OFF (`OPS_QR_LOGIN_ENABLED`): turning
   it on is a new sign-in path the owner must approve. (e) Chat media numbers (Open 8 h)
   are now enforced: 60 files / 512 MB a day per sender, 10 MB a voice note.

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
- **A dev-only tool tested only on a freshly bootstrapped database.** `seed_walk_states`
  passed every check on test databases holding bootstrap's plans, and failed on the
  developers' first real run: their database also had a cheaper 'Pro (USD)' plan and no
  exchange rate, and the states picked a plan by raw price across currencies (SIDE_BUGS
  S-295, Road to 10 round 5). Test such a tool against a database shaped like the
  developers' own: tests/walk_fixtures.py `walk_platform` now builds that shape.

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
