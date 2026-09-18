# Frontend Handoff — build against this API now

**Written 2026-09-18, backend HEAD `2708845` on branch `noti-system`.**
For the agent or engineer starting the Flutter client. Read `API_CONTRACT.md` first: it
is generated from the URL resolver and is the base truth. This file is the **delta since
that freeze**, plus what is safe to build and what is still moving.

Backend state: 334 API routes, 370 tests green on a freshly created database.
**Not deployed yet** — there is no running staging host. Point the client at a local
`runserver`/Docker until the first Fly deploy lands.

---

## 1. Build now — these areas are settled

| Area | Base path | Notes |
|---|---|---|
| Auth, OTP, password reset | `/api/auth/` | Settled this week; see §3 for what changed |
| Profiles, trainer↔client relations | `/api/auth/` | Consent flow is final: a request is approved by the side that did not make it |
| Routines, sessions, set logs | `/api/routine/` | 88 routes; list/detail shapes stable |
| Diet plans, meals, preferences | `/api/diet/` | Read paths stable; see §4 for write paths in flux |
| Social feed, posts, comments, challenges | `/api/social/` | Stable; comment rules tightened (§3) |
| Notifications, device tokens | `/api/notifications/`, `/api/auth/device-token/` | Stable |
| Analytics, achievements | `/api/analytics/`, `/api/achievements/` | Stable |
| Wallet (clients, trainers, agents) | `/api/wallet/` | Stable. **Clients need it:** requesting a trainer requires the fee in the wallet, top-ups from agents land there, and clients can send money to a trainer. Trainers see earnings there; agents top up clients from it |

## 2. Integration rules that will save you a refactor

1. **One API layer, one place.** Every request goes through a single client module:
   base URL, JWT header, refresh-on-401, error decoding. No `http` call anywhere else.
2. **Branch on `code`, never on message text.** Every error is
   `{detail, error, code, field_errors?}`. `detail` is translated and will change;
   `code` is stable and is the contract. Map codes to UI in one file.
3. **One model per resource, decoded in one place.** Keep the wire shape in a DTO and map
   it to your UI model. When a field is added or a response gains a wrapper, you edit the
   mapper and nothing else.
4. **Feature flags per area.** Payments, trainer-side diet editing and challenge writes
   are still moving (§4). Put each behind a flag so a contract change is a flag flip, not
   a re-architecture.
5. **Do not persist media URLs.** They carry an expiring signature (§5).
6. **Send `Accept-Language`** on every request (`en` or `ar`) — and keep your own strings
   in the app: about a quarter of backend messages are still English-only (§6).

## 3. What changed since `API_CONTRACT.md` was frozen

These are live on `noti-system` and are final — build to them.

**Registration and verification.** A new account is now created **enabled but
unverified** (previously it was created inactive, which made a pending signup
indistinguishable from a suspended one — a missed first code left the address
permanently unusable).

- `POST /api/auth/login/` and `POST /api/auth/token/` return **400** with
  `{"requires_verification": true}` until the address is verified. Show the OTP screen on
  that flag; do not treat it as a wrong password.
- A suspended account gets `403` with `code: "account_disabled"` from
  `POST /api/auth/verify-otp/`. It is not the same state and must not be retried.
- `POST /api/auth/resend-otp/` always answers 200 (anti-enumeration).
- `POST /api/auth/verify-otp/` failures now return **one** message whatever the cause —
  wrong code, no code, unknown address. Do not try to distinguish them in the UI.

**Paywall.** A refusal for subscription reasons is `403` with
`code: "no_entitlement"` (no subscription, or it lapsed) or
`code: "feature_not_in_plan"` (subscribed, plan lacks it). Previously both were the
generic `permission_denied`, so the app could not tell "renew" from "not allowed".
Route `no_entitlement` to the renew screen.

**Subscribe.** `POST /api/subscription/v1/subscriptions/` now returns `id` and `status`
with the plan, so you no longer need a second call to `/subscriptions/current/` before
starting payment.

**Challenges.** `status` is derived from the dates (`upcoming` / `active` / `completed`)
and is authoritative in responses. A client may only write `cancelled` or `completed`;
sending `active` returns 400. Do not compute the phase client-side.

**Comments.** `post` and `parent_comment` are immutable after creation (a comment could
previously be PATCHed into another user's private post). A reply must name a parent on
the same post.

**AI chat feedback.** 👍/👎 now succeeds for every user; it previously errored for anyone
who had not opted into training-data consent, which is the default.

## 4. Still moving — flag these, do not design around them

| Area | Risk |
|---|---|
| Payments: renew, refund, pay-after-lapse | Fix branch unmerged and unreviewed. Double-tap and refund behaviour will change |
| Automatic payment reconciliation | Today a transfer activates nothing unless the client calls reconcile. A background reconciler is coming; the client may stop needing to poll |
| Trainer-side diet editing, meal interact | Fix branch unmerged; write paths and 409 semantics on superseded plans will change |
| Routine sessions, bulk complete | Fix branch unmerged |
| Notification preferences | The "all off" switch is not honoured yet — assume it will start working |

Read paths in all of these are safe. It is the writes and their error codes that may move.

## 5. Media, pagination, ordering

- **Media URLs are signed and expire** (`MEDIA_URL_TTL`, 24 h by default). Treat a URL as
  valid for the session you fetched it in; refresh the parent resource rather than caching
  the URL or building one yourself.
- **Pagination:** page-number, `PAGE_SIZE` 25, `?page_size=N` honoured up to 100.
  Feeds and comments use **cursor** pagination — follow `next`, never synthesise offsets.
- **Ordering is a total order everywhere** (`-created_at, -id`), so paging does not repeat
  or hide rows. Do not re-sort a paged list client-side.

## 6. Arabic

`Accept-Language: ar` switches the API's own messages, and the user's stored
`preferred_language` drives anything the backend renders asynchronously (emails, pushes,
daily advice). **169 of 723 backend strings have no Arabic translation yet** (102 of them
in diet), so an Arabic user will see some English from the API until that pass lands.
Plan the UI so a backend message is never the only text the user gets for a critical
action — keep your own localized copy for buttons, screen titles and validation.

Also: a food's `name` and a meal's `name` come back in both languages
(`name` / `name_ar`, plus `display_name` chosen by the request language).

## 7. Running the backend locally

```bash
docker compose up -d db            # Postgres on 5433
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

Seed a usable catalogue for diet screens:

```bash
.venv/bin/python manage.py load_food_catalogue --apply
.venv/bin/python manage.py seed_recipes
.venv/bin/python manage.py seed_food_units --apply
```

Push notifications stay disabled unless `FIREBASE_CREDENTIALS_JSON` or
`FIREBASE_CREDENTIALS_PATH` is set; everything else works without it.

## 8. What the backend still owes you before launch

In the owner's priority order: a real Fly deployment; rotation of the production secrets
(they were baked into every image built so far); 12 open HIGH findings; four unmerged fix
branches; the Arabic translation pass. None of these should change the contracts in §1 —
if one does, it lands as an additive change or a new version prefix, per
`API_CONTRACT.md`.
