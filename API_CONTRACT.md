# Training Platform — API Contract (v1, frozen)

**Frozen 2026-09-02.** Everything below is generated from the URL resolver and verified
against running requests, not from prose. After this freeze, changes are **additive** or
go behind a new version prefix — no path, field, or error code is renamed or removed.

Base URL: `https://<host>/api/`
Content type: `application/json` (uploads use `multipart/form-data`)
Languages: `en`, `ar` — send `Accept-Language`. **Messages are translated; codes are not.**

---

## 1. Authentication

RS256 JWT. Send `Authorization: Bearer <access>` on every authenticated request.

| | value |
|---|---|
| Access token lifetime | **60 minutes** |
| Refresh token lifetime | **7 days** |
| Rotation | on — every refresh returns a **new refresh token** |
| Old refresh after rotation | **blacklisted immediately** |
| Algorithm | RS256 (asymmetric) |

**The client must store the new refresh token returned by `/api/auth/token/refresh/`.**
Reusing the previous one fails with `token_not_valid` — rotation with blacklisting means
the old token is dead the moment it is exchanged.

### Registration flow

```
POST /api/auth/register/
     {username, email, password1, password2, phone_number, user_type, date_of_birth,
      preferred_language?}
  -> 201 {user}                       account created unverified, OTP emailed

POST /api/auth/verify-otp/   {email, otp_code}
  -> 200 {message, user, access, refresh}     account activated
  -> 400 code otp_invalid | otp_expired | too_many_attempts (+ retry_after_seconds, Retry-After)
  -> 403 code account_disabled

POST /api/auth/resend-otp/   {email}
  -> 200 (always — see anti-enumeration)
  -> 429 code throttled (+ retry_after_seconds, Retry-After) after 3 an hour
```

Note `password1`/`password2` — not `password`. `user_type` is one of
`client`, `trainer`, `agent`. `date_of_birth` (ISO `YYYY-MM-DD`) is optional on this
route (the v2 sign-up screen always sends it); when sent, the server refuses anyone younger
than 14 with `field_errors.date_of_birth[0].code` = `under_minimum_age` (also
`date_of_birth_in_future`, `date_of_birth_implausible`). The same rule applies wherever the
date or `age` is written (`user/update/`, `client/profile/`), and once set the date cannot
be removed (`date_of_birth_required`).
`preferred_language` (`ar` | `en`, optional, additive 2026-09-27) is saved on the account
and decides the language of every email and push it gets, starting with this sign-up
code; omitted, the account takes the language `Accept-Language` names (if served), else
Arabic. `fr` or any other value: `field_errors.preferred_language[0].code` =
`invalid_choice`. `user.preferred_language` in the 201 carries what was saved; change it on
`user/update/`. The sign-up, resend and reset codes are emailed by a background worker in
that saved language — whatever language the request itself was in — usually within
seconds, and retried for about seven minutes if the mail server fails; the answers of the
three routes are unchanged.
The v2 onboarding answers (`training_goal`, `training_place`, `training_days_per_week`,
`injury_areas`) and `onboarding_missing` are on those two profile endpoints; the trainer
matching codes (`trainer_goal_focus`, `trainer_injury_focus`, `trainer_training_places`)
are on `trainer/profile/`. See `users/serializers.py` (`OnboardingFieldsMixin`).

### Login

```
POST /api/auth/login/  {email, password}
  -> 200 {access, refresh, key, user}
```

`key` is a legacy field that now carries the **same value as `access`**; new clients
should read `access`. (It previously carried a DRF Token, which this API never accepted —
every request made with it returned 401.)

OTP is 6 digits, **valid 10 minutes**, stored as a keyed HMAC-SHA256 (never plaintext),
and locks after **5 failed attempts** per email (counted in Redis, not on the row).

### Password reset

```
POST /api/auth/forgot-password/         {email}                       -> 200 (always) | 429 throttled
POST /api/auth/forgot-password/verify/  {email, otp_code}             -> 200 {reset_token}
                                        | 400 otp_invalid | otp_expired | too_many_attempts
POST /api/auth/password/change/  {current_password, new_password}  (signed in; S2.8)
                                        -> 200 {message, access, refresh}: every other
                                           session ends, store the new pair
                                        | 400 current_password_wrong | password_unchanged
                                          | password_not_set (no password yet: use
                                            forgot-password; never a guess)
                                          | field_errors.new_password (as at sign-up)
                                        | 429 throttled (5 wrong in 15 min)
POST /api/auth/forgot-password/confirm/ {reset_token, new_password}   -> 200
                                        | 400 field_errors.new_password: password_too_short |
                                          password_too_common | password_entirely_numeric (as at sign-up)
```

The `reset_token` is a single-use opaque string, **valid 15 minutes**, and is never stored in
readable form server-side — only a keyed hash of it is. It cannot be recovered from a
database dump.

**On successful reset every outstanding refresh token for that user is blacklisted.** The
app must treat a post-reset `401 token_not_valid` as "log in again", not as an error.

### Anti-enumeration

`forgot-password` and `resend-otp` always return `200` with the same message whether or
not the email exists. Do not use them to check account existence — the response is
identical by design. Their hourly limit (429 `throttled`) counts every address alike, so it
tells nothing either; so does `otp_invalid`, the same answer for a wrong code, no pending
code and an unknown address. Addresses are matched lower-cased (then as typed) on every
auth route, sign-in included.

---

## 2. Error contract

**Every** error response — including `429` from the rate limiter — carries these keys:

```json
{
  "detail": "Enter a valid email address.",
  "error":  "Enter a valid email address.",
  "code":   "validation_error",
  "field_errors": {
    "email":    [{"message": "Enter a valid email address.", "code": "invalid"}],
    "password": [{"message": "This field is required.",      "code": "required"}]
  }
}
```

**Branch on `code`, never on `detail`.** `detail` is translated into the user's language;
`code` never is. A client that switches on message text works in English and silently
stops working in Arabic.

`field_errors` is present only on `validation_error`. Original DRF field keys remain at
the top level for compatibility.

| HTTP | `code` | meaning for the app |
|---|---|---|
| 400 | `validation_error` | show `field_errors` inline; `detail` is the first message |
| 400 | `bad_request` | malformed request |
| 401 | `not_authenticated` | no token sent — go to login |
| 401 | `token_not_valid` | expired/rotated/blacklisted — try refresh, then login |
| 403 | `permission_denied` | wrong role, or subscription lacks the feature |
| 404 | `not_found` | object missing or not visible to this user |
| 405 | `method_not_allowed` | client bug |
| 429 | `rate_limited` | back off; honour the `Retry-After` header |
| 500 | `server_error` | retry with backoff; do not loop |
| 503 | `upload_busy` | an image upload arrived while the server was already decoding as many images as its memory holds; NOTHING was stored or judged. Send the same file again after `Retry-After` seconds (also `retry_after_seconds` in the body). Any route that takes a photo can answer it (added 2026-09-27) |

**These are the status DEFAULTS, not what a refusal answers.** Since the owner's decision
of 2026-09-24 every refusal a view writes names its OWN `code` (listed in that view's
docstring and in §7): `bad_request`, `permission_denied` and `conflict` are what the
envelope fills in only where nothing more specific exists — a framework refusal, or a
code that is new to the app. Branch on the specific code; treat one you do not know as
its status default (§6). A gate test fails any refusal written in a view without its own
`code` (`tests/test_every_refusal_names_its_code.py`).

`403` is also returned when the user's **subscription plan** does not include a feature
(diet, AI advice). Same code — check the user's plan before assuming an auth problem.

---

## 3. Pagination

**One shape, everywhere.** Every list endpoint returns:

```json
{"count": 143, "next": "…?page=2", "previous": null, "results": [ … ]}
```

- Default page size **25**; override with `?page_size=N`, capped at **100**
- `next`/`previous` are absolute URLs or `null` — follow them rather than building pages
- A few high-churn feeds use **cursor** pagination: same keys, but `count` is absent and
  `next` must be followed (page numbers do not apply). Treat a missing `count` as
  "unknown total", not as zero.

---

## 4. Push notifications (FCM)

Register the device token, then route on `data.type`.

**What differs by build (Road to 10 R1, 2026-11-24).** A notice is written for the account
and read or pushed on every device it has; three notices read differently, or not at all,
on a build whose policy flags forbid what they say (`notifications/build_gate.py`):

| `data.type` | needs | a build without it (today: play, ios) |
|---|---|---|
| `checkout_expired` | `can_purchase_platform_sub` | never sees it: no push, not in the inbox list, not in `unread_count` |
| `subscription_expired` | `can_purchase_platform_sub` | reads «خطتك الغذائية وبرنامجك التدريبي محفوظان ويمكنك الاطلاع عليهما.» (no "needs an active subscription") |
| `coaching_payment_due` | `can_pay_coaching_in_app` | (ios) reads «تدريبك مع %(name)s متوقف مؤقتاً. لا يزال بإمكانك الاطلاع على برامجك.» (no "until the next month is paid") |

The inbox (`GET /api/social/notifications/`) chooses at READ time from the request's
`X-App-Channel` and `X-App-Version`; a push chooses per DEVICE from the build its token
recorded at `POST /api/auth/device-token/` (send both headers there too). A request, or a
token, with no known channel is the strictest build (all three gated); a token registered
as `platform: ios` is the ios build. `data` never changes. apk and web read and receive
every notice exactly as before, and every record of money that already moved (a receipt, a
fee, a top-up with its amount, a sale, a withdrawal) reads the same on every build.

**`data.type` is always exactly the `event_type`** — the same values
`GET /api/notifications/preferences/event_types/` returns. Every value is a string (FCM requires it),
so parse ids client-side.

| `data.type` | additional `data` keys |
|---|---|
| `post_liked` | `post_id` |
| `comment_created` | `post_id`, `comment_id` |
| `user_followed` | `follower_id` |
| `achievement_awarded` | `achievement_id` |
| `challenge_progress` | `challenge_id` |
| `trainer_assignment_request` | `trainer_id`, `request_id` — the invitation; answer it with `POST /api/auth/client/respond-to-invite/` `{request_id, action}` (`request_id` since Road to 10 S3.1; `"0"` on a notice queued before) |
| `trainer_unassignment` | `trainer_id` |
| `client_request_received` | `client_id`, `request_id` — to the trainer; answer it with `POST /api/auth/trainer/respond-to-request/` `{request_id, action}` (`request_id` since Road to 10 S3.1; `"0"` on a notice queued before) |
| `client_request_approved` | `trainer_id`, `client_id` — the coaching's two people. Sent both ways: to the CLIENT when the trainer accepts the client's request, to the TRAINER when the client accepts the trainer's invitation; the text names whoever answered. On the invitation path `trainer_id` was the client's id until the phase-3 review of Road to 10 S3.1 (`client_id` since then; `"0"` on a notice queued before) |
| `client_request_rejected` | `trainer_id`, `client_id` — as `client_request_approved`, for a no |
| `client_request_cancelled` | `client_id` — to the trainer; the request is gone, so there is no request to open |
| `client_unassigned_trainer` | `client_id` — to the trainer; the client left |
| `routine_assignment` | `related_object_id`, `related_object_type`, `routine_id` (since Road to 10 S3.1; the same id as `related_object_id`) |
| `session_completed` | `related_object_id`, `related_object_type`, `session_id`, `client_id`, `routine_id` — sent to the client and to each trainer with an approved relation; the trainer opens the review with `GET /api/routine/workout-sessions/{session_id}/?expand=sets` (the three named ids since Road to 10 S3.1) |
| `session_reminder` | `reason` (`scheduled` \| `drift`), plus `routine_id`+`day` when scheduled, or `days_since_last_workout` when drift |
| `progress_milestone` | `milestone` (e.g. `streak-7`, `sessions-100`) |
| `custom` | `message_id`, `trainer_id` — a message from the user's trainer |
| `chat_message` | `conversation_id`, `message_id` — a message in the trainer–client chat. Sent only when the recipient has opened that conversation since the last `chat_message` about it, so a burst of messages is one push |
| `coaching_renewed` | `trainer_id` — a new month of coaching was charged from the wallet |
| `coaching_renewal_soon` | `trainer_id` — the paid month ends in a few days |
| `coaching_ending_soon` | `trainer_id` — the paid month ends in a few days and the coaching ends with it: the client asked not to renew (`POST /api/v1/coaching/renewal/` `renew: false`, Road to 10 S2.7). Sent instead of `coaching_renewal_soon`, once per month; nothing is charged |
| `coaching_payment_due` | `trainer_id`, `status` (`frozen`) — the month ended unpaid, so the coaching is paused; repeated on each reminder step while it stays paused |
| `coaching_ended` | `user_id` — the other person; `client_id`, `trainer_id` — both sides by role (since Road to 10 S3.1; `"0"` on a notice queued before). The grace period ran out unpaid; sent to both |
| `coaching_finished` | `user_id` — the other person; `client_id`, `trainer_id` — both sides by role (since Road to 10 S3.1). Coaching ended for a reason other than money; sent to both |
| `coaching_fee_held` | `period_id`, `month`, `role` (`client` / `trainer`: the recipient's side), `user_id` — the other person. Month 1 was charged into escrow when the trainer accepted; sent to both. The fact only, no amount, on every build |
| `coaching_fee_released` | `period_id`, `month`, `role`, `user_id` — a month's fee left escrow for the trainer. Month 1 (the first program arrived): sent to both. A renewal: to the trainer only (the client's notice is `coaching_renewed`). The trainer hears it when their share lands: a share owed for a stale exchange rate is announced when it is paid |
| `coaching_fee_refunded` | `period_id`, `month`, `role`, `user_id` — a held month went back to the client's wallet (declined, left or ended before the first program); sent to both |
| `subscription_expiring` | `subscription_id`, `ends_at` (ISO 8601) |
| `subscription_expired` | `subscription_id`, `ended_at` (ISO 8601) |
| `checkout_expired` | `payment_id` — the checkout was not completed in time |
| `payment_needs_review` | `payment_id` — a transfer arrived for it that is not exactly what it asked for; a person is checking it (B-051) |
| `payment_rejected` | `payment_id` — the transfer held for it could not be accepted; it goes back to its sender by hand (B-051) |
| `payment_confirmed` | `payment_id`, `invoice_number` — the payment cleared (whoever confirmed it: the poller, a webhook, "check my payment", an operator) and its receipt is ready: `GET /api/subscription/v1/payments/{payment_id}/receipt/`. The fact only — no amount, button or link — on every build |
| `trainer_verification_approved` | `application_id` — an operator accepted the trainer's documents; the account is verified |
| `trainer_verification_rejected` | `application_id`, `reason` — the application needs changes (or a verification was revoked); `reason` is the operator's words, also in the body |
| `withdrawal_requested` | `withdrawal_id`, `number` — the trainer's withdrawal was received; the money is held for it |
| `withdrawal_paid` | `withdrawal_id`, `number` — the operator confirmed it arrived |
| `withdrawal_failed` | `withdrawal_id`, `number`, `reason` — it could not be paid and the money is back in the balance; `reason` is the operator's words, also in the body |
| `routine_product_approved` | `product_id` — an operator published the trainer's program (plan step 10). Like every routine-product notice: the fact only, no price, button or link, on every build |
| `routine_product_rejected` | `product_id`, `reason` — the program went back to draft; `reason` is the operator's words, also in the body |
| `routine_product_sold` | `product_id`, `purchase_id` — to the trainer; the buyer is never named |
| `routine_product_purchased` | `product_id`, `purchase_id`, `routine_id` — to the buyer; `routine_id` is their own copy, or `""` (empty, never null: push data is strings) when no copy exists any more — then open the program by `product_id` |
| `routine_product_refunded` | `product_id`, `purchase_id` — to the buyer: the price is back in the wallet and the copy is gone |
| `routine_product_sale_refunded` | `product_id`, `purchase_id` — to the trainer: a sale was refunded and their share went back |
| `wallet_topped_up` | `reference_id`, `amount`, `currency` — an agent credited the client's wallet with cash; `reference_id` opens the row (`GET wallet/transactions/{reference_id}/`). Every build: a cash top-up is WALLET_MOVE, and the text names the shop, never the amount (BACKEND_BUGS B-115) |
| `personal_record` | `exercise_id`, `set_log_id` — the set that beat the previous best |
| `weekly_recap` | `week` (the ISO week key), `missed_sets` |
| `trainer_weekly_digest` | `week` (the ISO week key), `attention` — how many clients need a look |

`title`/`body` arrive in the FCM `notification` block, already rendered in the
recipient's language.

Every registered event type is emitted; there are no placeholders to skip.

This table is **gate-checked**: `tests/test_side_bugs_round2.py` diffs it against
`notifications/channels/fcm.py` `EVENT_CLASS_REGISTRY`, the same way the route table
below is diffed against the URLconf. It had drifted by eleven event types — every
coaching notice, both subscription notices, the checkout notice and all three progress
notices — so an app built from this document ignored pushes the server sends. A new
`event_type` goes in this table in the same commit as its registry entry.

**Reminder scheduling.** `session_reminder` fires at most once a day, and only for a
client with a program they can start (an assigned routine whose coaching is not paused or
over; nothing for someone with no program, or a trainer), in the user's own
evening: the sweep runs hourly and sends only to users whose local hour — resolved
through `preferred_timezone` — equals `workout_reminder_hour`. Both fields are on the
profile and are written with `POST /api/auth/user/update/` (a client may also use
`POST /api/auth/client/profile/`). Note POST, not PATCH, and note the paths: this
section used to name a profile route that has never existed, so a client built from
this document could not set the reminder hour at all.
`workout_reminder_hour` is 0-23 and
defaults to 18. To turn reminders off entirely, disable the `session_reminder` event
via the notification preferences endpoint rather than picking an unreachable hour.

Notification history is read at `GET /api/social/notifications/` (cursor-paginated).

Some notices also send an email; which ones, and how preferences stop it, is under
"Notifications" in section 7 (`email_event_types`).

---

## 5. Rate limits

| caller | limit |
|---|---|
| anonymous (per IP) | 2000 / hour |
| client | 500 / hour |
| trainer | 1000 / hour |
| admin | 5000 / hour |

Endpoint-specific, identity-scoped limits sit on top and are the ones users actually hit:

| endpoint | limit |
|---|---|
| `POST /api/auth/resend-otp/` | 3 / hour per email+IP |
| `POST /api/auth/forgot-password/` | 3 / hour per email |
| OTP verification | 5 wrong codes per email and purpose, then locked 15 minutes (400 `too_many_attempts`) |
| `POST /legal/delete-account/` (a web page, not JSON) | 5 / hour per network, then the page again with `429` and `Retry-After` |

Every limit answer carries `retry_after_seconds` and `Retry-After`: the seconds really left,
not the window's length. The global limiter's `429` has `code: "rate_limited"`; the
per-address limits above have `code: "throttled"`.

---

## 6. Versioning & breaking-change policy

- All diet endpoints are under `v1/`. Other apps are unversioned at the path level and
  are frozen as they stand.
- **Additive changes** (new endpoint, new optional field, new `code` value) may ship at
  any time. Clients must ignore unknown JSON fields and treat an unrecognised `code` as
  its HTTP-status default.
- **Breaking changes** (removing or renaming a path or field, changing a type, changing
  an existing `code`) require a new prefix (`v2/`); `v1` keeps working.
- **A refusal that answered only its status default** — the `code` the error envelope
  fills in when a view names none (`bad_request`, `not_found`, `conflict`,
  `permission_denied`; §2) — may gain a code of its own. That is a new `code` value, not
  a changed one: a client that follows the rule above reads the new value as exactly the
  default it replaces. A code a view NAMED is never changed. CONFIRMED by the owner on
  2026-09-24, and applied to every refusal (decision 9); first recorded 2026-09-23, when
  the app session asked for codes on eleven such refusals — BACKEND_BUGS B-054..B-061.)
- **One refusal, one code (BACKEND_BUGS B-092, 2026-09-24).** Decision 9's builders minted
  79 codes after the last pushed commit (`4cbb522`). Each was checked against the codes
  that existed at `4cbb522`; the four that named a refusal an OLDER code already named
  were moved onto the older code before anything shipped. No code the app could receive at
  `4cbb522` was renamed.

  | Minted 2026-09-24 (never shipped) | Now | Where | Why the older one |
  |---|---|---|---|
  | `trainers_only` | `not_a_trainer` | every `/api/auth/trainer/*` route (403) | the routine and mediakit trainer routes' code for "the caller is not a trainer" |
  | `already_your_client` | `already_with_this_trainer` | `POST /api/auth/trainer/assign-client/` (400) | `client/request-trainer/`'s code for the same state, an approved pair; the sibling `request_already_pending` was already shared by both sides |
  | `not_trainer` | `not_a_trainer` | the diet trainer routes (`diet/v1/trainer/templates/`, `diet-plans/`, `meals/`; 403) | the users, routine and mediakit trainer routes' code for "the caller is not a trainer"; at `4cbb522` these routes answered a code-less 403 before the service's `not_trainer` could be reached, so the app never received it (the review of B-092) |
  | `field_required` (+ `field`) | `missing_fields` (+ `fields`, `field` kept) | `POST /api/routine/exercises/create-with-image/` (400) | the diet and subscription routes' code for a missing required field |

  Checked and kept, because the older code names a different fact: `client_not_found`
  (404 on the routine assign routes: the id names no CLIENT, perhaps an existing trainer;
  `user_not_found` means no active account at all), `user_not_accessible` (covers a client
  reading someone else, where `not_your_client` is the trainer-side case only),
  `no_pending_request` (400, no id sent; `request_not_found` is a 404 for an id),
  `no_assigned_trainer` (`no_relation` is chat's "not a pair"), `trainers_cannot_generate`
  (admins may generate; it is not `clients_only`), `target_not_a_client` (the TARGET of a
  top-up; `clients_only` / `not_client` are about the caller), `daily_limit_exceeded` /
  `monthly_limit_exceeded` (a money cap per currency and period; `quota_exhausted` is the AI
  allowance), `subscription_exists` (`subscription_active` is running cover), the object-
  specific `*_not_yours` / `not_owner` codes, and the `<field>_required` codes, which follow
  the older `trainer_id_required` / `request_id_required` / `token_required` pattern.
  Also kept: wallet's 422 `idempotency_key_reused` beside the 409 `idempotency_conflict`
  of the set, workout and chat writes. Both are about an id, but they are different
  facts with opposite recoveries: `idempotency_conflict` says the id is the OTHER
  person's record, so the write never happened and a new id is the fix;
  `idempotency_key_reused` says the caller's OWN key was already spent on a different
  request of theirs, which may have moved money, so sending again under a new key could
  move it twice. (It was briefly unified onto `idempotency_conflict` and restored by the
  review, before anything shipped.) One older pair remains because the app could
  receive both at `4cbb522`: `not_client` (diet service) beside `clients_only`.
- Adding a new push `event_type` is additive — the app must ignore `data.type` values it
  does not recognise rather than crash.

---

## 7. Endpoints

### Achievements

`GET leaderboard/?limit=` (1..50, default 10) orders ties on points by user id, so a
place is stable between requests. An account the caller has blocked, or that blocked
the caller, is left out of `leaderboard` after the ranks are counted — the other rows
keep their true `rank`, the list can be shorter than `limit`, the same rule as a
challenge leaderboard.

The Challenge Winner and Champion badges (`challenge_wins`) count a first place only in
a challenge that is OVER — closed as completed, or past its end date and not cancelled
— and the check runs when the challenge closes. Being first while it runs earns nothing.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/achievements/` | auth | `AchievementViewSet` |
| `GET` | `/api/achievements/categories/` | auth | `AchievementCategoriesView` |
| `POST` | `/api/achievements/check/` | auth | `AchievementViewSet` |
| `GET` | `/api/achievements/leaderboard/` | auth | `AchievementViewSet` |
| `GET` | `/api/achievements/my/` | auth | `AchievementViewSet` |
| `GET` | `/api/achievements/progress/` | auth | `AchievementViewSet` |
| `GET` | `/api/achievements/{id}/` | auth | `AchievementViewSet` |
| `POST` | `/api/achievements/{id}/feature/` | auth | `AchievementViewSet` |

### AI Assistant

The assistant answers over the socket `ws://host/ws/ai/chat/?token=<JWT>`; these routes
are the history and the housekeeping around it. Every socket `error` frame and every
REST refusal carries a `code` — `ai_assistant/consumers.py` lists the socket's full set
in its class docstring. Two worth naming here:

| `code` | Where | When |
|---|---|---|
| `message_not_found` | `POST feedback/` (404) and the socket's `feedback` frame | No assistant message of yours by that id |
| `ai_consent_required` | every `/api/ai/` route except `data/` (`GET` and `DELETE`) and `training-consent/`, and the socket | The AI coach has not been allowed to read this person's data |

A turn that ends in an `error` frame is **not** charged against the daily message
allowance; each turn closes with `{"type": "rate_limit", "remaining", "resets_at"}`
carrying what is actually left. `resets_at` is the next midnight in the server's time
zone as ISO 8601 **with its offset** (`2026-09-24T00:00:00+03:00`), the same instant
`/api/subscription/v1/usage/today/` gives for `ai_messages`; it used to carry no offset.

`tool_used.display` and `complete.tools_used[].display` are translated into the
reader's language. Branch on `name`; `display` is for showing.

👍/👎 is stored on the reply for every user, over REST and over the socket alike, and
reads back as `feedback` (`positive` | `negative` | null) on each message of
`sessions/{session_id}/messages/`. `POST feedback/` answers
`{message, feedback, updated_records}`; `updated_records` counts training-data rows
only, which exist only for a user who opted in, so 0 is normal.
**Changing and clearing** (the owner, F12, 2026-09-24): sending again replaces the
rating, and `"feedback": null` clears it, on `POST feedback/` and on the socket's
`feedback` frame alike; the training copy follows. The key is required: a body or a
frame without it is refused (`400 validation_error` / `invalid_input`), never read as a
clear. **The coach uses the ratings**: every later answer is written with a short,
hidden note of the person's OWN recent ratings (the newest five 👎 and five 👍 of the
last 90 days, each reply cut to 160 characters). Nothing from anyone else; no new
field, no new route.

A `status` frame is `{"type": "status", "content", "code"}`. Render the `code`; the
translated `content` is there for a build that predates it. The set, in the order a
turn produces them: `connected` (on connect), `getting_ready`, then one per pass of the
tool-calling loop — `thinking`, `checking_data`, `analyzing`, `looking_deeper`,
`almost_done` (the last repeats if the loop runs longer).

**The socket declares its build** (spec §4, §10; 2026-09-27). Send `X-App-Channel` and
`X-App-Version` on the handshake as headers, exactly as on REST; a client that cannot
set handshake headers sends `?app_channel=ios&app_version=1.4.0` beside `?token=`
instead (a header that is present wins). Read once at connect with policy's parser. A
socket that declares no channel, or one policy does not know, gets what a REST caller
declaring none gets — the strictest flags — never a refusal. **What differs by build:**
on a build that may not sell plans or routine products (today `play`, `ios` and no
declaration; `ai_assistant/store_safe.py payment_talk_blocked`), a question about
paying, prices, subscribing or buying — Arabic, Levantine, Arabizi or English — is
answered with one fixed sentence that names no price, place or link, WITHOUT calling
the model, in the ordinary frames (`token`, `complete`, `rate_limit`; saved and charged
like any turn); every other question reaches the model with the store rule in its
instructions. On `apk` and `web` nothing changes. The model's reply is never edited on
any build. `sessions/{session_id}/messages/` follows the same rule on the build its
request declares: on a store build, a stored reply to a payment question reads as that
fixed sentence; everything else reads as written.

**`medical_disclaimer`** (spec §9 B4, additive): one sentence in the reader's language
("The AI coach can make mistakes. It is not a substitute for your trainer or your
doctor." — the canvas's own words), on the `connected` status, on every `complete`, and
on every `assistant` message of `sessions/{session_id}/messages/` (null on `user`
messages). The same on every build.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET, DELETE` | `/api/ai/data/` | auth | `GDPRDataDeleteView` |
| `POST` | `/api/ai/feedback/` | auth, HasAIAdviceAccess | `FeedbackView` |
| `GET` | `/api/ai/sessions/` | auth, HasAIAdviceAccess | `ChatSessionListView` |
| `GET` | `/api/ai/sessions/{session_id}/` | auth, HasAIAdviceAccess | `ChatSessionDetailView` |
| `GET` | `/api/ai/sessions/{session_id}/messages/` | auth, HasAIAdviceAccess | `ChatMessageListView` |
| `GET, POST` | `/api/ai/training-consent/` | auth | `AITrainingConsentView` |

### Analytics

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/analytics/` | auth | `APIRootView` |
| `GET, POST` | `/api/analytics/activities/` | auth | `UserActivityViewSet` |
| `GET` | `/api/analytics/activities/activity_summary/` | auth | `UserActivityViewSet` |
| `GET` | `/api/analytics/activities/recent_activities/` | auth | `UserActivityViewSet` |
| `GET` | `/api/analytics/activities/{id}/` | auth | `UserActivityViewSet` |
| `GET, POST` | `/api/analytics/dashboard/` | auth | `AnalyticsDashboardViewSet` |
| `GET` | `/api/analytics/dashboard/platform_stats/` | auth | `AnalyticsDashboardViewSet` |
| `GET` | `/api/analytics/dashboard/user_overview/` | auth | `AnalyticsDashboardViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/analytics/dashboard/{id}/` | auth | `AnalyticsDashboardViewSet` |
| `GET, POST` | `/api/analytics/goals/` | auth | `UserGoalViewSet` |
| `GET` | `/api/analytics/goals/active_goals/` | auth | `UserGoalViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/analytics/goals/{id}/` | auth | `UserGoalViewSet` |
| `POST` | `/api/analytics/goals/{id}/update_progress/` | auth | `UserGoalViewSet` |
| `GET, POST` | `/api/analytics/metrics/` | auth | `PerformanceMetricViewSet` |
| `GET` | `/api/analytics/metrics/current_metrics/` | auth | `PerformanceMetricViewSet` |
| `GET` | `/api/analytics/metrics/metric_trends/` | auth | `PerformanceMetricViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/analytics/metrics/{id}/` | auth | `PerformanceMetricViewSet` |
| `GET, POST` | `/api/analytics/sessions/` | auth | `UserSessionViewSet` |
| `POST` | `/api/analytics/sessions/end_session/` | auth | `UserSessionViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/analytics/sessions/{id}/` | auth | `UserSessionViewSet` |

**Body measurements (Road to 10 S2.11, the owner's D18).** `POST /api/analytics/metrics/`
`{metric_type, value, unit?, notes?}` takes four circumferences beside `weight`, in
CENTIMETRES, each within its ruler: `chest` 60-160, `arm` 20-60, `waist` 50-160, `thigh`
30-100. The row answers `unit: "cm"` whatever was sent. No route is new. Refusals, all 400
`validation_error` with `field_errors`: `value` field code `invalid` (outside the range);
`unit` field code `unit_not_cm` (a unit other than `cm` on one of the four);
`metric_type` field code `invalid_choice`. `body_fat` is not part of the launch.

`GET /api/analytics/metrics/?latest_for=weight,chest,arm,waist,thigh` answers the CALLER's
newest row of each named type, in the list's own shape (`{count, next, previous,
results}`, newest first; a type never logged has no row). 400 `bad_metric_type`
(+ `field`: `latest_for`) for a name that is not a metric type or an empty list.
`current_metrics/` and `metric_trends/?metric_type=` answer the new types as they answer
weight. Health data: the person's own, read by nobody else, in their export, deleted with
the account. Nothing differs by build.

### Authentication & Users

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/auth/` | auth | `APIRootView` |
| `GET` | `/api/auth/client/available-trainers/` | auth | `AvailableTrainersView` |
| `POST` | `/api/auth/client/cancel-trainer-request/` | auth | `ClientCancelTrainerRequestView` |
| `GET, POST` | `/api/auth/client/profile/` | auth | `ClientProfileView` |
| `GET` | `/api/auth/client/request-status/` | auth | `ClientRequestStatusView` |
| `POST` | `/api/auth/client/respond-to-invite/` | auth | `ClientRespondToTrainerInviteView` |
| `POST` | `/api/auth/client/request-trainer/` | auth | `ClientRequestTrainerView` |
| `POST` | `/api/auth/client/unassign-trainer/` | auth | `ClientUnassignTrainerView` |
| `GET` | `/api/auth/client/trainer-invites/` | auth | `ClientPendingTrainerInvitesView` |
| `DELETE, POST` | `/api/auth/device-token/` | auth | `FCMTokenView` |
| `POST` | `/api/auth/password/change/` | auth | `PasswordChangeView` |
| `POST` | `/api/auth/forgot-password/` | public | `PasswordResetRequestView` |
| `POST` | `/api/auth/forgot-password/confirm/` | public | `PasswordResetConfirmView` |
| `POST` | `/api/auth/forgot-password/verify/` | public | `PasswordResetVerifyView` |
| `GET` | `/api/auth/health/` | public | `HealthCheckView` |
| `GET` | `/api/auth/health/background/` | public | `BackgroundHealthView` |
| `POST` | `/api/auth/login/` | public | `CustomLoginView` |
| `POST` | `/api/auth/register/` | public | `CustomRegisterView` |
| `POST` | `/api/auth/resend-otp/` | public | `ResendOTPView` |
| `POST` | `/api/auth/token/` | *auth | `CustomTokenObtainPairView` |
| `POST` | `/api/auth/token/logout/` | auth | `JWTAuthLogoutView` |
| `POST` | `/api/auth/token/refresh/` | *auth | `TokenRefreshView` |
| `POST` | `/api/auth/trainer/assign-client/` | auth | `AssignClientView` |
| `GET` | `/api/auth/trainer/client-profile/` | IsTrainerOfApprovedClient | `ClientProfileViewSet` |
| `GET` | `/api/auth/trainer/client-profile/{id}/` | IsTrainerOfApprovedClient | `ClientProfileViewSet` |
| `GET` | `/api/auth/trainer/clients/` | auth | `TrainerClientsView` |
| `GET` | `/api/auth/trainer/pending-requests/` | auth | `TrainerPendingRequestsView` |
| `GET, POST` | `/api/auth/trainer/profile/` | auth | `TrainerProfileView` |
| `POST` | `/api/auth/trainer/respond-to-request/` | auth | `TrainerRespondToRequestView` |
| `POST` | `/api/auth/trainer/unassign-client/` | auth | `UnassignClientView` |
| `GET` | `/api/auth/trainers/public/` | public | `PublicTrainersListView` |
| `GET` | `/api/auth/trainers/stats/` | auth | `PublicTrainerClientStatsView` |
| `GET` | `/api/auth/user/details/` | auth | `UserDetailsView` |
| `DELETE, POST` | `/api/auth/user/profile-picture/` | auth | `ProfilePictureUploadView` |
| `GET, POST` | `/api/auth/user/update/` | auth | `UpdateUserDetailsView` |
| `POST` | `/api/auth/verify-otp/` | public | `OTPVerificationView` |

A trainer's email address is never sent to a client: `email` on
`client/available-trainers/` and `trainer_email` on `client/request-status/` are always
`null`, kept for builds that read them. `trainer/pending-requests/` gives the full row
(email, picture, age, gender, goals, activity level, note) only for a request the client
sent, and not once either of the two has blocked the other — that row keeps the short
shape (`request_id`, `client_id`, `client_name`, `client_username`, `initiated_by`,
`created_at`) so it can still be declined.

What wiring the trainer shell found (BACKEND_BUGS B-089, B-093, 2026-09-24; additive):

- `trainer/pending-requests/`: every row gains the QUOTE — `agreed_rate` /
  `agreed_currency` (the price quoted with the request, in the trainer's currency when
  the operator's rate converts it), `trainer_net` (that price less the platform's
  commission, split by the code that splits a paid month) and `commission_rate` (a
  fraction, `0.2`); all four null on a build that may not show a coaching fee, the first
  three when no price was quoted. The full row (a request the client sent, no block)
  gains `training_goal`, `training_place`, `training_days_per_week` (as the profile
  payloads send them: a code, or `""` / null while unanswered). A trainer's own
  invitation row gains `note`, what they wrote with it. Health notes, injury areas and
  training history are never on a pending row. The row is one function,
  `users.trainer_requests.pending_request_row`.
- `trainer/assign-client/` takes `message` (or `note`), optional: the invitation's note,
  the client's request-note rule (text, stripped, at most 500 characters; 400
  `invalid_note` / `note_too_long`). The client reads it as `note` on
  `client/trainer-invites/` and as `trainer_note` on `client/request-status/`, which also
  gains `initiated_by`; `note` there stays the client's own (`""` on an invitation). A
  note is `""` while either of the two has blocked the other.

`client/request-trainer/` answers 404
`not_found` for no such trainer and for a `trainer_id` that is not a number, and both
unassign routes answer 409 `refund_failed` when a held month cannot be returned.

`register/` (`username`), `user/update/`, `trainer/profile/` and `client/profile/` (the
names, and a trainer's bio, specialisations and certifications) answer 400
`content_not_allowed` when the text holds a term on the banned-terms list, with the
field in `field_errors` and nothing saved; see "The banned-terms filter" under Safety
(2026-09-27).

**`user/details/` names the role** (BACKEND_BUGS B-121, 2026-09-24; additive). The answer
gains `user_type` (`client` | `trainer` | `agent` | `admin`) and `media` — the person's
media map in the trainer cards' shape, `{slot: {url_1x, url_2x, blur, focal, brightness,
alt_ar, alt_en}}`: a trainer's filled kit slots, `{}` for every other role.
`profile_picture` stays a string or null and is now ABSOLUTE, as every other endpoint
answers a person's picture (it was the bare `/media/...` path). The view's docstring is
its contract: `Authorization: Bearer`, the fields, 401 `not_authenticated` /
`token_not_valid`.

### Chat

Messages between a trainer and the client they coach: text, (B-091, 2026-09-24) a
photo, a voice note or a video with an optional caption, and (2026-09-27) a trainer's
**program card**. Exactly one conversation per pair. Nothing here differs by build.

Reading and writing are decided separately (the owner, 2026-09-19). Being in the
conversation is what lets you READ it, for ever; writing asks
`chat.permissions.may_message`, and a coaching that is frozen, unpaid or ended refuses
the send with the same `code` (and `trainer_id`) every training write answers with. The
conversation row carries `can_send` and `refusal` so the app can explain a closed
composer without posting first. A conversation that is not the caller's is **404**, never
403.

Every message carries `kind` — `text`, `image`, `voice`, `video` or `program` — on REST
and on the socket's `message.new` (the same serializer); switch on it, and draw an
unknown one as a placeholder rather than guessing from which fields are present
(BACKEND_BUGS B-042). Every message also carries, additively, `media` (null on text and
on a program card; `{url, duration_seconds, width, height, poster_url}` otherwise),
`exercise` (`{id, name}` on a video that names its move, else null) and `program` (the
card below, else null). `media.url` and `media.poster_url` are PRIVATE: signed, expiring
(an hour on GCS), and handed out only in answers the two people in the thread receive —
use them, never store them. A message taken back serves `media: null` and
`program: null`. The conversation list's `last_message` carries `kind` and never the
media or the card; the home card's `trainer_message` carries `kind`, and for a VOICE note
only it also carries `audio_url`, `duration_seconds` and `state` (2026-09-27), signed the
same way and served only to the client of the thread. For a PROGRAM card only, the home
card carries `program` (2026-09-28, BACKEND_BUGS B-175): the thread's own `program` for
the same message, by the same rule (null when taken back, deleted, or no longer readable
by the client).

**Video poster frames** (2026-09-27). `media.poster_url` is a video's poster: one JPEG
frame near its start (at most 720 px a side, no metadata), cut AFTER the send by a
Celery worker with ffmpeg (`chat.tasks.cut_video_poster`) — never on the request — and
stored privately beside the video under the same signed-URL rules. It is null on a
photo and a voice note, null in the send's own answer and in `message.new`, and filled
on every read once the worker has stored it; the worker then sends `message.updated`
(the whole message, as REST serves it) on the socket. Where the worker has no ffmpeg
(logged once per worker process) or cannot read the video it stays null: draw a
placeholder. The files go with the message (a conversation's delete, an erasure).

**Program cards** (the trainer only; «حدّث برنامجك · برنامج القوة · يوم الدفع»,
`screens/05-chat-utility/Chat.png`) are sent on the text route — and over the socket's
`message.send` — with `{"kind": "program", "routine_id" | "template_id", "day"?,
"body"?, "client_uuid"?}`: a routine the trainer wrote that this client is on (their
private copy), or a template the trainer owns, which names the client's routine written
from it (the newest active one). The card: `program: {routine_id, template_id, name,
day, day_name, exercises, changes}` — open it as `GET /api/routine/routines/{routine_id}/`
and its `day`; `name` and `day_name` are the routine's now, in the reader's language;
`exercises` (rows on that day) and `changes` (`null` on the first card for a routine,
else `{since, added, edited, removed | null}` since the previous card for it in this
thread) are what the card said when it was sent. A reader gets `program` only while they
may READ that routine — the rule of the routine routes (`routine.permissions.
readable_routines`): so a client whose coaching is frozen, unpaid or ended still sees
and opens it (reading is not training), and a card whose routine was taken off the
client or deleted stays in the thread with `program: null`. The same replay, pair rule
and fan-out as text; the push reads the caption, or «حدّث برنامجك · <routine>» in the
client's language. Refusals, in the order decided: `program_trainer_only` (403),
`validation_error`, `program_target_required`, `unknown_program` (another trainer's
routine or template answers the same), `program_not_for_this_client`,
`day_out_of_range` (400s), then the send's 403s. A `kind` that is neither `text` nor
`program` on this route is 400 `invalid_kind`.

**Photos, voice notes and videos** are sent with `POST /api/v1/chat/media/`
(`multipart/form-data`: `conversation`, `kind`, `file`, optional `body` as the caption,
`exercise_id` on a video only, `client_uuid`). Each goes through the platform's
existing guard: a photo is capped at 5 MB (checked first), content-checked and
re-encoded (no EXIF or GPS survives), and a refused photo is not kept anywhere (no
quarantine sample); a video (MP4, MOV, WebM) is identified from its own bytes and capped
at `MEDIA_VIDEO_MAX_BYTES` (64 MB) and `MEDIA_VIDEO_MAX_SECONDS` (90 s), so this is the
third route whose body may exceed 15 MB, on both spellings; a voice note (M4A, Ogg,
WebM) the same way under `CHAT_VOICE_MAX_BYTES` (10 MB) and 90 s. A **daily allowance
per sender** (review 2026-09-24, numbers proposed, not yet confirmed by the owner):
`CHAT_MEDIA_DAILY_COUNT` files (60) and `CHAT_MEDIA_DAILY_BYTES` (512 MB) over any
rolling 24 hours, taken-back files included (they are kept); past it, 429
`media_quota_exceeded` with `retry_after` / `retry_after_seconds` and `Retry-After` —
the seconds until the oldest of those files leaves the window — decided from the
upload's size before its bytes are read; text never counts. The pair rule, blocks, the
coaching freeze and the idempotent `client_uuid` are the text send's, checked before a
byte is stored. A CLIENT's video with an `exercise_id` is a **form check**: it waits in
the trainer's attention queue until the trainer writes in that thread after it. Every
code the route answers, in the order decided: `length_required` (411, a chunked body),
`payload_too_large` (413), `rate_limited` (429), `not_authenticated` /
`token_not_valid` (401), `unsupported_media_type` (415, not a multipart or urlencoded
body), `parse_error` (400, a multipart body that cannot be parsed), `not_found` (404),
`invalid_client_uuid` (400, the top-level `code`), `idempotency_conflict` (409),
`validation_error` (a bad `kind`, a caption over 4000), `exercise_only_on_video`,
`unknown_exercise`, then the send's 403s (`no_relation`, `blocked`, `coaching_frozen`,
`coaching_unpaid`, `coaching_ended`), then `media_quota_exceeded` (429),
`media_file_required`, a video's `media_not_a_video`, `media_video_too_large`,
`media_video_too_long`, `empty_file`, a voice note's `media_not_audio`,
`media_audio_too_large`, `media_audio_too_long`, `empty_file`, and the image guard's own
(`file_too_large`, `empty_file`, `dangerous_extension`, `invalid_mime_type` — a HEIC is
refused here —, `corrupt_image`, `invalid_aspect_ratio` (over 10:1), `unreadable_image`
(a truncated photo), `unsupported_format`, `image_dimensions_too_large`,
`image_too_many_pixels`, and `mime_detection_failed` / `file_validation_error` when the
guard itself fails), and for a photo `upload_busy` (503, 2026-09-27: every image-decode
slot of the server is taken — nothing was stored or counted; send it again after
`Retry-After`). The push quotes the caption, or says "Photo" / "Voice note" /
"Video" in the recipient's language — never a link. A chat video's poster frame comes
later, from a worker (above); a WebM or Ogg file declares no duration, so
`duration_seconds` may be null.

`refusal` / the send's 403 `code`, in the order they are decided: `no_relation` (never an
approved pair and never a paid month), `blocked` (either of the two blocked the other —
`moderation`; the thread stays readable), `coaching_ended` (the relation is gone — ending
a coaching deletes it — but the pair paid for coaching), `coaching_frozen`,
`coaching_unpaid`. `conversations/start/` answers the same 404 for a blocked pair as for
a stranger, and opens (or returns) the thread of a pair whose paid coaching has ended.

`POST .../messages/` may carry `client_uuid`, the app's own id for the message: the first
send answers 201, a retry answers **200** with the same message, and an id already used
by the other person in that thread answers 409 `idempotency_conflict`; one that is not a
UUID answers 400 `invalid_client_uuid` as the top-level `code` (and the socket's `error`
frame names the same code). `DELETE
messages/{id}/` tombstones the sender's own message — the row stays in the thread with
`deleted: true` and an empty `body`, and nothing is ever hard-deleted.

Live: `ws://host/ws/chat/{conversation_id}/?token=<access JWT>`. Client frames
`{"type": "message.send", body, client_uuid?}` (or a program card: `kind: "program"`
and the card's fields, as on REST) and `{"type": "message.read"}`; server frames
`message.new`, `message.updated` (a message already sent now reads differently —
today, a video's `poster_url` arrived; replace the row by `id`), `message.read`,
`message.deleted` and `{"type": "error", code, detail}`. `message.send` is text or a
program card; a media message sent
over REST reaches both sockets as `message.new`. A socket send runs the same checks and the same
serializer as the REST route, and `detail` is in the reader's language. The socket's
own codes: `invalid_frame`, `unknown_type`, `server_error`, `auth` (the token expired or
the account was suspended since connect — re-checked on every frame; the token's expiry
also before every frame the server sends, so a socket that only listens closes too; the
socket then closes with 4001) and `rate_limited` (with `retry_after` seconds: `message.send` and
`message.read` frames spend the same hourly allowance as the REST routes — 1500 an hour
for a client). Close codes: 4001 no usable token (at connect, or after `auth`), 4004 not
your conversation.

Push: `chat_message`, keyed on the conversation, sent only when the recipient has opened
that thread since the last time they were told about it — ten messages in a minute are
one push, however fast the notification worker drains. A message its sender deletes
stops being quoted in the recipient's inbox (`context.preview` is blanked on the
`chat_message` row); a push already on the phone cannot be taken back.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/chat/conversations/` | auth | `ConversationListView` |
| `POST` | `/api/v1/chat/conversations/start/` | auth | `ConversationStartView` |
| `GET, POST` | `/api/v1/chat/conversations/{id}/messages/` | auth | `ConversationMessagesView` |
| `POST` | `/api/v1/chat/conversations/{id}/read/` | auth | `ConversationReadView` |
| `DELETE` | `/api/v1/chat/messages/{id}/` | auth | `MessageDeleteView` |
| `POST` | `/api/v1/chat/media/` | auth | `MediaMessageView` |

### Diet

After the platform plan ends (the owner, 2026-09-19) the subscriber is **frozen**: the
routes marked `HasDietAccessOrFrozenRead` keep answering 200 (the plans and progress stay
readable), every other diet and AI route answers 403 `no_entitlement` as before. The app
tells "ended" from "never subscribed" by `access` on `subscriptions/current/`
(`active | frozen | inactive`), and shows the renewal reminder while `renewal_due` is
true (3 days before the end). Pushes: `subscription_expiring` (3 days before),
`subscription_expired` (on the day it ends, then after 3, 7, 14 and 30 days). No price and
no link in either, on any build.

**`medical_disclaimer`** (spec §9 B4, 2026-09-27, additive): the nutrition onboarding
and diet generation carry one sentence in the reader's language ("This plan is general
guidance, not medical advice. Check with your doctor before changing how you eat…"):
every 200 of `GET` and `POST preferences/` (where the tastes and allergies are saved),
the 200 of `plans/generate/`, both 201s of `plans/generate-rule/`, and
`plans/tasks/{task_id}/` once `plan_id` is there. The same on every build.

No plan is generated from an activity level the client never chose (the owner,
2026-09-20): `plans/generate/` and `plans/generate-rule/` answer 400
`activity_level_required` (`field: "activity_level"`) until it is written, and a trainer's
plan without `daily_calories` answers 400 `client_activity_level_required`.

`advice/latest/` is **not** an AI route, despite the `has_ai_advice` entitlement's
name: the sentence is composed by `diet/tasks.py` from the client's goal, their plan's
calorie target and their BMI, so it carries the diet guard and neither `HasAIAdviceAccess`
nor the `ai_consent_required` guard. Everything the AI coach actually answers is under
`/api/ai/` and carries both. `advice.context_data.user_metrics.daily_calories` is the
figure the advice text names — the plan's target for today — or `null` when there is
none it can stand behind.

`client/progress/`, `client/progress/enhanced/` and `client/progress/weekly/` answer 403
`clients_only` to a caller who is not a client.

**Undo a confirmed meal, the same day** (Road to 10 S3.5, the owner's D11, 2026-11-22).
`POST client/meals/{meal_id}/undo/`, no body, reopens a meal confirmed TODAY (the
server's calendar, `Asia/Damascus`): its ticked components are un-ticked, the meal is not
completed, and the day's totals, the progress reads, home (its cached page is dropped),
the trainer's adherence week, the activity log and the meal-count achievement progress
follow. 200 `{message, meal_id, reopened, components_reopened, meal_is_completed: false,
progress: {date, meals_completed, total_meals, completion_percentage, calories_consumed,
target_calories, calories_percentage, protein_consumed, target_protein, carbs_consumed,
target_carbs, fat_consumed, target_fat, is_day_completed}}`; `reopened: false` when
nothing was confirmed (a second undo is harmless). Refusals: 403 `clients_only`,
`no_entitlement` / `feature_not_in_plan`; 404 `not_found`; 409 `plan_superseded`; 409
`undo_window_closed` (new) with `confirmed_on` (YYYY-MM-DD or null) for a meal confirmed
on an earlier day. Nothing differs by build.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/diet/v1/advice/latest/` | auth, HasDietAccess | `DailyAdviceView` |
| `POST` | `/api/diet/v1/client/meals/interact/` | auth, HasDietAccess | `ClientMealInteractionView` |
| `GET` | `/api/diet/v1/client/meals/{meal_id}/` | auth, HasDietAccessOrFrozenRead | `ClientMealDetailsView` |
| `POST` | `/api/diet/v1/client/meals/{meal_id}/complete/` | auth, HasDietAccess | `MealCompletionView` |
| `POST` | `/api/diet/v1/client/meals/{meal_id}/undo/` | auth, HasDietAccess | `MealUndoView` |
| `GET` | `/api/diet/v1/client/progress/` | auth, HasDietAccessOrFrozenRead | `ClientProgressView` |
| `GET` | `/api/diet/v1/client/progress/enhanced/` | auth, HasDietAccessOrFrozenRead | `EnhancedClientProgressView` |
| `GET` | `/api/diet/v1/client/progress/weekly/` | auth, HasDietAccessOrFrozenRead | `ClientWeeklyProgressView` |
| `GET` | `/api/diet/v1/food/categories/` | auth, HasDietAccess | `FoodCategoryListView` |
| `POST` | `/api/diet/v1/food/import/` | auth, **admin** | `FoodImportView` |
| `GET` | `/api/diet/v1/food/list/` | auth, HasDietAccess | `FoodListView` |
| `GET` | `/api/diet/v1/food/search/` | auth | `FoodSearchView` |
| `GET` | `/api/diet/v1/meals/{meal_id}/components/` | auth, HasDietAccessOrFrozenRead | `MealComponentsView` |
| `GET` | `/api/diet/v1/my/diet-plans/` | auth, HasDietAccessOrFrozenRead | `MyDietPlansView` |
| `GET` | `/api/diet/v1/nutrition/plan/{plan_id}/` | auth, HasDietAccessOrFrozenRead | `DietPlanNutritionView` |
| `GET` | `/api/diet/v1/plan/{plan_id}/meals-with-ingredients/` | auth, HasDietAccessOrFrozenRead | `DietPlanMealsWithIngredientsView` |
| `POST` | `/api/diet/v1/plans/generate-rule/` | auth, HasDietAccess | `GenerateDietPlanRuleBasedView` |
| `POST` | `/api/diet/v1/plans/generate/` | auth, HasDietAccess | `GenerateDietPlanView` |
| `GET` | `/api/diet/v1/plans/tasks/{task_id}/` | auth, HasDietAccess | `PlanTaskStatusView` |
| `DELETE, GET, POST` | `/api/diet/v1/preferences/` | auth | `UserPreferencesView` |
| `GET, POST` | `/api/diet/v1/preferences/food-category/` | auth, HasDietAccess | `UserFoodCategoryPreferenceView` |
| `DELETE, PUT` | `/api/diet/v1/preferences/food-category/{food_id}/` | auth, HasDietAccess | `UserFoodCategoryPreferenceDetailView` |
| `GET, POST` | `/api/diet/v1/trainer/diet-plans/` | auth, HasDietAccess | `TrainerDietPlanView` |
| `DELETE, POST, PUT` | `/api/diet/v1/trainer/meals/` | auth, HasDietAccess | `TrainerMealView` |
| `DELETE, POST, PUT` | `/api/diet/v1/trainer/meals/{meal_id}/` | auth, HasDietAccess | `TrainerMealView` |
| `GET` | `/api/diet/v1/trainer/templates/` | auth, HasDietAccess | `TrainerTemplatesView` |
| `GET` | `/api/diet/v1/trainer/clients/{client_id}/preferences/` | auth, HasDietAccess, **approved trainer** | `TrainerClientPreferencesView` |
| `GET` | `/api/diet/v1/trainer/clients/{client_id}/adherence/` | auth, HasDietAccess, **approved trainer** | `TrainerClientAdherenceView` |
| `GET` | `/api/diet/v1/trainer/clients/{client_id}/automatic-plan/` | auth, HasDietAccess, **approved trainer** | `TrainerClientAutomaticPlanView` |
| `GET, POST` | `/api/diet/v1/trainer/saved-meals/` | auth, HasDietAccess, trainer | `TrainerSavedMealListView` |
| `DELETE, GET, PATCH, PUT` | `/api/diet/v1/trainer/saved-meals/{saved_meal_id}/` | auth, HasDietAccess, trainer (own) | `TrainerSavedMealDetailView` |

### Trainer nutrition plans (BACKEND_BUGS B-131..B-134)

What wiring the `14-trainer-nutrition` screens found. The views' docstrings
(`diet/views.py TrainerDietPlanView`, `TrainerMealView`; `diet/trainer_plan_views.py`)
are the full contract.

**A plan is sent with its meals (B-132).** A trainer plan replaces every active plan of
the client whose dates overlap it, his automatic plan included. `POST
trainer/diet-plans/` did that at once, before any meal existed, and the meals came in
later requests, so a refused meal list or a dropped phone left the client an empty
active plan. Now:

- `POST trainer/diet-plans/` WITH `meals` (a list of at most 500 `trainer/meals/` items
  without `diet_plan_id`) SENDS the plan whole: every meal is checked first, and one
  refused (with `index`) writes and replaces nothing. 201 `{message,
  superseded_plan_ids, will_replace_plan_ids: [], diet_plan}`.
- WITHOUT `meals` (the call as it always was) it makes a DRAFT: nothing is replaced,
  the client does not see it (`my/diet-plans/` leaves it out, `nutrition/plan/{id}/` and
  `plan/{id}/meals-with-ingredients/` answer the client 404), and the first successful
  `trainer/meals/` call on it (one meal or a list) sends it in the same transaction.
  200 `{message, superseded_plan_ids: [], will_replace_plan_ids, diet_plan}`;
  `will_replace_plan_ids` are the plans sending it would replace as things stand.
- `trainer/meals/` answers gain `published: [{diet_plan_id, superseded_plan_ids}]`, the
  plans that call sent (`[]` otherwise), and every refusal inside a list now carries
  `index` (the service's refusals did not).
- The client's own generation replaces only active plans, so it does not touch a draft;
  the draft, once sent, replaces whatever the client then follows.
- A plan is never SENT to begin before today (the review, 2026-09-24): sending would
  retire the plan the client ate from on days they already lived. A create with `meals`
  starting in the past, or a `trainer/meals/` call that would send a draft whose start
  has passed, answers 409 `draft_start_passed` and writes nothing. A replaced plan
  records the day it was replaced (`superseded_on`), and its row's `adherence_percent`
  counts only the days it governed.

**Days (B-133).** `duration_days` (1-364) beside `duration_weeks` on the create; days win
when both are sent; out of range or not a whole number is 400 `bad_duration`. The plan's
last day is `start_date + days - 1`, and its stored weeks are the days rounded up.

**The rows (B-133, B-134).** `diet_plan` on the create and each row of `GET
trainer/diet-plans/?client_id=` gain `status` (`draft`, `scheduled`, `active`, `ended`,
`superseded` — why it is or is not followed), `duration_days`, `duration_weeks`; the
rows also `adherence_percent` (the platform's adherence figure from the meals the client
ticked, the one the AI coach quotes; null for a draft, a plan not started, or nothing
ticked). The list gains `adherence_withheld`: `coaching_frozen` / `coaching_unpaid` /
`coaching_ended` while the coaching cannot be used (every `adherence_percent` is then
null), else null. Rows are newest start first, ties by id.

**The review (B-131).** Per day, the plan's calories and macros against its target,
with each miss judged by the engine's own test and bands (`tolerance_percent`: calories
10, protein 15, carbs 20, fat 20, from `diet/planner/policy.py`), the averages over the
days that hold meals, and the allergen and dislike verdict against the client's own
preferences. For a saved plan you wrote, or for the plan on the phone before it is sent
(nothing is written). Health data: the client's approved trainer only — 403
`not_a_trainer` to anyone else (admins included), `not_assigned_client`,
`not_plan_owner`, `plan_superseded`, and `coaching_frozen` / `coaching_unpaid` /
`coaching_ended` (with `client_id`) while the coaching cannot be used. Identical on every
build.

| Method | Path | Auth | View |
|---|---|---|---|
| `POST` | `/api/diet/v1/trainer/diet-plans/review/` | auth (trainer), HasDietAccess | `TrainerDraftPlanReviewView` |
| `GET` | `/api/diet/v1/trainer/diet-plans/{plan_id}/review/` | auth (trainer), HasDietAccess | `TrainerPlanReviewView` |

**The trainer's macro split (B-137, the owner 2026-09-28).** `POST trainer/diet-plans/`
and `POST trainer/diet-plans/review/` take optional `target_protein`, `target_carbs`,
`target_fat` (grams a day, all three or none). Sent, they are the plan's target, stored
as sent, when 4P + 4C + 9F is within 10 percent of `daily_calories` and the protein
within the engine's bounds for the goal (60-250 g); otherwise 400 `bad_macro_split` and
nothing is written. Not sent, the split is derived from the calories and the goal as
before. It is set when the plan is written; no route edits a plan's split. The create's
`diet_plan` gains `daily_targets` `{calories, protein, carbs, fat}`, the target the plan
holds.

**Whose plan (B-140).** The `plan` block of `plan/{id}/meals-with-ingredients/` gains
`client_id`, the user whose plan it is.

New codes: `bad_macro_split` (400, above). `bad_meals` (400: `meals` is not a non-empty
list, or an item is not an object). Reused with their meaning: `bad_duration` (now also
`duration_days`),
`batch_too_large` (with `limit`), `duplicate_meal` and the meal refusals (with `index`),
`not_a_trainer`, `not_assigned_client`, `not_plan_owner`, `plan_superseded`,
`coaching_frozen`, `coaching_unpaid`, `coaching_ended`.

### Home (server-driven)

The screen the app opens on, and the fourth layer of the design canvas's build guide.
The server answers an ORDERED list of typed sections and the app renders what it is
handed: it must cope with any subset, in any order, and SKIP a `type` it does not know,
which is what lets an old build survive a new section.

`{"role", "as_of", "refresh_after_seconds", "sections": [{"type", "data"}]}`. A section
is `{"type", "data"}` and nothing else, and one with nothing to say is left out rather
than sent empty. `refresh_after_seconds` is how long until the page may change: 120 on a
freshly built page — or fewer, the seconds until the first `next` entry of today's
timeline turns due, when that is sooner (BACKEND_BUGS B-179, 2026-09-28) — and the time
LEFT (counted from `as_of`) on one served from the cache (BACKEND_BUGS B-043). In the `workout_hero` week strip a day before the
routine starts or after it ends is `rest` and is not counted in `week_stats.workouts
.planned` (B-045); `counts.estimated_minutes` is null when the coach set no duration
(B-044); `challenge_rank.participants` is counted by the same rule as `rank` (B-046). `role` is the account's shell (`client`, `trainer`, `agent`, `admin` —
`admin` is the operator); only the client shell is designed, so the other three get
`"sections": []` today rather than a refusal.

The closed set of types: `workout_hero`, `week_stats`, `nutrition_today`,
`trainer_message`, `ai_advice`, `challenge_rank`, `find_trainer_hero`,
`setup_checklist`, `featured_trainers`, `ready_programs`, `media_banner`, and
`day_timeline` (the owner, 2026-09-28 — below). The two home
artboards are two lists out of that one set — a client with no coach gets the first-day
sections — and every `data` shape, every code and everything that differs by build is
written out in the docstring of `home/views.py HomeView`, which is the contract.

Nothing here is a rendered sentence: sections carry codes and numbers (muscle keys,
`cta.state`, meal states, a refusal code, an advice code) and the app writes the Arabic
or the English. What differs by build, and by nothing else: `ready_programs` and
`media_banner` are ABSENT on a build whose `can_show_prices` is off (`play`, `ios`, and
a caller that declares no channel), and the `coaching_price` / `money_type` keys inside
`featured_trainers` follow `can_show_coaching_prices` exactly as on
`GET /api/v1/trainers/matches/`. Nothing else in the payload carries a price, an amount
or a link to the web hub, on any build.

`week_stats` and the streak are the same numbers, from the same cache entry, as
`GET /api/routine/v1/analytics/recent-progress/`; `trainer_message` reads `chat/` and
reports the same unread count as `GET /api/v1/chat/conversations/`; `ai_advice` appears
only once the person has given AI consent (`/api/v1/ai-consent/`). The payload is cached
per user, per build and per language for 120 s (or until the first `next` meal or
workout of today's timeline turns due, when that is sooner — B-179, below) and retired at once when a workout
session is started, changed or finished, a day closes, a chat message arrives or is
taken back, the client reads the thread, a block is made or lifted, or the client's
coaching relation changes.

Additive in `workout_hero`: an open session begun while the coaching was paid answers
`cta: {"state": "continue", "session_id"}` after the coaching freezes, because the
server still accepts its sets and its finish (judged at the session's start). Additive
in `trainer_message`: `message.deleted` (bool). A newest message its sender took back
keeps the card with `body: ""` and `deleted: true`, as the conversation list shows it.

**What the home-records build added** (BACKEND_DELTA items 9 and 12, 2026-09-27).
Additive: no route, section type, key or `code` renamed or removed; no new route.

- **The day timeline** (since 2026-09-28 ALSO answered as the `day_timeline` section, the
  owner's decision — "What wiring the home found" below) is `nutrition_today.timeline`, not a new section type: the
  canvas's closed set of types has none for it, its «يومك» is the meal list with the
  workout placed among it, and a key inside the existing card is invisible to every
  installed build (a new type would also be skipped, but would move the section order
  and draw the same meals twice). Entries `{kind: "meal", at, state, meal_id,
  meal_type, name, calories}` and at most one `{kind: "workout", at, at_source, state,
  routine_id, routine_name, day, muscles, exercises, sets, estimated_minutes}` (+
  `session_id` while a session on it is open, + `refusal` when the hero's button is
  `refused`). `state` `done | now | next`, a meal's being its card's `state` renamed;
  `at_source` `session | reminder_hour`. No diet section, no timeline. The «الآن» pill
  belongs to the last `now` entry that has a time (`at` not null); only when no timed
  entry is `now`, to the last untimed `now` (2026-09-28, BACKEND_BUGS B-178: an untimed
  meal is `now` all day and sorts last, so "the last `now`" gave it the pill over a
  workout under way).
- **`ready_programs` cards are the routine products on sale** — each is the
  `GET /api/v1/products/` row, plus the section's old keys from the product (`id` is
  now the PRODUCT's; `name`, `description`, `goal`, `days_per_cycle`, `exercises`,
  `trial` = a free preview exists) and `equipment` (codes), `no_equipment`, and
  `money_type: "ROUTINE_PRODUCT"` beside `price`. The section is still ABSENT on a
  build whose `can_show_prices` is off (`play`, `ios`, no channel), per the BuildGuide.
  `goal` is the row's own (2026-10-07, wave-1 review R-5): one of the four codes
  `build_muscle` / `strength` / `fat_loss` / `rehab`, or null — the same value
  `GET /api/v1/products/` answers; the card no longer writes the template's raw text
  over it. A template's `goal` is that closed list on every write — the template
  route, both operator consoles and `POST templates/{id}/copy/`.
- **`trainer_message` of `kind: "voice"`** adds `audio_url` (PRIVATE: signed and
  expiring, like the thread's `media.url`), `duration_seconds` and `state`
  (`ready | unavailable`). Only that kind carries them.
- **`trainer_message` of `kind: "program"`** (2026-09-28, BACKEND_BUGS B-175) adds
  `program`: `{routine_id, template_id, name, day, day_name, exercises, changes}` — the
  thread's own card for the same message — or null when the card was taken back, its
  routine deleted, or the routine is no longer one the client may read. Only that kind
  carries it; a text message still has exactly its five keys.
- **`PATCH/PUT workout-sessions/{id}/` that finishes a session** (and a `POST` of one
  already `completed`) answers `personal_records` — see "What wiring the train screens
  found" below.

**What wiring the home found, 2026-09-28** (BACKEND_BUGS B-175, B-176, B-178, B-179,
B-182; the owner's decisions of that day). Additive: no route, key or `code` renamed or
removed; no new route.

- **`day_timeline`, a 12th section type** (B-176): `{date, timeline}`, the SAME list as
  `nutrition_today.timeline` (one builder, one rule: no diet section, no rail; left out
  when the day is empty), answered where `Home.html` draws the rail. The order is now
  `workout_hero`, `find_trainer_hero`, `setup_checklist`, `trainer_message`,
  `day_timeline`, `week_stats`, `nutrition_today`, `ai_advice`, `challenge_rank`,
  `featured_trainers`, `ready_programs`, `media_banner` — `nutrition_today` (the
  calories card) moved after `week_stats`. `nutrition_today.timeline` stays for the
  builds already installed, which skip the type they do not know.
- **One hero** (B-182): `find_trainer_hero` is not sent beside a `workout_hero` — a
  client with no coach who bought a programme gets the day's hero alone. The one state
  that answers both is a client with no coach whose day is `refused` (a former client
  whose coaching ended): `find_trainer_hero` then comes FIRST, `workout_hero` right
  after it.
- **`ready_programs` for a coached client too** (the owner): same build gating (absent
  where prices may not show), same place; a programme already bought is `owned: true`,
  `can_buy: false`.
- **The page lives until its next state change** (B-179): a fresh page's
  `refresh_after_seconds` (and its cache entry) is 120, or the seconds until the first
  `next` meal or workout on today's timeline turns due when that is sooner, so a meal
  due inside the window is `now` once its time comes. Midnight is not a boundary.

**The free first workout (Road to 10 S2.4, the owner's D10, 2026-11-22).** Additive: a
13th section type, `trial_workout`, between `featured_trainers` and `ready_programs`
(HomeNew's order), and one new route, `GET/POST /api/routine/trial-workout/` (Routines
below). Sent only to a client with no approved coach (a frozen or unpaid coaching is a
coach) and no programme (no routine assigned), once the server has the platform's
trial workouts (`manage.py bootstrap_platform`):
`{template_id, routine_id | null, day: 1, place: home | gym, name, description,
exercises, sets, estimated_minutes, equipment, no_equipment, muscles, last_done_on |
null, times_done}` (+ `session_id` while a session on it runs). `place` is the
onboarding answer (`training_place`), home when unanswered. Identical on every build:
free, no price, nothing to buy.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/home` | auth | `HomeView` |

### Notifications

`POST message-client/` `{client_id, message}` — a trainer's one-line notice to a client
they coach. It answers to the chat's rules: 404 `not_found` for a client who is not an
approved client of the caller, 403 `user_blocked` when either of the two has blocked the
other, and 403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` (with
`trainer_id`) when the coaching cannot be used. An admin is not a party to a coaching and
is not refused by those three.

`comment_created` quotes the first words of a comment; once a moderator hides the
comment or it is deleted, the quote is blanked on the inbox row.

**Email (the payments spec, section 7; 2026-09-27).** Six kinds of notice also send an
email, to a VERIFIED address only, written in the recipient's saved
`preferred_language`, with a plain-text and an HTML part, by the worker after the commit
(`notifications/channels/email.py`). No email carries a link, a button or the price of
anything to buy, on any build:

| preference `event_type` | the email |
|---|---|
| `payment_confirmed` | the payment's receipt as a PDF attachment (the same document as `payments/{id}/receipt/?format=pdf`). Email only: there is no push of this type |
| `subscription_expiring` | once per cover, when its end comes within 7 days. The 3-day push is unchanged and sends no email |
| `subscription_expired` | once per cover, with its first lapsed notice (not on each reminder step) |
| `withdrawal_requested`, `withdrawal_paid`, `withdrawal_failed` | with each push; the number and the amount |

Preferences govern the email as they govern the push: `channels.email: false` on a type's
row (or on `all`) stops its email, `is_enabled: false` stops push and email, and the
inbox row is written either way. `GET preferences/event_types/` answers additively
`{"event_types": [...], "email_event_types": [the six above]}`; `payment_confirmed` is a
valid `event_type` for `POST preferences/` (it was `400 unknown_event_type`). Since
2026-11-22 (Road to 10 S0.4) it also answers `role` and `role_event_types` /
`role_email_event_types`: the same lists cut to the kinds the caller's ROLE is ever sent
(`notifications/audience.py`, read from each kind's call site), `all` always kept. A
settings page draws those rows; `event_types` keeps every kind, and a preference on any
listed kind is still accepted. A trainer's kinds: `client_request_received`,
`client_request_cancelled`, `client_unassigned_trainer`, `client_request_approved` /
`_rejected`, `coaching_ended` / `_finished`, `coaching_fee_held` / `_released` /
`_refunded`, `trainer_verification_approved` / `_rejected`, `withdrawal_requested` /
`_paid` / `_failed` (also by email), `routine_product_approved` / `_rejected` / `_sold`
/ `_sale_refunded`, `trainer_weekly_digest`, `chat_message`, `session_completed` and the
five social kinds.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/notifications/` | auth | `APIRootView` |
| `POST` | `/api/notifications/message-client/` | trainer or admin | `TrainerMessageView` |
| `GET, POST` | `/api/notifications/preferences/` | auth | `NotificationPreferenceViewSet` |
| `GET` | `/api/notifications/preferences/event_types/` | auth | `NotificationPreferenceViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/notifications/preferences/{id}/` | auth | `NotificationPreferenceViewSet` |

### Media (design v2 slots)

Every picture fills a named slot and travels as `media: {"<slot>": {url_1x, url_2x, blur,
focal: [x, y], brightness, alt_ar, alt_en}}`; an empty slot is absent. Trainer cards carry
theirs; the app's own pictures are here. See `mediakit/slots.py` for the slot list.

A **video** slot (`exercise.loop`, `exercise.cue`, `trainer.loop`, `trainer.intro`) fills
the same place with a different set of keys: `{video_url, poster_url, hls_url,
duration_seconds, width, height, state, alt_ar, alt_en}`. `state` is `pending` | `ready` |
`failed` — `pending` means the video plays now and its poster frame is still being cut.
`poster_url` and `hls_url` are ABSENT until they exist (`hls_url` needs a transcoder,
which is not built yet). The poster is cut off the request by the worker's ffmpeg
(`mediakit.fill_video_metadata`; the image installs ffmpeg since 2026-09-27): a frame
one second in, or the first frame of a shorter clip, with no metadata. A worker without
ffmpeg logs it once and leaves the asset `ready` with no poster. Every media URL is signed and expires after an hour
(`MEDIA_SIGNED_URL_TTL`): use what the API just returned, never a stored copy.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/content/media/` | public | `AppMediaView` |
| `POST` | `/api/v1/content/media/video/` | auth (owner of the target, or admin) | `VideoUploadView` |
| `GET` | `/api/v1/content/media/exercise/{id}/` | auth | `ExerciseMediaView` |

`GET /api/v1/content/media/` differs by BUILD in one slot: `app.banner` (the operator's
promotional 21:9 picture) is served only when the build's `can_show_prices` flag is on —
`apk` and `web` by default, never `play` or `ios`, never a request with no
`X-App-Channel` — the same rule home applies to the same asset. Elsewhere it is absent,
exactly like an empty slot. The response carries `Vary: X-App-Channel, X-App-Version`.

`POST /api/v1/content/media/video/` is `multipart/form-data`: `file`, `slot`,
`owner_type`, `owner_id`, optional `alt_ar`/`alt_en`. It is the one route whose body may
exceed 15 MB (cap `MEDIA_VIDEO_MAX_BYTES`, 64 MB; duration cap `MEDIA_VIDEO_MAX_SECONDS`,
90 s). The container is decided by the FILE'S BYTES, never by its name or its
`Content-Type`: MP4/MOV and WebM only. Codes: `media_file_required`,
`media_unknown_slot`, `media_unknown_owner`, `media_slot_is_not_video`,
`media_not_a_video`, `media_video_too_large`, `media_video_too_long`,
`media_alt_too_long` (`alt_ar`/`alt_en` over 200 characters, refused before anything is
stored), plus `payload_too_large` (413) from the body cap. Answers on both spellings, with and without
the trailing slash, as `capabilities` does (B-035).

`GET /api/v1/content/media/exercise/{id}/` answers
`{exercise_id, media, media_source, media_trainer_id}` and obeys the exercise visibility
rule exactly as `/api/routine/exercises/{id}/add-media/` does — an exercise the caller
may not see answers `404 code="not_found"`, byte-identical to an id that does not exist.
Video is not an exception to that rule. Pass `?routine_id=` when the exercise was opened
from inside a routine (see the next section for what that changes); a routine the caller
is not part of is ignored, not refused.

### The per-trainer exercise library (design v2 item 7)

A coach puts THEIR OWN demonstration on a catalogue exercise, so the client watches
their coach perform the move instead of a stock clip. The exercise row is shared and is
never copied — one id, one history, one set of records for everybody — and the coach's
footage hangs beside it as a `trainer_exercise` asset keyed by (trainer, exercise).

**Which media a viewer is shown** is decided in one place,
`mediakit.resolution.resolve_exercise_media`, and in this order:

1. the media the author of THAT routine put on that exercise — the coach who
   prescribed it is the one demonstrating it. This line is for the client the routine
   was prescribed to, its author and an operator; a trainer reading a routine another
   coach wrote (a new coach reading their client's old programme) skips it, so one
   coach's private footage never reaches another;
2. failing that, the media of any trainer the viewer has an approved relation with;
3. failing that, the shared catalogue's own media.

A client with no coach gets (3). A trainer sees their own library over the catalogue. A
whole source wins, not a slot at a time: a coach who filmed only `exercise.loop` shows
their loop and no poster, rather than their loop over the catalogue's still.

Every exercise object (`ExerciseSerializer`: the exercise list and detail, the routine
day, the template rows, the daily summary) therefore carries three ADDITIVE fields:

| field | value |
|---|---|
| `media_slots` | `{slot: payload}` in the shape above, resolved. An empty slot is absent |
| `media_source` | `routine_author` \| `my_trainer` \| `catalogue`, or `null` when there is no media anywhere |
| `media_trainer_id` | whose it is; `null` for the catalogue |

`media` on an exercise is UNCHANGED — it is still the legacy `ExerciseMedia` list
(`[{id, media_type, content}]`). The slots arrive under `media_slots` precisely so that
nothing already shipped moves.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET, POST` | `/api/v1/trainer/exercise-media/` | auth (trainer, or admin with `trainer_id`) | `TrainerExerciseMediaView` |
| `DELETE` | `/api/v1/trainer/exercise-media/{id}/` | auth (the asset's trainer, or admin) | `TrainerExerciseMediaDetailView` |
| `GET` | `/api/v1/trainer/media-kit/` | auth (trainer, or admin with `?trainer_id=`) | `TrainerMediaKitView` |

`GET media-kit/` (BACKEND_BUGS B-094) is the trainer's OWN kit, whether or not the
directory lists them (the public card `/api/v1/trainers/{id}/` answers 404 for a trainer
it does not show): `{trainer_id, kit_status: {complete, filled, required, missing},
filled, total, slots: [{slot, kind, required, done, media}], media}`. `slots` is the eight
`trainer.*` slots in slot-list order; `media` is the public card's `{slot: payload}`, an
empty slot absent; `kit_status` counts the required slots (what featuring and first place
need), `filled` / `total` all eight. `403 not_a_trainer` for anyone else. Same on every
build.

`GET ?exercise_id=` answers `{exercise_id, trainer_id, count, limit, media}` — what THIS
trainer has on that exercise, never the catalogue's and never another coach's.

`POST` is `multipart/form-data`: `file`, `exercise_id`, `slot` (`exercise.poster`,
`exercise.thumb`, `exercise.loop`, `exercise.cue`), optional `alt_ar`, `alt_en`, and
`trainer_id` for an admin. Image or video is decided by the SLOT and each goes through
the platform's existing pipeline for its kind — the same content sniffing and the same
caps as `/api/v1/content/media/video/` (`MEDIA_VIDEO_MAX_BYTES` 64 MB,
`MEDIA_VIDEO_MAX_SECONDS` 90 s), so this is the second route whose body may exceed
15 MB. Uploading into a filled slot replaces it. Answers on both spellings, with and
without the trailing slash.

A trainer may only touch an exercise they may SEE (`visible_exercises_for`: their own
plus the shared catalogue) and only their own assets. `DELETE` never reaches the
catalogue's media, whoever asks. Verification (`trainer_is_verified`) is NOT required —
it gates nothing else a trainer publishes on this platform.

Codes: `media_exercise_id_required`, `media_file_required`, `media_unknown_slot`,
`media_library_full` (the per-exercise cap, `MEDIA_TRAINER_EXERCISE_MAX_ASSETS`,
3), `media_kind_not_supported_yet`, plus every code the image and video pipelines
already answer (`media_not_a_video`, `media_video_too_large`, `media_video_too_long`,
`media_wrong_ratio`, `media_too_small`, `media_unsupported_format`,
`media_alt_too_long`), `not_a_trainer`
(403) and `not_found` (404). A catalogue asset, another coach's asset, an asset on an
exercise the caller may not see and an id that does not exist all answer the same 404,
byte for byte.

`POST /api/v1/content/media/video/` refuses `owner_type=trainer_exercise` with 403: the
coach's library has one way in, so the cap lives in one place.

### Trainer verification (plan step 6, BACKEND_BUGS B-090)

A trainer sends five documents — the identity card's front and back, a personal-trainer
certificate, a recent photo (all required) and a sports-nutrition certificate
(optional) — and an operator reviews them in the console (`/dj-admin/`, the
"Verification queue", oldest submission first). The design is
`12-trainer-shell/TrainerVerify` and `TrainerLocked`. The views' docstrings
(`trainer_verification/views.py`) are the full contract: the body, every `code`, and what
each state allows.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/trainer/verification/` | auth (trainer) | `MyVerificationView` |
| `DELETE, POST` | `/api/v1/trainer/verification/documents/{slot}/` | auth (trainer) | `DocumentView` |
| `POST` | `/api/v1/trainer/verification/submit/` | auth (trainer) | `SubmitView` |

**States:** `not_started` → (submit) `pending` → `verified`, or `rejected` with a
`rejection_reason` → fix and submit again (a new `round`; nothing the operator decided
before is erased). An operator can also revoke a verification, which answers
`rejected` with the reason: the documents the revoke names are `rejected` (in
`needs_replacing`, and `submit` answers `verification_documents_rejected` until they
are replaced); a revoke that names none sends every document back to `uploaded`, to be
reviewed afresh. `status` is `verified` for every verified trainer, including those
verified by hand before this existed (no documents on file).

**The nutrition certificate** is optional and has no effect on roster or match order:
the roster and the matches rank on `trainer_is_verified`, never on a document. The
design's "ترفع ترتيبك" ("raises your ranking") on that row is not built — whether it
should be is an owner decision — so the app must not promise it.

**Each document** is listed for every slot, filled or not: `state` `missing` |
`uploaded` | `accepted` | `rejected` (with `rejection_reason`), a `thumbnail_url` (images)
and a `file_url`. `{slot}` is `identity_front`, `identity_back`, `trainer_certificate`,
`nutrition_certificate` or `photo`. `POST` is multipart with one `file`; posting into a
filled slot replaces it, and after a rejection every change resets that document to
`uploaded`. `can_change` says per slot what is allowed now: anything while `not_started`
or `rejected`, only an empty optional slot while `pending`, nothing once `verified`.

**The files are private.** They are stored apart from every other upload, never under
`/media/`, and every URL is signed and expires after `url_expires_in` seconds (five
minutes): read the route again rather than storing a URL. Only the trainer (their own)
and operators are ever given one. On the development backend they are served at
`/private-media/...?e=&s=`; on GCS the bucket signs them. A photo is stored re-encoded,
upright, with its metadata (GPS included) removed; the bytes decide the format — JPEG,
PNG or WebP photos, or a PDF for the two certificates — never the name. HEIC is refused.
How large a photo may be is what a 512 MB server can open: at most 10,000 px on a side;
a camera JPEG up to 40 MP (over 16 MP it is stored at half its width and height); a PNG
up to 16 MP, a WebP about 6.7 MP, a progressive JPEG about 8 to 13 MP by colour format.
Over that, `verification_image_too_large` carries `max_pixels` and `max_side`.

Codes (every one listed in the view docstrings): `not_a_trainer` (403),
`verification_unknown_slot` (404), `verification_file_required`,
`verification_unsupported_format` (`accepted`), `verification_unreadable_file`,
`verification_file_too_large` (`max_bytes`), `verification_image_too_large`
(`max_pixels`, `max_side`), `verification_image_too_small` (`min_side`), `verification_pdf_encrypted`,
`verification_pdf_active_content`, `verification_duplicate_document` (`slot`) — all 400;
`verification_in_review`, `verification_already_verified`,
`verification_already_pending`, `verification_documents_missing` (`missing`),
`verification_documents_rejected` (`rejected`) — all 409.

The decision arrives as the push `trainer_verification_approved` or
`trainer_verification_rejected` (§4; the rejection carries the reason). Verification
changes nothing an unverified trainer may do today (see BACKEND_DELTA decision 5): it sets
`trainer_is_verified`, which the roster ranks and the paywall exemption reads.
`GET /api/auth/trainer/profile/` and `GET /api/auth/user/update/` also carry a read-only
`verification_status` (null for anyone who is not a trainer).

### Routine products (plan step 10, BACKEND_BUGS B-099, B-100)

A trainer lists one of their templates for anyone to buy (`15-trainer-money/TrainerProducts`,
`TrainerProductForm`); an operator reviews it in the console (`/dj-admin/`, "Routine
products": approve, or reject with a reason the trainer is shown); a client buys it from
the wallet and gets their OWN copy, an ordinary routine of theirs. This is
`ROUTINE_PRODUCT` money, on the other side of the store rules from coaching (payments
spec §3): separate models (`routine.RoutineProduct`, `routine.RoutinePurchase`), these
separate routes, and a buyer's copy that belongs to the buyer (`created_by` is the buyer;
the trainer never sees it, and no coaching ever freezes it). The views' docstrings
(`routine/product_views.py`) are the full contract; the money is `routine/products.py`.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET, POST` | `/api/v1/trainer/products/` | auth (trainer) | `TrainerProductsView` |
| `GET, PATCH` | `/api/v1/trainer/products/{id}/` | auth (trainer) | `TrainerProductDetailView` |
| `POST` | `/api/v1/trainer/products/{id}/submit/` | auth (trainer) | `TrainerProductSubmitView` |
| `POST` | `/api/v1/trainer/products/{id}/retire/` | auth (trainer) | `TrainerProductRetireView` |
| `GET` | `/api/v1/products/` | auth | `ProductCatalogueView` |
| `GET` | `/api/v1/products/{id}/` | auth | `ProductDetailView` |
| `POST` | `/api/v1/products/{id}/buy/` | auth (client) | `ProductBuyView` |

**States:** `draft` → (submit, a VERIFIED trainer) `in_review` → (operator) `published`,
or back to `draft` with `review.reason`; `retired` from any state (the trainer). Only a
`draft` is edited (`PATCH`). A published product whose template is edited afterwards is
off sale (`on_sale: false`, `template_changed: true`) until it is submitted again and
approved — the review saw a version of the program, and only that version is sold. A
product is also off sale while its trainer is not verified: an operator who revokes a
verification takes the trainer's products out of the catalogue and `buy/` (404
`not_found`), and they come back unchanged if the trainer is verified again.

**The sale** (`buy/`, one atomic step, idempotent on `idempotency_key`): the price
leaves the buyer's wallet, `COACHING_COMMISSION_RATE` of it (20%) goes to the platform
and the rest to the trainer, all three ledger rows `ROUTINE_PRODUCT`; the rate is
snapshotted on the purchase. A price in the other currency is converted at the
operator's rate (snapshotted too); a stale or missing rate refuses with
`exchange_rate_stale` / `exchange_rate_missing` before anything moves. The ledger reads
the buyer's row as `kind: routine_product_purchase` (counterparty: the trainer) and the
trainer's as `routine_product_sale` (no counterparty: the buyer is not named).
**Refund** is operator-only (console, "Routine purchases" → refund, with a reason): every
leg is reversed at the sale's amounts and rate, and the buyer's copy is revoked
(unassigned, retired; their training history on it is kept). A training write that
still names a refunded copy answers 403 `product_refunded`.

**Differs by build.** Buying: `apk` and `web` only — `play` / `ios` are refused in code
(403 `not_available_on_channel`; no channel: 400 `app_channel_required`). Prices: a build
whose `can_show_prices` is off is sent NO price key (catalogue, detail, and the trainer's
own product), `can_buy` is false, and a `price` in a trainer's body from such a build is
refused 403 `not_available_on_channel`. Listing, reading, the free preview (`preview`: the
first `preview_days` days of the program), owning and training on a bought copy work on
every build.

Codes: `not_a_trainer` (403), `not_found` (404), `validation_error` (400;
`field_errors.<field>[0].code`: `template_not_yours`, `price_out_of_range`,
`preview_days_out_of_range`, `cover_not_yours`), `product_not_editable` (409, `status`),
`trainer_not_verified` (403), `product_not_submittable` (409, `status`),
`product_incomplete` (409, `missing`), `program_too_long` (409, `max_days`,
`program_days`), `preview_covers_program` (409, `program_days`, `cycle_days`: the preview must be shorter than the template's week, which the program repeats),
`product_already_retired` (409), `bad_trainer_id` (400), `own_product` (403),
`clients_only` (403), `already_owned` (409, `purchase_id`, `routine_id`),
`checkout_open` (409, `payment_id`: `buy/` while the caller has a checkout of this product
open on the web hub — paying both would pay twice), `product_not_for_sale` (409), `exchange_rate_missing` / `exchange_rate_stale` (409),
`insufficient_balance` (402, `amount`, `currency`), `idempotency_key_reused` (422),
`purchase_refused` (400), `throttled` (429). Pushes: `routine_product_approved`,
`_rejected`, `_sold`, `_purchased`, `_refunded`, `_sale_refunded` (§4).

**Templates (B-100, additive).** `RoutineTemplateSerializer` (every `templates/` route)
gains `weeks` (writable, 1-52, or null while the trainer has not said how many weeks the
program runs) and `session_count` (read-only: `days` x `weeks`, null while `weeks` is).
`DELETE templates/{id}/` answers 409 `template_listed_for_sale` while a product of yours
is in review or published on it.

### Trainer home: the attention queue (B-091)

The trainer's home is a queue, not a dashboard (`12-trainer-shell/TrainerHome`): one
ordered list of what needs them, each item with its own actions. `GET` only, trainers
only (403 `not_a_trainer` for anyone else). Identical on every build except the quote
on a request row: `agreed_rate`, `agreed_currency`, `trainer_net`, `commission_rate` are
null where `can_show_price` forbids a coaching fee (`ios`, no channel). The view's
docstring (`users/attention_queue.py AttentionQueueView`) is the full contract.

`{count, items: [{kind, id, client: {id, name, first_name, last_name,
profile_picture}, at, <kind>: {...}}]}` — `kind` is `new_request`, `form_check`,
`idle_client`, `slipping_client` or `program_ending`; `id` is stable (`form_check:812`);
`at` is when the item became due, never later than the response; the kind's own fields
sit under a key named after it: the pending-requests row (`pending_request_row`, the
quote and the client's v2 answers included); the form check's `message_id`, `conversation_id`, `exercise`,
`caption`, `duration_seconds`, `sent_at` and signed `media`; the idle client's `reason`
(`quiet` / `never_started`), `days`, `last_trained_on`, `last_workout`, `program`; the
slipping client's `this_week_percent`, `usual_percent`, `planned_sets`, `missed_sets`,
`program`; the ending programme's `routine_id`, `name`, `week` of `weeks`, `end_date`,
`days_left` (within 7 days).

One total order: requests, form checks, idle, slipping, programmes ending; within a
kind the longest-waiting request, the oldest unanswered video, the highest `attention`
urgency score, the biggest drop against the client's own average, the fewest days
left — ties broken by a unique id. Only approved, active, unblocked clients; a client
whose coaching is frozen, unpaid or ended at that moment (the instant the chat refuses)
appears in none of the kinds. A form check is a video sent since the current coaching
began, and leaves the queue when the trainer sends any message in that thread after it.
The threshold for idle is `attention`'s own (`QUIET_DAYS`, 5 days); `never_started` is
only for a client who has never trained. The four older reads
are unchanged.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/trainer/attention/` | auth (trainer) | `AttentionQueueView` |

### Trainer withdrawals and payout methods (plan step 6, BACKEND_BUGS B-097, B-098)

The trainer's money out (`15-trainer-money/TrainerWithdraw`, `TrainerWithdrawStatus`,
`TrainerPayoutMethod`; the spec, section 8 "Trainer payouts"). Trainers only: anyone
else is 403 `not_a_trainer`. The views' docstrings (`payouts/views.py`) are the full
contract: every body, every `code`, what differs by build.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/trainer/withdrawals/setup/` | auth (trainer) | `WithdrawalSetupView` |
| `GET, POST` | `/api/v1/trainer/withdrawals/` | auth (trainer) | `WithdrawalListView` |
| `GET` | `/api/v1/trainer/withdrawals/{id}/` | auth (trainer) | `WithdrawalDetailView` |
| `POST` | `/api/v1/trainer/withdrawals/{id}/cancel/` | auth (trainer) | `WithdrawalCancelView` |
| `GET, POST` | `/api/v1/trainer/payout-methods/` | auth (trainer) | `PayoutMethodListView` |
| `DELETE` | `/api/v1/trainer/payout-methods/{id}/` | auth (trainer) | `PayoutMethodDetailView` |
| `POST` | `/api/v1/trainer/payout-methods/{id}/default/` | auth (trainer) | `PayoutMethodDefaultView` |

**Payout methods.** `shamcash` — `{kind, address, address_confirm, holder_name}`: the
account ADDRESS typed twice (spaces, dashes and case do not matter) and the name on the
account; or `agent_cash` — `{kind, agent_id}`, cash collected from an agent of
`GET /api/wallet/agents/`. Never a password, PIN or code. The first is the default;
`is_default: true`, or `POST …/{id}/default/`, moves it. A method reads `{id, kind,
address, holder_name, agent, is_default, verified, verified_at, created_at}`;
`verified` is the design's «محقّق» (money sent there arrived). Codes:
`payout_method_kind_invalid`, `payout_address_required`, `payout_address_invalid`,
`payout_address_mismatch`, `payout_holder_name_required`, `payout_holder_name_too_long`,
`payout_agent_unavailable` (400); `payout_method_exists` (with `id`),
`payout_methods_limit` (with `max`) (409); `payout_method_not_found` (404).

**A withdrawal.** `POST withdrawals/` `{amount, method_id?, idempotency_key}` —
**only verified trainers** (403 `trainer_not_verified`). The amount leaves the wallet at
once, into the platform's payout-clearing wallet, so it cannot be spent twice: the
design's «أموالك في رصيدك حتى يُؤكَّد الوصول» does not describe this server — the money
is held for the request and comes BACK to the balance only if it fails or is cancelled.
States: `requested` → `approved` → `sent` → `confirmed` (arrived), or `failed` (money
back, `failure_reason`), or `cancelled` by the trainer while `requested`
(`POST …/{id}/cancel/`, 409 `withdrawal_not_cancellable` after; 409
`withdrawal_hold_reversed` when support already put its money back). Numbered
`WD-2026-09-23-041`. A withdrawal reads `{id, number, amount, currency, state,
destination {kind, address, holder_name, agent}, method_id, transfer_reference,
failure_reason, can_cancel, requested_at, approved_at, sent_at, confirmed_at, failed_at,
cancelled_at, timeline [{state, at}]}`; `destination` is a copy taken at the request.
An operator approves, sends through ShamCash, records the transaction id (one
transaction pays one withdrawal), confirms or fails it in the console (`/dj-admin/`,
"Withdrawal queue"). Guardrails and their codes: `withdrawal_below_minimum` (400,
`minimum`), `withdrawal_already_open` (409, one at a time), `daily_limit_exceeded` (429,
`daily_cap_left`), `insufficient_balance` (402), `amount_exceeds_available` (409 —
part of the balance is settling; `available`, `settling`, `settling_until`),
`payout_method_required` (409), `payout_method_not_found` (404),
`payout_agent_unavailable` (409), `request_in_flight` (409), `idempotency_key_reused`
(422). `setup/` answers every number the screen draws — `balance`, `available`,
`settling`, `settling_until`, `hold_days`, `in_withdrawal`, `minimum`, `daily_cap`,
`daily_cap_left`, `fee` (always `0.00`), `verified`, `open_withdrawal`, `methods` — and
`refusal`, the code a request would get right now whatever the amount, or null.

**By build.** Requesting and cancelling move wallet money, so they call the build check
first: 403 `not_available_on_channel` where `can_transfer_wallet` is off (the first iOS
build), 400 `app_channel_required` with no known `X-App-Channel`. `setup/` shows the same
in `refusal`. Every read and the payout methods are identical on every build.

**The settling window (B-097).** A coaching payout — a `trainer_fee_payout` row — and
a program sale — a `routine_product_sale` row (a buyer can be refunded, and the refund
takes the trainer's share back; B-138) — are "settling" for `hold_days` (3) after their
release before they can be withdrawn (`wallet.settling.SETTLING_KINDS`); nothing else
settles (a client's payment to a trainer, `transfer_received`, is withdrawable at once),
and a payout support reversed is not settling. `GET /api/wallet/balance/`
gains `available` (`balance - settling`), `settling`, `settling_until` (when the next
settling payout becomes available, ISO 8601, or null) and `in_withdrawal` (the open
withdrawal's money, already out of `balance`); for a wallet no payout reached,
`available` is the balance. Every `GET /api/wallet/transactions/` row gains
`available_at` and `settling`: on a `trainer_fee_payout` or `routine_product_sale` row
read by the trainer, when its money becomes withdrawable and whether that is still ahead; `null` / `false` on
every other row. Two new `kind`s: `withdrawal` (out, to payout clearing) and
`withdrawal_returned` (in, a failed or cancelled withdrawal).

Pushes (§4): `withdrawal_requested`, `withdrawal_paid`, `withdrawal_failed`. The spec's
table also lists email for these; there is no email notification channel in this
codebase, so none is sent.

### The operator's phone (BACKEND_BUGS B-119, B-120)

`16-agent-operator/OperatorPhone`: what is waiting in the console, and a link that opens
the console on a computer. Everything else operators do stays on the web console
(`/dj-admin/`). Console operators only — an active staff account that is a superuser or
has `user_type` `admin`, exactly who the console's queue pages let in; anyone else is
403 `admins_only`. The views' docstrings (`admin_dashboard/ops_api.py`) are the full
contract. Identical on every build. New: one table (`admin_dashboard/0001`,
`ConsoleLogin`), two settings (`OPS_QR_LOGIN_ENABLED`, `OPS_CONSOLE_BASE_URL`).

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/ops/queue-counts/` | auth (console operator) | `QueueCountsView` |
| `POST` | `/api/v1/ops/console-login/` | auth (console operator) | `ConsoleLoginView` |
| `GET` | `/api/v1/ops/console-login/{id}/` | auth (console operator) | `ConsoleLoginDetailView` |
| `POST` | `/api/v1/ops/console-login/{id}/approve/` | auth (console operator) | `ConsoleLoginApproveView` |
| `POST` | `/api/v1/ops/console-login/{id}/cancel/` | auth (console operator) | `ConsoleLoginCancelView` |

**The four queues** (B-119). `queue-counts/` answers `{queues, total, console_url}`;
`queues` is four `{key, count, console_url}` in the design's order: `transfer_reviews`
(open transfer reviews, «دفعات بانتظار المطابقة»), `trainer_verifications` (applications
`pending`), `community_reports` (reports `open` or `reviewing`), `withdrawals`
(`requested`, `approved` or `sent`). `console_url` is the absolute url of that queue's
console page (`OPS_CONSOLE_BASE_URL` when set, else this server's host). One COUNT per
queue; live, not cached.

**Signing in to the console from the phone** (B-120). **Off until the owner switches
`OPS_QR_LOGIN_ENABLED` on**: while off, all four `console-login` routes answer 403
`console_login_disabled` and write nothing. The design draws the QR on the phone, so the
phone makes the link — and the link alone signs nobody in:

1. `POST console-login/` → 201 `{id, state: "issued", url, expires_at, expires_in: 120,
   …}`. Draw `url` as the QR (or offer to mail it). Two minutes, one computer; the server
   keeps only a hash of its secret; a new link cancels the caller's earlier one. 403
   `email_not_verified` for an operator whose address the OTP flow never verified; 429
   `throttled` past 5 links in 10 minutes (`retry_after_seconds`).
2. A computer opens `url` (`/dj-admin/qr-login/…/`), presses Continue, and shows a
   three-digit number. The link is then bound to that browser; no other can open it.
3. The phone polls `GET console-login/{id}/` (every 2 s): `state` `issued` → `claimed`
   (with `computer: {opened_at, user_agent}`). The operator types the number:
   `POST …/approve/` `{"code": "472"}` → `approved`. 400 `console_login_code_required`,
   400 `console_login_code_mismatch` (with `attempts_left`; the third wrong number
   cancels the link), 409 `console_login_not_claimed`, 409 `console_login_closed`, 410
   `console_login_expired`, 404 `console_login_not_found` (not the caller's).
4. The computer signs itself in on its next check (a console session, once) and the
   phone reads `used`. `POST …/cancel/` ("not my computer") → `cancelled`; 409
   `console_login_closed` once `used`.

States: `issued`, `claimed`, `approved`, `used`, `cancelled`, `expired`. The number is
never in an API answer.

### Trainers (design v2)

One card shape for all three (`users/trainer_views.py`): `proof` (platform-verified
numbers), `media`, and `coaching_price` + `money_type` only on a build that may show a
coaching fee. Matches carry up to three `reasons`, each `{code, text: {ar, en}}`.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/trainers/` | public | `TrainerRosterView` |
| `GET` | `/api/v1/trainers/matches/` | auth (client) | `TrainerMatchesView` |
| `GET` | `/api/v1/trainers/{id}/` | public | `TrainerDetailView` |

**The legacy public list follows the directory** (Road to 10 phase 0 review, 2026-11-23).
`GET /api/auth/trainers/public/` (public, kept for older builds) lists whom
`GET /api/v1/trainers/` lists — a trainer with a first name (S0.3, B-198) on an enabled
account shown to other people, with a confirmed address — of those accepting clients. A
nameless trainer was still on it. Every key of its row stays: `username` is now always
null (the directory never shows an account's username, and this list answers anyone),
and `client_count` is the number from 5 up, null below (the owner's privacy rule, as
for a programme's buyers). `?search=` matches first and last names only, as the
directory's does (a username search would confirm the names it no longer shows).
`trainer_hourly_rate` / `coaching_currency` are unchanged: null together on a build that
may not show a coaching fee. The signed-in client's list, `GET
/api/auth/client/available-trainers/`, follows the same listing rule (blocks still left
out) and the same `client_count` rule; its `username` and search are unchanged.

### Coaching (monthly)

Coaching is sold by the month: month 1 is held when the trainer accepts and paid out when
the first program arrives — from `routines/{id}/assign_to_client/` or
`templates/{id}/generate/`; each later month is charged from the wallet when the last one
ends. The platform keeps 20% of every month. Every client is told 3 days before a month
ends (`renewal_reminder` on `coaching/current/`, push `coaching_renewal_soon`). A month
the wallet cannot cover **freezes** the coaching (the owner, 2026-09-19): status
`frozen`, the trainer's routines stay readable and every training write on them answers
403 `coaching_frozen` with `trainer_id` (sessions, set logs, bulk logs, progress,
update_progress, bulk-complete); the push `coaching_payment_due` goes out when it freezes
and after 3, 7, 14 and 30 days. The hourly run charges again once the wallet covers the
month, and that month starts when it is paid. After a coaching ends the same writes
answer 403 `coaching_ended` (`coaching_unpaid` while support has returned the month of a
coaching that still stands). Training inside a paid month (offline sync) is always
accepted. Other pushes: `coaching_renewed`, `coaching_finished` (the trainer stopped at
the month's end, or an account was suspended), and the money of each month to both
people: `coaching_fee_held` (month 1 into escrow), `coaching_fee_released` (out of escrow
to the trainer; on a renewal to the trainer only) and `coaching_fee_refunded` (a held
month back to the client) — §4. A trainer cannot end a paid month the
client is in (`trainer/unassign-client/` answers 409 `coaching_month_active`;
`at_period_end: true` stops the renewal instead), and cannot delete the account while
coaching anyone (`DELETE privacy/erase/` answers 409 `trainer_has_active_clients`).

**«لا تجدّد بعد هذا الشهر» (Road to 10 S2.7, the owner's D12).** The client switches it with
`POST /api/v1/coaching/renewal/` `{"renew": false}` and back with `{"renew": true}`
(repeats are harmless; the answer is the `coaching/current/` object). When the paid month
ends the coaching ends instead of renewing: nothing charged, nothing held, the month
already paid stays the trainer's; both get `coaching_finished`. The 3-day reminder is
`coaching_ending_soon` instead of `coaching_renewal_soon`. `coaching/current/` carries
`stop_renewal`, `ends_at` and `ending_reminder`; the trainer reads the same object (no
price) as `coaching` on `GET /api/auth/trainer/client-profile/{id}/`. `renew: true` is
refused where the build may not pay for coaching (403 `not_available_on_channel`);
`renew: false` never is; on a `frozen` coaching it is accepted and the next hourly run
ends it, nothing charged. Codes: `renew_required`, `no_coaching`, `nothing_to_renew` (a
coaching that never renews: free, sold as one fee before months existed, or `unpaid`).

**Answering a coaching request.** `trainer/respond-to-request/` and
`client/respond-to-invite/` both take `{request_id, action: "approve"|"reject"}` and
refuse with the same codes, because both go through the same two helpers:

| `code` | Status | When |
|---|---|---|
| `request_id_required` | 400 | No `request_id` in the body (`trainer/respond-to-request/`; the client side answers `request_not_found` instead) |
| `invalid_action` | 400 | `action` is neither `approve` nor `reject` |
| `request_not_found` | 404 | No pending request or invitation of yours by that id |
| `request_predates_consent` | 409 | Sent before `initiated_by` existed — ask for a new one |
| `wrong_party_to_answer` | 403 | You sent this one; the other side accepts it |
| `request_already_answered` | 409 | Answered while you were answering it |
| `trainer_unavailable` | 409 | The trainer was deactivated in the meantime |
| `user_blocked` | 403 | One of you blocked the other after it was sent |
| `refund_failed` | 409 | The hold could not be returned; nothing was changed |
| `insufficient_balance` | 402 | Approving: the client's wallet does not cover month 1 (in the client's currency) |
| `exchange_rate_stale` / `exchange_rate_missing` | 409 | Approving: the trainer prices in the other currency and the operator's rate is too old / was never set — the same body a checkout answers |
| `currency_mismatch` | 409 | Approving: the charge could not be moved in the client's currency |
| `client_has_other_trainer` | 400 | The client is already another trainer's (approving only) |
| `hold_failed` | 402 | Approving: anything else; nothing was changed, try again. The 402 used to carry no `code` and the internal reason in `error` (SIDE_BUGS S-085) |

`client/request-trainer/` checks the client's wallet against month 1 IN THE CLIENT'S
CURRENCY (converted from the trainer's at the operator's rate): its 402
`insufficient_balance` carries `required`, `balance` and `currency`, and it answers 409
`exchange_rate_stale` / `exchange_rate_missing` when the trainer prices in the other
currency and the rate cannot convert it (SIDE_BUGS S-084).

A `request_id` that is not a number answers `request_not_found` on both routes.

Templates: a template is reused for any number of clients and every client gets a
private copy (`templates/{id}/generate/`: `client_id`, or `client_ids` for several, or
`assign: false` for an unassigned draft to personalise and then `assign_to_client`).
`routines/?source_template=<id>` lists the copies made from one template.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/coaching/current/` | auth (client) | `CurrentCoachingView` |
| `POST` | `/api/v1/coaching/renewal/` | auth (client) | `CoachingRenewalView` |

### Safety: reporting, blocking, AI consent

Identical on every `X-App-Channel` — a store reviewer tries these on the store build.
All authenticated.

| Method | Path | Auth | View |
|---|---|---|---|
| `POST` | `/api/v1/reports/` | auth | `ReportCreateView` |
| `GET` | `/api/v1/reports/mine/` | auth | `MyReportsView` |
| `GET, POST` | `/api/v1/blocks/` | auth | `BlockListCreateView` |
| `DELETE` | `/api/v1/blocks/{user_id}/` | auth | `BlockDeleteView` |
| `GET, POST` | `/api/v1/ai-consent/` | auth | `AIConsentView` |

**Reporting.** `POST reports/` takes `{target_type, target_id, reason, note?}` and
answers `201 {id, status}`. `target_type` is `user`, `chat_message`, `routine`,
`diet_plan`, `exercise`, `social_post`, `comment`, `coach_message` or `challenge`;
`reason` is `spam`, `harassment`, `nudity`, `violence`, `scam`, `impersonation` or
`other`; `note` is the reporter's own words, 1000 characters at most.
`chat_message` is a message in a conversation with the AI coach; `coach_message` is a
message in the trainer–client chat, reportable by the two people in that conversation
(also after its sender deleted it); `challenge` is a community challenge, authored by
its creator.

| `code` | Status | When |
|---|---|---|
| `target_not_found` | 404 | No such target, **or** the reporter may not see it — one answer for both, so the route cannot be used to discover which private routines, plans and messages exist |
| `cannot_report_yourself` | 403 | Your own account, or something you wrote. Content generated FOR you (your diet plan, the AI's replies) is not yours and stays reportable |
| `already_reported` | 409 | You already have an open report on this target. Reportable again once it has been handled |

`GET reports/mine/` pages `{id, target_type, target_id, reason, note, status,
resolution_note, handled_at, created_at}`, newest first. `status` is `open`,
`reviewing`, `actioned` or `dismissed`. Who handled it is never returned.

**Blocking.** `POST blocks/` `{user_id}` → `201 {user_id, blocked: true}`, idempotent;
`cannot_block_yourself` (400) and `user_not_found` (404). `DELETE blocks/{user_id}/` →
204, also when there was no block. `GET blocks/` pages `{user_id, username,
created_at}` — only the blocks this person MADE; one made against them is never
disclosed.

The effect is symmetric and immediate. Neither account appears in the other's post
list, feed, post search, comment lists, follower and following lists, notifications,
public profiles, the available-trainers list or trainer matches; neither can send the
other a trainer request (`user_blocked`, 403, on `client/request-trainer/` and
`trainer/assign-client/`), and a request sent before the block cannot be accepted
after it (same code on `trainer/respond-to-request/` and `client/respond-to-invite/`).
In the chat neither can start a thread with the other (404) or write in an existing one
(403 `blocked`, and the row reads `can_send: false, refusal: "blocked"`); the thread
stays readable. `notifications/message-client/` answers 403 `user_blocked`, and so
does a client's `POST wallet/client/transfer/` to a trainer of the pair (nothing moves);
the `note` of a transfer made before the block reads null on both people's ledger rows
while the block stands (the review of wave 1, 2026-10-07).

Neither can follow the other: `user_blocked` (403) on `POST /api/social/follows/` and
`POST /api/social/follows/follow_user/`, with no push sent. A follow made before the
block is kept, hidden from both, and shows again when the block is lifted.
Blocking does not hide a challenge from either of them, but a blocked person is
dropped from its leaderboard — with the ranks left as they were, so the board still
reads 1, 2, 4. Nor does a block end or hide an approved coaching: a client who blocks
their current trainer is still in that trainer's `trainer/clients/` list until the
coaching ends.

Every count agrees with the list it summarises, for the viewer reading it:
`followers_count`, `following_count` and `posts_count` on a public profile, and
`likes_count` and `comments_count` on a post and a comment, all exclude the rows a
block hides. So two people looking at the same post can be shown different numbers,
and neither number ever exceeds what that person can actually fetch.

In the app's "report and block" flow, call `reports/` FIRST: what a blocked person
wrote is no longer visible to the reporter, and therefore no longer reportable.

**The banned-terms filter** (spec B1, 2026-09-27; additive: one new `code`, no new
route). Text a person writes for other people to read is checked against a list the
operators edit in the console (`/dj-admin/moderation/bannedterm/`), on create AND on
edit, and only the fields the request writes:

| Route | Fields checked |
|---|---|
| `POST`, `PUT`, `PATCH` `/api/social/posts/` (and `{id}/`) | `title`, `content` |
| `POST`, `PUT`, `PATCH` `/api/social/comments/` (and `{id}/`) | `content` |
| `POST`, `PUT`, `PATCH` `/api/social/challenges/` (and `{id}/`) | `title`, `description`, `rules`, `reward_description`, `unit` |
| `POST` `/api/auth/user/update/`, `/api/auth/trainer/profile/` | `first_name`, `last_name`, and a trainer's `trainer_bio`, `trainer_specializations`, `trainer_certifications` |
| `POST` `/api/auth/client/profile/` | `first_name`, `last_name` |
| `POST` `/api/auth/register/` | `username` |
| `POST` `/api/wallet/client/transfer/` | `note` (BACKEND_BUGS B-163; nothing moves) |

A refused write answers **400 `content_not_allowed`** (the top-level `code`), with every
refused field in `field_errors` carrying the same code:

```json
{"code": "content_not_allowed", "detail": "…", "error": "…",
 "field_errors": {"content": [{"message": "…", "code": "content_not_allowed"}]},
 "content": ["…"]}
```

Nothing is written: no post, no comment counter moved, no push, no account. The
message never names the word. It is checked after every other validation, so a request
that also has another mistake answers that one (`validation_error`) first. Matching
ignores case, Arabic letter forms (أ/إ/آ/ا, ة/ه, ى/ي), diacritics, tatweel, invisible
characters, repeated letters and punctuation between letters; whole word or anywhere,
per term. **Identical on every `X-App-Channel` and for every account.** Private chat is
not filtered (`chat/views.py`): it has reporting (`coach_message`) and blocking. A
change to the list takes effect at the next write, without a release.

**AI consent.** `GET ai-consent/` → `{granted, policy_version, granted_at,
revoked_at}` (a trainer gets the same answers; the AI coach is a client's plan feature,
so a trainer's privacy page offers no AI rows, decided in the app by role); never asked reads as all-null with `granted: false`, which is how the
app tells "not asked yet" from "said no" (`revoked_at` set). `POST ai-consent/`
`{granted, policy_version}` returns the same body; `policy_version` is required when
granting and ignored when revoking.

Until it is granted, the AI coach refuses: `403 ai_consent_required` on every
`/api/ai/` route except `DELETE /api/ai/data/` (deleting what the assistant holds has
to work after consent is withdrawn — that is the commonest reason to ask), `GET
/api/ai/data/` (what that deletion would remove) and `/api/ai/training-consent/`
("help us improve", a separate opt-in — see B-084 below), and over
the WebSocket `ws/ai/chat/` the reply is
`{"type": "error", "code": "ai_consent_required", "content": "..."}` with the socket
left open, so the app can show the consent sheet and carry on in the same connection.
Revoking takes effect on the very next message. On the REST routes the entitlement
check runs first, so a lapsed subscriber is still told `no_entitlement`.

### Platform policy (what each build may show and sell)

Every request carries `X-App-Channel: apk | play | ios | web` and `X-App-Version`. The
channel is a constant compiled into the build, never derived at runtime. The response is
computed from those two headers alone and is identical for every user of a build.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/config/capabilities/` | public | `CapabilitiesView` |

Response: `{channel, can_purchase_platform_sub, can_purchase_routine_product,
can_pay_coaching_in_app, can_show_prices, can_show_coaching_prices, can_show_web_hub_link,
can_view_entitlements, can_view_wallet, can_transfer_wallet, links}` — all booleans but
`channel` and `links`.

`links` (additive, 2026-09-27, spec B7) — where the public pages are, per language, so the
app opens them and they can move without a release:
`{"privacy_policy": {"ar": url, "en": url}, "terms_of_use": {...}, "account_deletion":
{...}}`, absolute URLs ending `?lang=ar|en` (see "Public pages" below). The same for every
channel and version; the base is `PUBLIC_PAGES_BASE_URL` (production refuses to boot
without an https one, B-174), else, in development, the API's own host.

The flags are enforced by the server, not only rendered by the app:

| `code` | Status | When |
|---|---|---|
| `app_channel_required` | 400 | The capabilities endpoint, or any endpoint that opens a checkout or moves wallet money, called without a known `X-App-Channel` |
| `not_available_on_channel` | 403 | `POST v1/subscriptions/` (paid plan), `POST v1/subscriptions/{id}/change-plan/` (paid plan), `GET`/`POST v1/subscriptions/{id}/upgrade/`, `POST v1/subscriptions/{id}/renew/`, `POST v1/subscriptions/{id}/pay-from-wallet/`, `POST v1/gateways/`, `POST wallet/client/transfer/`, `POST wallet/agent/topup/proxy`, and asking or accepting a trainer who charges, from a build whose flag for that kind of money is off |

On a build with `can_show_prices: false` the plan endpoints omit `price`, `subscriptions/current/`
omits `plan_price` and the four conversion keys beside it (`price_converted`,
`exchange_rate`, `price_of_record`, `price_of_record_currency` — absent, not null), and
`GET v1/gateways/` answers an empty list, with `can_pay: false` and `reason:
"not_available_on_channel"`. On a build that may sell (Road to 10 S0.5, the owner's D5,
2026-11-22) every registered gateway is listed with `available` (it can take a payment
now), `kind` — `automatic` (the server reads the merchant account and confirms the
payment itself) or `reviewed` (the customer transfers to the merchant account and a
person confirms it in the console), null when unavailable — and `review_hours` (for
`reviewed`: within how many hours it is confirmed, `PAYMENT_REVIEW_HOURS`; null
otherwise); `enabled` equals `available`. The answer's `can_pay` is true when one is
available, else `reason: "no_gateway"`. Sell only on `can_pay`, and word the promise
from `kind`.

**A plan paid from the wallet** (Road to 10 S2.13, the owner's D19 of 2026-11-22). On a
build that may sell a plan (`apk`, `web`) `GET v1/gateways/` also answers
`can_pay_from_wallet: true` and `wallet: {name: "wallet", display_name, available: true,
kind: "wallet", balance, currency, pay_path}` (the caller's balance, in their currency),
beside the gateways — never in `gateways`, and `can_pay`/`reason` still speak of the
gateways only; on the others `can_pay_from_wallet: false`, `wallet: null`. Sell a plan
when `can_pay` or `can_pay_from_wallet`. `POST v1/subscriptions/{id}/pay-from-wallet/`
`{idempotency_key, amount, currency}` (all required: the key made once per Review screen,
and the exact figure the person saw) takes the plan's price in the caller's currency
(converted at the operator's rate when the plan is priced in the other one, the rate
snapshotted) out of the wallet for the platform (100%), and the payment is `completed`
at once: method `wallet`, an invoice, the receipt, `payment_confirmed`. A first payment
starts the plan now; on an `active`/`expired` subscription it is a renewal. The same key
again answers the same payment (`replayed: true`) and moves nothing; another key within 10
minutes of a wallet payment of that subscription is 409 `already_paid` (`payment_id`,
`end_date`), so a second tap cannot take the price twice. `play`/`ios`: 403
`not_available_on_channel` (`can_purchase_platform_sub` is store-locked). Codes:
`validation_error`, `not_found`, `payment_not_required`, `subscription_active` (a trial
running), `checkout_open` (a ShamCash checkout is open), `already_paid`, `exchange_rate_stale` /
`exchange_rate_missing`, `price_changed` (`expected_amount`, `expected_currency`), 402
`insufficient_balance` (`required`, `balance`, `currency`), `purchase_refused`,
`throttled`. A refund puts the amount back in the wallet.

Additive fields:
`money_type` on payments and wallet transactions (`PLATFORM_SUB`, `COACHING_1TO1`,
`ROUTINE_PRODUCT`, `WALLET_MOVE`) and `source` on subscriptions (`shamcash`, `agent_wallet`,
`operator_grant`, `trial`, reserved: `apple_iap`, `google_play`; blank until entitled).

### Remote theme (a skin without a release)

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/config/theme/` | public | `ThemeView` |

### Support (Road to 10 S2.12)

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/config/support/` | public | `SupportView` |

`{"email": "support@yalagym.com", "whatsapp": null | "+9639..."}`: how the Help page
reaches support, the same for every build and person. `whatsapp` is null until the owner
sets `SUPPORT_WHATSAPP`. Its own route, not keys in `capabilities.links`.

Keyed on `X-App-Channel` and `X-App-Version` and on nothing else — not the account, the
country, the IP, the device's clock or an experiment bucket. Two users on one build get
byte-identical bytes.

Response: `{id, version, valid_until, tokens}`. `id` is the override's row id or `null`;
`version` is a 16-character fingerprint that changes if and only if the answer does;
`valid_until` is ISO 8601 or `null`; `tokens` maps a design-system token name to its new
value and may be empty. **No override is a 200 with `"tokens": {}`, never a 404.**

A token value is one of three shapes and nothing else: a colour is
`{"dark": "#FF5A1F", "light": "#F5511A"}` (either key optional), a spacing or radius
token is a pixel length as text (`"18px"`), a type style is a subset of `fontSize`,
`lineHeight`, `fontWeight`, `letterSpacing`. Only colour, spacing, radius and typography
tokens exist here — control sizes and shadows are not overridable, so nothing remote can
shrink a tap target. A name or shape outside that is refused when the operator saves the
row, along with any colour pair that would drop text under 4.5:1 contrast (3:1 for
`ink-faint`, `line-strong` and `focus-ring`), so the app never has to validate a payload.
The see-through backdrops text is written on are measured as they are drawn: `ink` on
`glass`, and the dark theme's `ink` on `media-veil`, must hold 4.5:1 over the darkest
and the brightest photo that can be behind them — so a veil or a glass made transparent
is refused like any other unreadable pair.

A theme carries no copy and no links, by construction: there is no field in this response
that holds a sentence or a URL. That is how the store rule — remote themes may not carry
prices, purchase buttons or payment links into `play` or `ios` builds — is met.

`ETag: "<version>"`, `Cache-Control: public, max-age=0, must-revalidate`,
`Vary: X-App-Channel, X-App-Version`. Send the tag back as `If-None-Match` and the repeat
fetch is `304 Not Modified` with no body.

| `code` | Status | When |
|---|---|---|
| `app_channel_required` | 400 | No `X-App-Channel`, or one this server does not know |

### App releases (plan step 11: the update prompt and the APK download)

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/app/latest/` | public | `LatestReleaseView` |

Both spellings answer (`/api/v1/app/latest` too). Query: `channel` (`apk | play | ios`;
defaults to `X-App-Channel`), `build` (the caller's build number; defaults to the `+N` of
`X-App-Version: 1.4.0+N` when `channel` is the caller's own). No authentication runs.

Response: `{channel, latest, build, update_available, update_required}`. `latest` is `null`
when nothing is published, else `{version_name, build_number, min_supported_build,
release_notes: {ar, en}, published_at, download_url, sha256, size_bytes, store_url}`.
`update_available` = `build < latest.build_number`, `update_required` = `build <
latest.min_supported_build`; both `null` when no build was sent, `false` when nothing is
published. Verify the file against `sha256` before installing.

**Only the direct APK is a file.** `download_url`, `sha256` and `size_bytes` are `null` on a
`play` or `ios` release, which may carry `store_url` (its store page) instead; a Play or App
Store build asking about another channel is refused, so no store build can learn where an
APK is. The hub (`web`) and the APK may ask about any channel. An uploaded APK's
`download_url` is signed and expires (an hour on object storage): fetch again rather than
keep it. `Cache-Control: public, max-age=300`, `Vary: X-App-Channel, X-App-Version`.

| `code` | Status | When |
|---|---|---|
| `app_channel_required` | 400 | No `channel` and no known `X-App-Channel` |
| `invalid_choice` | 400 | `channel` is not `apk`, `play` or `ios` (`web` has no releases) |
| `channel_mismatch` | 400 | A `play` or `ios` build asked about another channel |
| `build_invalid` | 400 | `build` is not a whole number of up to nine digits |

Operators add and publish releases in the console (`/dj-admin/releases/apprelease/`): an
uploaded APK gets its checksum and size from the server; a hosted one (the hub domain, a
CDN) is named by its HTTPS URL with the checksum and size from the build pipeline. A
published release never changes its file or build; raising its `min_supported_build`
later is how a fix becomes a required update, and unticking "published" withdraws it.

### Returning to the app (payments spec §5: Android App Links, iOS Universal Links)

JSON association files for the stores' verifiers, served at the root of the host; they
must answer on the HUB's domain over HTTPS with no redirect (a separate hub site forwards
these two paths here). No authentication runs, no header is read, the answer is the same
for everyone. `Content-Type: application/json`, `Cache-Control: public, max-age=3600`.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/.well-known/assetlinks.json` | public | `AndroidAssetLinksView` |
| `GET` | `/.well-known/apple-app-site-association` | public | `AppleAppSiteAssociationView` |

- `assetlinks.json`: `[{relation: ["delegate_permission/common.handle_all_urls"], target:
  {namespace: "android_app", package_name, sha256_cert_fingerprints: ["AB:CD:..."]}}]`,
  one statement per package in `APP_LINKS_ANDROID_PACKAGES`, each with every fingerprint
  in `APP_LINKS_ANDROID_SHA256_FINGERPRINTS` (the direct APK's signing key, and Play's
  app-signing key once there is a Play build). The paths the app opens are its manifest's
  intent filters.
- `apple-app-site-association`: `{applinks: {apps: [], details: [{appIDs: ["TEAM.bundle"],
  components: [{"/": "/app/*"}]}, {appID: "TEAM.bundle", paths: ["/app/*"]}]}}` from
  `APP_LINKS_IOS_TEAM_ID`, `APP_LINKS_IOS_BUNDLE_IDS` and `APP_LINKS_PATHS` (the second
  spelling is for iOS before 13).
- `404 app_links_not_configured` for a file whose values are not set (or are malformed,
  which is logged): no id is ever guessed.
- **The link carries no secret and no entitlement.** The hub's "Open the app" is a plain
  URL on the hub (e.g. `https://<hub>/app/payments/<payment_id>`): an id at most, never a
  token or a price. The app, opened on it, re-fetches what the customer holds
  (`payments/{id}/status/`, `subscriptions/current/`); the link grants nothing.

### Public pages (spec B6, B7): privacy policy, terms, account deletion

HTML for a browser, not JSON — opened from the app (their URLs are in capabilities
`links`), from the store listings and from the hub. No authentication runs and nothing on
them depends on who is looking. `?lang=ar|en`; Arabic (right-to-left) when absent.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/legal/privacy/` | public | `privacy_page` |
| `GET` | `/legal/terms/` | public | `terms_page` |
| `GET, POST` | `/legal/delete-account/` | public | `delete_account_page` |

- **Privacy policy, terms.** The newest published version in the language asked (the
  other language's, with a line saying so, while one is missing; 404 when neither exists).
  Operator-written and versioned in the console (`/dj-admin/legal/legaldocument/`); a
  published version never changes. `Cache-Control: public, max-age=300`, no cookie. Until
  the owner publishes the approved text, version 1 is a seeded placeholder marked DRAFT /
  مسودة on its face.
- **Account deletion.** Explains deletion in the app (Me → Privacy and data → Delete my
  account) and takes a request from someone who cannot open it: form fields `email`,
  `reason` (optional, ≤ 1000 characters), `lang`. A POST queues it for an operator and
  answers `303` to `?lang=..&sent=1` — the same answer whether or not an account uses the
  address; it deletes nothing. `400` (the page, with a sentence) for an address that is not
  one or a reason too long; `429` with `Retry-After` past 5 posts an hour from one network.
  Never cached (the form carries a CSRF token). The operator confirms ownership by email,
  erases the account through the existing erasure, and closes the request
  (`/dj-admin/legal/accountdeletionrequest/`).

### Privacy (GDPR)

| Method | Path | Auth | View |
|---|---|---|---|
| `DELETE, GET` | `/api/privacy/erase/` | auth | `PersonalDataEraseView` |
| `GET` | `/api/privacy/export/` | auth | `PersonalDataExportView` |

**Deleting an account, every role (Road to 10 R4, 2026-11-23).** A client, an agent and a
trainer who coaches nobody are deleted (`DELETE privacy/erase/?confirm=ERASE`, 200); a
trainer still coaching anyone is refused, 409 `trainer_has_active_clients` with
`active_clients`. Additive: `GET`, the 400 `confirmation_required` and the 200 each carry
`money` — `{wallet: [{currency, amount}], held_coaching_months, agent_cash: [{currency,
amount}], contact_support}`, non-zero amounts only. Money never refuses a deletion; while
`contact_support` is true the app says, before the person confirms, that the balance
cannot be spent after deletion and support returns it on request (the privacy policy).
On the 200 it is what remains (a held month is already back in `wallet`). An agent's
`agent_cash` is the platform's cash they still hold; deleting does not settle it, and a
deleted agent leaves the cash directory (`GET /api/wallet/agents/`).

The export discloses nothing the API itself withholds: its `blocks` section is `{id,
blocked, created_at}` for the blocks this person MADE, never one made against them, and
`reports_made` carries what `GET /api/v1/reports/mine/` pages, without who handled it.
An erasure also removes rows about the person that other people hold — blocks made
against them, and the chat notices they sent, which sit in the other side's inbox with
their name and words — and never counts them in the preview or the report.

Both are bounded however long the person has used the app (2026-09-27): rows are read,
deleted and anonymised a page at a time, and a large export is streamed (the same JSON
document and headers; read the body to its end). The stored files of an erasure leave
storage shortly AFTER it answers, through a background task — a photo URL signed before
the erasure may still load for a few moments.

### Routines & Workouts

**The free first workout (Road to 10 S2.4, the owner's D10).** `GET
/api/routine/trial-workout/` answers the object the home section `trial_workout`
carries; `POST` writes the client's OWN copy of the platform's workout (a private
one-day routine, `trial: true`, never assigned) and answers the same object with
`routine_id` set — 201 the first time, 200 after (one copy per person). Then it is
trained through the ordinary routes (`POST workout-sessions/ {routine: routine_id,
day: 1}`, sets, finishing the session) and kept in history. Refusals on both: 403
`not_a_client`; 404 `trial_not_offered` with `reason` `has_coach` (an approved
coaching, frozen or unpaid included), `has_program` (a routine is assigned) or
`unavailable` (the server has no trial workout). The copy has no schedule
(`routines/{id}/schedule/` is 404), is not listed by `GET routines/` (read it by id;
`trial` is additive on every routine), and its days do not count towards the streak or
the week tile. Free on every build. A new server gets the two platform workouts
(`trial-home`, `trial-gym`) from `manage.py bootstrap_platform`.

**Starter templates for trainers (Road to 10 S2.10, the owner's D16).** No route is new.
`GET /api/routine/templates/public_templates/` now lists the PLATFORM's starter
templates: eight, one per goal (`build_muscle`, `strength`, `fat_loss`, `rehab`) and
place (`home`, `gym`), named in Arabic and English, three training days each, with
every row in `exercises`. Additive on every template row: `place` (`home` | `gym` | `""`,
writable; anything else 400 `validation_error`, field code `bad_place`) and
`by_platform` (read-only; true on the platform's, whose `created_by` is null). Additive
list filters: `?place=home|gym` and `?by_platform=true|false`. `POST
templates/{id}/copy/` copies a starter into the trainer's own private library, keeping
the starter's name (no "Copy of") and its `place`. A trainer's own template cannot be
made public: `POST`, `PATCH` or `PUT templates/` turning `is_public` on answers 400
`template_cannot_be_public` with `field: "is_public"`. A new server gets the starters
from `manage.py bootstrap_platform`. Nothing differs by build.

**What each training day planned against what was done** (the owner, 2026-09-20). Closing
a day writes one `DayOutcome`, whichever way it closed: `update_progress`
(`completed` / `skipped`, and `confirmed_unlogged` when the person confirmed the
`unlogged_sets` warning), `user-exercise-progress/bulk-complete/`, finishing a workout
session on that day, the recalculation completing it by itself, or the nightly sweep for
a day opened and never closed. The plan is frozen at that moment, so a later edit by the
coach never rewrites last week; sets synced afterwards still count.

- `POST routines/{id}/update_progress/` answers `outcome` (planned, logged and missed
  sets and reps, `complete`, `confirmed_unlogged`).
- `GET routine-progress/` carries `outcome` per day (null while the day is open).
- `GET analytics/adherence/?period=week|month|year&user_id=` — the window's
  `days_closed`, `perfect_days`, `short_days`, `skipped_days`, `sets`, `reps`,
  `volume_kg`, `perfect_streak`, `last_break`, `approximate`, `by_day`. 400 `bad_period`.
- `GET v1/analytics/recent-progress/` (the home week) carries the same block as
  `adherence`.
- `GET analytics/streaks/` counts only days with nothing missing: a day closed short or
  skipped BREAKS the streak, and `last_break` names it (`date`, `routine_id`, `day`,
  `missed_sets`, `reason`). Days closed before outcomes existed fall back to their status
  and set `approximate`.
- The trainer sees the same numbers: `trainer/client-progress/{id}/` `weekly_stats` and
  `trainer/client-progress/recent/` `this_week` gain `planned_sets`, `logged_sets`,
  `missed_sets`, `set_completion_percent`, `perfect_days`.

**The checkout, and what closes it** (plan step 5, 2026-09-20).

- `auto_renew` is READ-ONLY. It is still answered on every subscription (the app reads
  it), and a `PATCH /api/subscription/v1/subscriptions/{id}/` that carries it answers
  400 `auto_renew_read_only` — refused, not accepted and dropped. Nothing charges a plan
  automatically; cancelling is on the subscription itself.
- A checkout nobody finishes is CLOSED by the server after
  `PAYMENT_CHECKOUT_TTL_MINUTES` (60) and the customer is told
  (`checkout_expired`), so they can open a new one — a subscription has at most one open
  checkout, and a lost webhook used to lock a customer out of buying for ever. Money that
  arrives for a closed checkout is still honoured (`expired -> completed`).
- The server polls the gateway every minute for checkouts it has not been told about
  (`subscription.poll_open_payments`), after leaving the webhook
  `PAYMENT_POLL_GRACE_SECONDS` to arrive first. The app does not need to poll; it may
  still call `POST /api/subscription/v1/payments/{id}/reconcile/` when the customer asks.

**The checkout, corrected** (SIDE_BUGS S-075, S-077, S-131, S-134..S-137, 2026-09-23).
All additive: no shipped `code` changed.

- `POST /api/subscription/v1/subscriptions/` writes the subscription and **no payment**.
  It used to write a pending payment at the plan's price in the PLAN'S currency, with no
  reference, which nothing could complete. The checkout is `POST v1/gateways/`.
- A checkout's window (`expires_at`, and when the poller closes it) starts when the
  checkout is OPENED — also when it takes over a reference-less row written earlier.
- Money for a CLOSED checkout is looked for without a webhook: every 10 minutes for
  `PAYMENT_LATE_MONEY_WINDOW_HOURS` (72) the server asks the gateway about each expired
  checkout that has a reference (`subscription.poll_expired_checkouts`) and completes
  the ones that were paid. The app does nothing; the subscription simply becomes active.
- The poller's `checkout_expired` inbox row is written whatever the customer's push
  preferences say; the preference decides only whether it is pushed.
- `PATCH /api/subscription/v1/subscriptions/{id}/` with `auto_renew` in the body now
  answers `auto_renew_read_only` as the TOP-LEVEL `code` (it was only inside
  `field_errors`, under a top-level `validation_error`). `field_errors.auto_renew` is kept.
- `POST /api/subscription/v1/gateways/` and `POST .../subscriptions/{id}/renew/` answer
  `amount` and `currency`: what the returned reference charges, in the buyer's currency —
  for a checkout that was already open, the amount it was opened at, which after a rate
  change is not today's figure. Draw these on the Pay screen.
- `POST v1/gateways/`'s optional `amount`/`currency` assertion is checked against what
  the call will charge — the open checkout's amount when there is one — and a mismatch is
  `409 price_changed` with `expected_amount`/`expected_currency`.
- Codes added to refusals that had none:

  | Endpoint | Status | `code` | When |
  |---|---|---|---|
  | `POST v1/gateways/` | 400 | `missing_fields` | `gateway` or `subscription_id` missing |
  | `POST v1/gateways/` | 404 | `not_found` | no such subscription, or not the caller's |
  | `POST v1/gateways/` | 400 | `payment_not_required` | the plan is free |
  | `POST v1/gateways/` | 400 | `invalid_amount` | the quoted `amount` is not a number |
  | `POST v1/gateways/` | 409 | `price_changed` | the quote is not what the call would charge |
  | `POST v1/gateways/` | 409 | `subscription_active` | cover is running; renew instead |
  | both | 503 | `currency_not_chargeable` | the gateway takes no payments in the buyer's currency (today: every USD buyer, until the owner sets ShamCash's USD bounds) |
  | both | 400 | `gateway_error` | the gateway refused or failed to open the checkout |
  | `.../renew/` | 400 | `not_renewable` | the subscription is not `active` or `expired` |
  | both | 500 | `server_error` | unexpected fault |

  The full table for each endpoint is in its docstring (`subscription/views.py`
  `PaymentGatewayView.post`, `SubscriptionViewSet.renew`).

**Receipts, and the money screens** (plan step 5, 2026-09-20 — `screens/07-money/`).

Everything in this block is a RECORD OF MONEY THAT HAS ALREADY MOVED, not a sale: no
price for something unbought, no purchase button, no payment instruction, no hub link.
It therefore takes no `X-App-Channel`, is not gated by `require_purchase_allowed`, and
answers **identically on `apk`, `play`, `ios` and `web`** — spec §3 lets a store build
show plan status, expiry and history, and history without receipts is not history.

- `GET /api/subscription/v1/payments/{id}/receipt/` — one completed payment, as data:
  `{invoice_number, issued_at, status, item{plan_id, plan_name, plan_type,
  duration_days, money_type}, amount, currency, method{code, name}, reference,
  covers{from, until}, payer{id, name, email}, platform{name, legal_name,
  contact_email, website}, pdf_url}`. The invoice number is minted once by
  `PaymentService` and never reused, so two reads are the same document.
  `?format=pdf` returns the same receipt as an A5 PDF, written in the language of
  `Accept-Language`; Arabic is the primary one. Errors: `404 not_found` (no such
  payment, or someone else's — the same answer either way) and
  `404 receipt_not_available` (the payment is not `completed`). The payer and a
  superuser, nobody else.
- `GET /api/subscription/v1/payments/` rows gain `item` (what was bought, in pieces),
  `completed_at`, `reference` and `receipt_available` — the "مدفوعات سابقة" list.
- `GET /api/subscription/v1/usage/today/` — `{quotas: [{feature, used, limit,
  resets_at}]}` for `daily_meals` and, on a plan with the AI coach, `ai_messages`
  (the same counter the WebSocket enforces). `limit: 0` is unlimited; no subscription
  is `{quotas: []}`, not an error. A pure read: it creates no usage row.
- `subscriptions/current/` gains `plan_duration_days` (what a renewal adds, and the
  denominator of the days-left gauge — `start_date` cannot serve, a renewal moves only
  `end_date`), `plan_currency` and `plan_features` (`diet`, `routines`, `challenges`,
  `ai_advice`, `priority_support`, `max_meals_per_day`, `max_routines`). `plan_price`
  and its four conversion keys (`price_converted`, `exchange_rate`, `price_of_record`,
  `price_of_record_currency`) are withheld from a build that may not show prices;
  `plan_currency` is the READER'S currency and stays on every build.
- `subscriptions/current/` answers **404 `no_subscription`** when the account has never
  had one — show the plans rather than an error. The body kept its `message`, which
  shipped without a `code`; branch on the `code` (§2), never on the text.
- The plan endpoints gain `currency` and `is_featured` (the "الأكثر اختياراً" badge an
  operator sets). `price` is still withheld from `play`/`ios`; a currency code and a
  badge are not prices.
- `GET /api/wallet/balance/` gains `held` (money in escrow for a coaching month — out
  of the balance, not spent), `held_for` `{trainer_id, trainer_name}` or null, and
  `account_number` ("YG-204-781"), which is what the client reads out to an agent. The
  agent top-up endpoints accept that number as `client_identifier`, alongside the user
  id and the email.
- `GET /api/wallet/transactions/` rows gain `kind`, `reason`, `direction` (`in`/`out`),
  `signed_amount` and `counterparty` `{id, name, role}`. **Branch on `kind`, never on
  `reason`** — `reason` is translated. `kind` is one of `trainer_fee_hold`,
  `trainer_fee_refund`, `trainer_fee_payout`, `platform_commission`,
  `plan_trainer_share`, `plan_payment`, `plan_credit`, `agent_topup`,
  `trainer_transfer`, `transfer_received`, `reversal`, `adjustment`, `topup`,
  `transfer`. The counterparty of a coaching row is the TRAINER, never the escrow
  wallet the money passed through.
- `GET /api/wallet/transactions/{reference_id}/` — one row, same shape.
  `404 not_found` for a reference that is not the caller's.
- **A row names only what it already names to you** (BACKEND_BUGS B-184, both routes;
  every key stays): `actor` is your own id or the `counterparty`'s, else null;
  `source_wallet` / `destination_wallet` is your wallet's id on your side and null on
  the other; `source_owner` / `destination_owner` on the other side is the
  `counterparty`, or null — never the platform's escrow, revenue or payout-clearing
  account, never a program's buyer; `metadata` carries only `title`, `product_title`,
  `routine_name`, `product_id`, `month`, `withdrawal_id`, `withdrawal_number` and
  `original_reference` when the row has them (`wallet.activity.READER_METADATA_KEYS`)
  — no other account's id, no `purpose` (read `kind`), no operator's `reason`.

#### Two currencies (2026-09-21)

The platform trades in **SYP and USD**, both first class, with an exchange rate an
operator maintains in the console. Four things the app needs:

- **A person has ONE currency and it is their wallet's.** `GET`/`PUT
  `/api/wallet/currency/` is the only place it lives: `{currency, currency_locked,
  currency_options}`. `GET /api/wallet/balance/` answers the same three keys beside the
  balance, so a screen that already reads the balance needs no second call. `PUT`
  `{"currency": "SYP"|"USD"}` is allowed only while the wallet is untouched — no
  balance and not one ledger leg; after that it is `409 currency_locked`. Other codes:
  `400 currency_required`, `400 currency_unsupported`.
- **Every price the app is shown is already in the reader's currency.** The plan
  endpoints' `price`/`currency` are the plan's price of record converted for this
  reader, with `price_converted` (bool), `exchange_rate` (string or null) and — only
  when converted — `price_of_record` and `price_of_record_currency`. **The app must
  never convert anything itself and must never show two numbers.** All six are withheld
  together from a build that may not show prices.
- **A checkout refuses rather than guesses.** `POST
  /api/subscription/v1/gateways/` and `POST /api/subscription/v1/subscriptions/{id}/renew/`
  answer `409` with
  `code: "exchange_rate_stale"` or `"exchange_rate_missing"` when the plan is priced in
  the other currency and the operator's rate is missing or too old. A SAME-currency
  purchase never consults the rate and is never refused for this. They answer
  `503 currency_not_chargeable` when the gateway has no amount bounds for the buyer's
  currency: ShamCash has SYP bounds only, so **every USD checkout is refused** until
  `SHAMCASH_USD_MIN_AMOUNT` and `SHAMCASH_USD_MAX_AMOUNT` are set (the owner's numbers;
  none is assumed). A new exchange rate, or an edited `ChannelCapabilities` row, reaches
  the cached plan list at once.
- **A coaching price is in the client's currency too.** `GET /api/v1/coaching/current/`
  gains `coaching_currency` beside `coaching_price`; the price is what the client will
  actually be charged, and both are null together when the rate needed to quote it is
  missing or stale.
- **Every trainer price on a list carries its unit, in the reader's currency**
  (SIDE_BUGS S-041). `trainer_hourly_rate` on `trainers/public/`,
  `client/available-trainers/` and `client/request-status/` gains `coaching_currency`;
  `agreed_rate` on `client/request-status/` and `client/trainer-invites/` gains
  `agreed_currency`; a v2 trainer card (`v1/trainers/`, `v1/trainers/{id}/`, matches,
  the first-day home) gains `coaching_currency` beside `coaching_price`. For a
  signed-in reader the number is converted into THEIR currency; for nobody signed in
  (`v1/trainers/`, `trainers/public/` without a token) it is the trainer's price of
  record; and when the operator's rate cannot convert it, it is the price of record
  too — the currency key always names the unit, so read them together. The legacy
  lists keep sending JSON numbers; the card keeps its string. Null (legacy) or absent
  (card) together on a build that may not show a coaching fee.
- **A top-up names its currency.** `POST wallet/agent/topup/` and `.../topup/proxy`
  REQUIRE `currency` (`SYP` | `USD`, the currency of the cash); the signed message of
  `agent/topup/` is `client_identifier|amount|timestamp|idempotency_key|currency`. The
  answer gains `currency`. A client who trades in the other currency is `409
  currency_mismatch` with `client_currency` and `topup_currency` (arriving money never
  chooses a person's currency). The agent's caps are per currency: `daily_limit` /
  `monthly_limit` cap pounds, `daily_limit_usd` / `monthly_limit_usd` cap dollars, and
  `0` means none (SIDE_BUGS S-059).
- **A transfer converts nothing.** `POST wallet/client/transfer/` between two people in
  different currencies is `409 currency_mismatch` with `client_currency` and
  `trainer_currency`; a short wallet is `402 insufficient_balance` with `currency`
  (it was a bare 400; SIDE_BUGS S-160).
- **A transfer to a trainer is a coaching payment, with a note** (BACKEND_BUGS B-163,
  the owner 2026-09-28). `POST wallet/client/transfer/` takes an optional `note` (at
  most 140 characters; a longer one is `400 validation_error`, a banned term `400
  content_not_allowed`, and nothing moves; a different note under the same
  `idempotency_key` is `422 idempotency_key_reused`). The client pays `amount`; the
  platform keeps the coaching commission (`COACHING_COMMISSION_RATE`, 20%, copied onto
  the transfer) and the trainer receives the rest at once — «تأخذ المنصة عمولتها منه
  لا منك». Written as a coaching payout: client -> escrow, escrow -> trainer, escrow ->
  platform, all `money_type` `COACHING_1TO1` (was `WALLET_MOVE`). The client's ledger
  still shows one `trainer_transfer` row of `amount`, whose `reference_id` the transfer
  answers; the trainer's one `transfer_received` row, of their share (not a settling
  kind: withdrawable at once). Both rows of `wallet/transactions/` and `/{reference_id}/`
  carry `note` (null without one; null while either of the two has blocked the other;
  null on every other row). The note is kept beside the ledger, never in it, and is
  erased with either person's account. A pair of whom either has blocked the other
  cannot transfer: `403 user_blocked`, nothing moves. The 200's `trainer_balance` is
  what THIS payment gave the trainer (the `amount` of their `transfer_received` row),
  never the trainer's wallet balance, which it was until the review of wave 1
  (2026-10-07); the key is kept. The build must
  allow both `WALLET_MOVE` and `COACHING_1TO1` (`403 not_available_on_channel`; every
  default build allows both or neither).
- **`PUT /api/wallet/currency/`** is also refused, `409
  currency_locked_open_checkout` with `currency`, while money priced in the current
  currency is on its way: a plan checkout still open or completed and not yet paid
  out, a coaching request or invitation still pending, or a coaching hold (SIDE_BUGS
  S-159).
- **Support.** `POST wallet/admin/reversal/` refuses one leg of a currency exchange,
  `409 exchange_leg` with `exchange_reference` (SIDE_BUGS S-161), and any leg of a
  trainer's withdrawal, `409 withdrawal_leg` with `withdrawal` (the payouts review,
  2026-09-24), and any leg of a routine product's sale or its refund — the checkout's
  mint into escrow, the wallet sale's charge, the trainer's share, the commission —
  `409 routine_product_leg` with `purchase` `{id, status, paid_with}` (null if the
  purchase is gone; SIDE_BUGS S-237). Undoing a sale is the purchase's refund, or its
  payment's for a checkout, which reverses every leg together. A client's payment to a
  trainer (B-163) is reversed WHOLE by its `reference_id` (the client's leg): the
  trainer's share and the commission go back to escrow and the amount to the client in
  one transaction (`400 insufficient_balance` when the trainer no longer holds the
  share; nothing moves); the trainer's leg or the commission alone is `409
  coaching_transfer_leg` with `transfer_reference`. The one ledger row of a plan paid
  from the wallet (Road to 10 S2.13) is `409 plan_payment_leg` with `payment_id`: refund
  the payment instead, which also takes back the cover.
  `GET wallet/admin/alerts/suspicious/` judges `large_topups` per currency
  (`WALLET_LARGE_TOPUP_THRESHOLDS`), each entry gaining `currency`, and counts cash
  top-ups only — never the mint leg of a plan payment (SIDE_BUGS S-162).
- **A receipt says what was converted.** `GET
  /api/subscription/v1/payments/{id}/receipt/` gains `exchange`: `null`, or
  `{price_of_record, price_of_record_currency, rate, rate_text}` read off the payment's
  own snapshot — never today's rate.
- `POST /api/subscription/v1/gateways/` and `GET
  /api/subscription/v1/payments/{id}/status/` now answer a real `expires_at`: when the
  reference stops being payable, which is what `Pay.png` counts down to. It was read
  only from the gateway's own response, and the ShamCash adapter has never set that
  key, so it was null on every checkout ever opened. Null once the checkout is no
  longer open. `status/` also answers `reference`. **The countdown is
  `PAYMENT_CHECKOUT_TTL_MINUTES` (60 minutes today) and the design draws 72 hours —
  one of the two is wrong; see the plan's open decisions.**
- `GET /api/wallet/agents/` — `{id, name, city, area, hours, phone}` per agent, for
  "وكلاء قريبون" on the TopUp screen. Only agents an operator has listed and whose
  account is active; `?city=` narrows it. Paying an agent in person is not an in-app
  purchase (spec §4: "top-up is done with an agent in person, so there is nothing to
  hide").

**The agent shell** (plan step 9; the spec, section 8 "Agents"; BACKEND_BUGS B-113..B-118,
B-124, 2026-09-24). The screens are `16-agent-operator/`. The view docstrings carry every
field and code; in short:

- **Caps per agent and per currency, fail closed** (`wallet/agents.py`): one top-up
  (`per_transaction_limit`; empty = none of its own), a local day, a calendar month, and
  the CEILING on the cash the agent holds for the platform, unsettled (empty = the
  monthly cap). Refusals `429 per_transaction_limit_exceeded`, `daily_limit_exceeded`,
  `monthly_limit_exceeded`, `receivable_ceiling_exceeded`, each with `limit`, `used`,
  `remaining`, `currency`. The defaults for a NEW agent are 50,000 / 250,000 / 5,000,000
  SYP and a 600,000 ceiling (settings `AGENT_DEFAULT_*`; the owner confirms them); dollar
  caps stay 0 until an operator sets them. A replay of a finished top-up (same
  `idempotency_key`, same body) is answered with its receipt before any cap is read.
- **The receivable**: every agent top-up posts the cash the agent now holds for the
  platform to `wallet.AgentReceivableEntry` (the spec's `agent_receivable` account; +
  top-up, - settlement, - reversed top-up), in the top-up's own transaction. Operators
  record settlements in the console (`/dj-admin/wallet/agentreceivable/`), idempotently,
  as immutable `AgentSettlement` rows.
- `GET /api/wallet/agent/me/` — the agent's profile, `status`, `can_top_up`,
  `blocked_reason`, `next_settlement_on` (null unless an operator set one — nothing
  schedules settlements) and, per currency, caps, used today / this month, counts,
  remaining, `receivable`, `receivable_ceiling`, `receivable_room`, `max_topup`,
  `blocked_reason`, `last_settlement`. Additive (2026-09-28): `currency`, the agent's
  own wallet currency, so the app picks `currencies[currency]` without reading
  `wallet/balance/` (BACKEND_BUGS B-144); `today` (YYYY-MM-DD) and `time_zone`
  ("Asia/Damascus"), the server's day and zone that the caps and `since`/`until` are
  read in (B-146). The top-level `status` is `AgentProfile.status`, the same value as
  `profile.status` (B-148). The agent ledger's, receipts' and settlements' timestamps
  and the key's `paired_at` / `last_signature_at` now carry the server's offset
  (+03:00), like `wallet/transactions/`: the same instants.
- `GET /api/wallet/agent/ledger/` — the agent's own top-ups, settlements and reversed
  top-ups, newest first (`?since=&until=&kind=&currency=`). Additive (BACKEND_BUGS
  B-147): a `reversal` row carries `reverses_reference_id`, the reference of the top-up
  it took back (its own `reference_id` is the reversal's, which has no receipt), and a
  `topup` row carries `reversed` (true once support reversed it); each is null on the
  other kinds. `GET
  /api/wallet/agent/settlements/`; `GET /api/wallet/agent/receipts/{reference_id}/` — a
  top-up this agent made (`404 not_found` for anyone else's), with `receipt_number`,
  the client, the shop, `remaining_today`, `client_notified`, `reversed`.
- Both top-up endpoints answer `receipt_number` and `remaining_today` beside
  `{reference_id, balance, currency}`, and push the client `wallet_topped_up` (§4).
- **The key**: `apikey/ensure/` answers `key_id` and `fingerprint` ("7F3A · 91C2 · 0B4E");
  `create/` and `ensure/` take an optional `device_name`; `apikey/status/` gains
  `key_id`, `fingerprint`, `device_name`, `paired_at`, `last_signature_at`,
  `revoked_at`, `topups_stopped`; `POST apikey/revoke/` deactivates the key without a
  replacement and stops the app's top-ups (`403 agent_key_revoked`) until a new key is
  paired. The signed endpoint refuses a key that is not the caller's (`401
  agent_auth_invalid`). One agent, one device (BACKEND_BUGS B-145, the owner
  2026-09-28): `ensure/` with a `device_name` other than the active key's is `409
  key_paired_elsewhere` with `device_name` (the device the key is on), `fingerprint`
  and `paired_at`, and nothing changes; `create/` moves the key on purpose.
- A top-up to a suspended or erased account answers `404 not_found`, exactly as a
  missing one (`users.visibility`).

**What wiring the money screens found** (BACKEND_BUGS B-047..B-052, 2026-09-23).

- `POST /api/subscription/v1/subscriptions/{id}/change-plan/` `{plan_id}` moves a
  subscription holding no running cover (`pending`, `expired`, `cancelled`, or past its
  end) onto another plan, and charges nothing: the next step is `POST v1/gateways/`,
  which prices the new plan. `200 {message, changed, subscription}` (`changed` false
  when it is already that plan). `400 invalid_plan`; `409 plan_change_while_active`
  (cover is running — pricing a mid-period change is the owner's decision);
  `409 checkout_open` with `payment_id` and `expires_at` (a checkout is open; a transfer
  cleared after the change would otherwise buy the new plan at the old price). Needs
  `X-App-Channel` when the new plan is paid, and answers `403 not_available_on_channel`
  on `play`/`ios` like every other plan purchase. `plan_id` is an integer as
  `POST v1/subscriptions/` takes it (`true`, `2.9`, a non-number: `invalid_plan`); a body
  that is not a JSON object is `400 validation_error`. The `subscription` it answers is
  the one `GET v1/subscriptions/{id}/` answers: the plan priced in the reader's currency,
  no price on a store build. Full table: `SubscriptionViewSet.change_plan`.
- `POST v1/gateways/` answers `409 price_changed` also when the subscription moved to
  another plan while the call ran (a change-plan from another device); `expected_amount`
  / `expected_currency` are what the plan it is on now would charge, and no checkout is
  opened.
- Every checkout records the plan it was priced for, with its name in every language and
  the period sold. `item` on a payment row and on the receipt names THAT plan, in the
  reader's language, so earlier payments keep naming what they bought after a plan change
  or a rename; a cancellation row names the plan that was cancelled. Completion grants the
  period that was sold, so the receipt and the cover agree.
- A transfer that clears late against an expired checkout, after a plan change: with
  nothing running it grants the plan it paid for; with paid cover running, the
  subscription holds the dearest plan among the paid periods still running (by the plans'
  prices of record), and a refund re-decides it the same way.
- A frozen subscription reads its diet plans if its plan has diet OR the last plan it paid
  for did — choosing a plan without diet, which buys nothing, no longer takes the read
  away.
- `payments/{id}/reconcile/` on a payment already completed answers `payment_id` too.
- `payments/{id}/status/` and `payments/{id}/reconcile/` answer a `code` on every
  refusal: `404 not_found`, `400 no_gateway_reference`,
  `502 gateway_unreachable`, `400 reconcile_rejected` (B-048).
- `payments/{id}/receipt/?format=pdf` answers its 404s (`not_found`,
  `receipt_not_available`) as JSON with `Content-Type: application/json`, not as bytes
  labelled PDF (B-049).
- `subscriptions/current/` gains `plan_id` beside `plan_name` (B-050).
- `subscriptions/{id}/renew/` answers `expires_at`, as `POST v1/gateways/` does (B-052).

**Upgrading while a plan runs** (the owner, 2026-09-23).

- `GET /api/subscription/v1/subscriptions/{id}/upgrade/?plan_id=` — the quote, charging
  nothing: `{from_plan, to_plan, currency, new_price, credit, to_pay, days_left,
  starts: "when_paid"}`, every amount in the caller's currency. The credit is each running
  payment at what it cost × its share of the WHOLE days left ÷ the period it bought (the
  day under way is used), so `to_pay` holds for a day at a time. While an upgrade to the
  same plan is open, `GET` answers ITS quote with `payment_id`.
- `POST` the same path `{plan_id, gateway?, amount?, currency?}` opens the checkout for
  `to_pay` and answers what `POST v1/gateways/` answers plus `upgrade` (the quote it was
  struck at). When it clears the credit is re-read and the new plan starts, running for
  what the money and the credit buy — its FULL length when it clears as quoted. A refund
  takes back exactly what the refunded money bought: refunding the upgrade leaves the
  credited days on the new plan, refunding a credited payment takes back the days its
  credit became.
- Only a DEARER plan: `409 not_an_upgrade` otherwise (a cheaper plan waits until the
  period ends, then `change-plan/`). `409 no_running_cover` when nothing runs (use
  `change-plan/`, then Pay). `409 upgrade_not_payable` with `to_pay` and `minimum` when the
  credit covers the new price or leaves less than the gateway takes. Also
  `checkout_open`, `price_changed`, `exchange_rate_*`, `invalid_plan`, and the channel
  refusals. Full table: `SubscriptionViewSet.upgrade`.
- `change-plan/` keeps answering `409 plan_change_while_active` while cover runs; its
  table now points at `upgrade/`.
- The receipt of an upgrade gains `upgrade`: `{from_plan_id, from_plan_name, new_price,
  credit, days_left, granted_days, period_price, currency, late}` (null on every other
  receipt): `period_price` is what the granted days were worth (paid + credit, so
  `period_price - credit` is the amount paid), and the PDF prints the plan it replaced,
  that price and the credit. Paid as quoted within its checkout window, the quoted
  credit is honoured and the full length granted. `late: true` means
  no cover was left to credit when the money cleared: `credit` and `days_left` are null.
- `renew/` and `POST v1/gateways/` answer `409 checkout_open` (`payment_id`, `status`,
  `expires_at`) when the open checkout is an upgrade or a payment under review, instead
  of handing it back as their own.

**Money that is almost right is held for a person** (the spec, section 7; BACKEND_BUGS
B-051, 2026-09-23).

- A payment's `status` can be `needs_review`: a transfer arrived quoting its reference
  with another amount or currency, or with its amount and no reference (the only open
  checkout that amount fits). It is the design's PayMatching: it never expires (no
  `expires_at`), it still blocks a second checkout, and it ends `completed` (an operator
  approves it, or an exact transfer arrives) or `rejected` (the money goes back to its
  sender by hand). `payments/{id}/status/` gains `review`: null, or `{reason, held_at}`
  with `reason` one of `amount_differs`, `currency_differs`, `reference_missing`,
  `checkout_closed`, `duplicate_transfer`. `payments/{id}/reconcile/` can answer 200
  `status: "needs_review"`.
- Pushes, in the spec's table: `payment_needs_review` and `payment_rejected` (push and
  in-app, `data.payment_id`). No amount, no instruction to pay again.
- An approved transfer whose amount was not the checkout's is recorded at what
  arrived: the payment's `amount` and its receipt say what was received.
- Operators work the queue in the console (`/dj-admin/subscription/transferreview/`):
  approve, reject, dismiss, or attach a transfer to the checkout it was for. A sweep
  reads the merchant account every ten minutes for transfers no checkout claimed. One
  ShamCash transaction confirms one payment, once.

**Every step of a payment is on the record** (the spec, section 7; plan step 5,
2026-09-27). Additive: no route, key or `code` changed; identical on every build.

- `GET /api/subscription/v1/payments/{payment_id}/status/` and `GET
  /api/subscription/v1/payments/{id}/` gain `events`: every state the payment went
  through, oldest first, `[{from_status, to_status, at, by, backfill}]`. `from_status` is
  null for the checkout's opening. `by` is who or what moved it: `customer` (opened the
  checkout, or "check my payment"), `gateway` (a webhook), `poller` (the server's own
  look at the merchant account), `sweep` (transfers no checkout claimed), `operator` (a
  person in the console) or `system`. A payment made before this existed carries ONE
  event with `backfill: true`: the state it was in then, `from_status` null, `at` the
  best time its row gave. The list (`GET payments/`) does not carry `events`. The app
  never sees the evidence (a transfer's details are its sender's) or which operator.
- Each event is written by `PaymentService` in the same transaction as the step, and the
  database refuses to change or delete one. Operators read them, with their evidence, on
  the payment in the console (`/dj-admin/subscription/payment/{id}/change/`).
- The spec's checkout session IS the payment: its states map onto `status` as OPEN =
  `pending`, CONFIRMED = `completed`, NEEDS_REVIEW = `needs_review`, REJECTED =
  `rejected`, EXPIRED = `expired` (also `failed` / `cancelled` when a gateway says so),
  REFUNDED = `refunded`. MATCHING is never stored: a match runs inside one poll, webhook,
  reconcile or sweep and ends in the same step as `completed`, `needs_review` or no
  change. `money_type`, `amount`, `currency`, `reference` and `expires_at` are already on
  every payment read.
- Pushes, in the spec's table: `payment_confirmed` (push and inbox, `data.payment_id`,
  `data.invoice_number`: the receipt is ready) and, for a month of coaching,
  `coaching_fee_held` / `coaching_fee_released` / `coaching_fee_refunded` to BOTH people
  (`data.role` says which side the recipient is on). The fact only, on every build: no
  amount, no button, no link.

**Around the checkout: the sender's saved address, and "ShamCash is not responding"**
(the spec, sections 5 and 6; 2026-09-27). Additive: no route, key or `code` changed.

- `GET / PUT / DELETE /api/subscription/v1/payer-address/` — the public ShamCash account
  address the caller pays FROM, on their profile. `PUT {address, address_confirm}` (typed
  twice; spaces and dashes ignored, case-insensitive, 8-64 ASCII letters and digits)
  saves or replaces it; `GET` answers `{address, saved_at, updated_at}` (`address`
  grouped in fours, or all three null); `DELETE` 204 (also when none). Refusals:
  `400 payer_address_required`, `payer_address_invalid`, `payer_address_mismatch`,
  `validation_error`. Never a password, PIN, OTP or session. Identical on every build;
  encrypted at rest; exported and erased with the account.
- What it does: a transfer with the right amount and a missing or mistyped reference,
  sent FROM the address exactly one customer saved and proved (2026-09-28, below),
  holds that customer's checkout for
  review (`status: "needs_review"`, `review.reason: "reference_missing"`, the push
  `payment_needs_review`) — even when several checkouts fit the amount, and for a
  checkout closed within the last 72 hours. It never confirms a payment: only the
  reference with the exact amount does, or an operator. So the reference still goes in
  the transfer's note.
- `gateway_state` on `payments/{payment_id}/status/`, `payments/{payment_id}/reconcile/`,
  `POST v1/gateways/`, `subscriptions/{id}/renew/` and `POST subscriptions/{id}/upgrade/`:
  `{state: "ok" | "degraded", since, last_success_at}` of the gateway the payment is paid
  through. `degraded` once the server has failed to read the merchant account at least
  `PAYMENT_GATEWAY_DEGRADED_AFTER_FAILURES` (2) times in a row over at least
  `PAYMENT_GATEWAY_DEGRADED_AFTER_SECONDS` (60); `since` is when it stopped answering
  (null while `ok`); `last_success_at` the last read that answered (null before any).
  Draw "ShamCash is not responding. Your payment is safe; we will keep checking." while it
  is `degraded`: nothing is expired, failed or refused because of it, the pollers keep
  asking, and a transfer already made is found when ShamCash answers again. Operators
  see the same state in the console (`/dj-admin/subscription/gatewayhealth/`).

**Payments fix run, wave 1 (2026-09-28): product checkouts' reads, the receipt email,
payment instructions by build, a proven payer address.** No route or `code` changed.

- A routine product's web-hub checkout is its buyer's payment on every payment read
  (BACKEND_BUGS B-167): `GET /api/subscription/v1/payments/` lists it,
  `payments/{id}/` answers it with `events`, and `payments/{id}/receipt/` (JSON and
  `?format=pdf`) is the buyer's (`payer` is the buyer). Its `subscription` is null and
  its `item` adds `product_id` and `product_title` (the plan keys are null or empty:
  draw the title alone). A plan's `item` is unchanged. Its confirmation now emails the
  receipt as a plan's does.
- `payments/{id}/receipt/` gains `email: {status, sent_at}` (B-171): `sent` (the email
  with the PDF left, at `sent_at`), `queued`, `skipped`, `failed`, or `none` (never
  owed: the payer muted `payment_confirmed` email, or their address is not verified).
  Say «وصلك الإيصال بالبريد» only for `sent`. JSON only; the same on every build.
- An OPEN payment's `amount` and `reference` (a payment instruction) are ABSENT from
  `payments/{id}/status/` and from the rows of `payments/`, `payments/{id}/` and a
  subscription's `payments` for a build that may not buy its kind of money (`play` and
  `ios` for a plan or a routine product, or no `X-App-Channel`), as
  `checkout/sessions/{id}/` already withholds them (SIDE_BUGS S-239, spec section 4).
  The same money in such a row's `metadata` goes too: `metadata.product.price`, and
  `metadata.upgrade` `new_price`, `credit`, `to_pay` and `parts`.
  Completed, refunded or closed payments carry all of it on every build. A product
  checkout's `metadata.product` never carries the sale's `commission_rate`.
- The saved payer address (above) holds a checkout among several, or a closed one,
  only once it is PROVEN: a transfer FROM it has confirmed one of the customer's own
  checkouts by its reference (SIDE_BUGS S-241). Changing the address unproves it.
  Until then a match is only written down for the operator, and the transfer is held
  on the one checkout its amount fits, or queued. The signal now covers routine
  product checkouts too (S-240). `payer-address/` answers the same body as before.

**What wiring the trainer money screens found: escrow and the ledger's reads**
(BACKEND_BUGS B-096, B-101, 2026-09-24). Additive; identical on every build (money that
already moved, spec §3: balance and history are visible everywhere).

- `GET /api/wallet/balance/` gains, for a TRAINER, the other side of escrow
  (TrainerEarnings' «محجوز لدى المنصة · 3 عملاء بانتظار أول برنامج»):
  `held_for_trainer` (what the trainer will receive when each held month 1 is released
  by the first program: the client's fee less the platform's commission at the rate
  copied onto that month, in the trainer's currency), `held_for_trainer_gross` (the
  clients' fees for the same months), `awaiting_first_program` (how many clients) and
  `held_for_trainer_items`, newest first: `{relation_id, client {id, name, role},
  status: "awaiting_first_program", kind: "trainer_fee_hold", reason, month,
  requested_at, held_at, amount, commission, commission_rate ("0.2"), net, currency,
  charged_amount, charged_currency, estimated}` with `amount = commission + net`.
  `estimated` is true when the month was charged in the other currency: the trainer's
  share is converted when it is paid, so the figure is today's rate. A hold an operator
  reversed is not counted. `"0.00"`, `"0.00"`, `0`, `[]` for anyone who is not a
  trainer. The items are NOT ledger rows and never appear in `transactions/` — draw them
  as the design's «حجز رسوم تدريب» rows; nothing is written for the trainer until the
  money is released (then a `trainer_fee_payout` row arrives as before).
- `GET /api/wallet/transactions/` takes optional filters, ANDed; without them the call
  is unchanged: `since=YYYY-MM-DD` / `until=YYYY-MM-DD` (inclusive, the server's zone),
  `money_type=` (one or several of the four) and `kind=` (one or several `kind` values;
  a row matches exactly when its own `kind` is that value). Repeat a parameter or
  separate values with commas.
- `GET /api/wallet/transactions/summary/` — the same ledger summed per calendar month
  and per `money_type`, for the monthly chart and «من أين أرباح أيلول» without paging:
  `{currency, since, until, money_type, kind, months: [{month: "2026-09", in, out, net,
  count, by_money_type: {PLATFORM_SUB|COACHING_1TO1|ROUTINE_PRODUCT|WALLET_MOVE: {in,
  out, net, count, people}}}], totals: {in, out, net, count, by_money_type}}`. Every
  month the window touches is listed, oldest first, empty ones too; every amount is in
  the wallet's `currency`; `people` counts the different people the rows were with (a
  trainer's `COACHING_1TO1.people` is the design's «16 عميلاً»). Same filters; the
  window defaults to this month and the five before, and may touch at most 12 months.
- Refusals, 400, each its own code: `bad_date` (+ `field`; not `YYYY-MM-DD`, or outside
  2000..2999 — the code the diet and routine date parameters already answer),
  `date_range_reversed` (`since` after `until`), `date_range_too_long` (summary only,
  + `max_months: 12`), `bad_money_type` (+ `money_type_options`), `bad_kind`
  (+ `kind_options`; the code diet's meal events answer for an unknown `kind`).

**A withdrawal's ledger row says where the money went and whether it arrived**
(BACKEND_BUGS B-125, 2026-09-24). Additive; identical on every build. Each row of
`GET /api/wallet/transactions/` (and its detail route) gains `withdrawal`: on a
`withdrawal` / `withdrawal_returned` row read by the trainer whose withdrawal it is,
`{id, number, state, arrived, destination_kind, destination_label}` — `state` is the
withdrawal's state NOW, `arrived` is `state == "confirmed"`, `destination_kind` is
`shamcash` or `agent_cash`, `destination_label` the last four characters of the ShamCash
address (`•••• 1234`) or the agent's shop name; `null` on every other row. The
withdrawal routes' refusal tables now say the BUILD is checked before the role
(B-126): anyone on a build that may not move wallet money is answered
`not_available_on_channel`.

**What the payouts review found** (2026-09-24). Additive; identical on every build.

- `GET /api/wallet/balance/` gains, for a TRAINER, what the platform OWES them:
  `owed_to_trainer` and `owed_to_trainer_items`. A month already paid out whose
  trainer's share crosses a currency, paid while the operator's rate was missing or
  stale (month 1 at the first program, or a renewal), runs — but the share stays in
  escrow, owed, until the hourly run finds a fresh rate. It is neither held nor yet a
  ledger row, so until now it was in no figure at all. Items, newest first:
  `{period_id, relation_id, client {id, name, role}, status: "payout_owed", kind:
  "trainer_fee_payout", reason, month, owed_since, starts_at, amount, commission,
  commission_rate, net, currency, charged_amount, charged_currency, estimated}`, the
  amounts as `held_for_trainer_items` expresses them (`estimated: true` when converted
  at today's rate or taken from the price of record). When the share is paid the item
  goes and a `trainer_fee_payout` row arrives. `"0.00"` and `[]` for anyone who is not
  a trainer. `held_for_trainer`, `held_for_trainer_gross`, `awaiting_first_program` and
  `held_for_trainer_items` keep their meaning: nothing owed is counted in them.
- `POST /api/wallet/admin/reversal/` refuses any leg of a trainer's withdrawal — the
  request, the return, the payout — `409 withdrawal_leg` with `withdrawal` `{id,
  number, state}` (null if no withdrawal names the row). Undoing a withdrawal is fail
  or cancel in the payouts console.
- `POST /api/v1/trainer/withdrawals/{id}/cancel/` can answer `409
  withdrawal_hold_reversed` (+ `state`): the withdrawal's money was already put back in
  the balance outside the withdrawal, so cancelling would return it twice; nothing
  moved and support closes it. Not reachable through any endpoint now; the backstop
  for a reversal written some other way.
- The payouts console refuses approve and sent for a trainer who is no longer verified
  or whose account is suspended (the trainer is not told; the operator fails it, and
  the trainer gets the `withdrawal_failed` push with the reason, as before).

**What wiring the Me screens found** (BACKEND_BUGS B-053..B-057, 2026-09-23). Additive:
the refusals below answered only their status default (`bad_request` / `not_found`) and
now answer their own code, which §6 reads as that default; no code a view named changed.

- `GET/POST /api/auth/user/update/` — the profile gains `phone_number`, READ-ONLY: the
  number given at registration, or null when none is on file (the column's placeholder
  `0000000000`). Sent in a POST it is ignored and the rest of the request is saved, as
  every other read-only field there; a number changes through support (B-053). A
  successful POST now answers the saved profile — the same shape `GET` answers, read back
  from the database — plus `message`, which it answered alone before (B-057).
- `DELETE /api/auth/device-token/` — no `token`: 400 `token_required` with `field`; a
  token this account does not hold: 404 `token_not_found`. Both were the generic
  `bad_request` / `not_found` (B-054). The POST side answers `token_required` too since the
  owner's decision 9 (2026-09-24).
- `POST /api/auth/user/profile-picture/` without the `profile_picture` part: 400
  `profile_picture_required` with `field`, was the generic `bad_request` (B-055).
- **How large an uploaded image may be** — on every route the image guard serves
  (profile picture, chat photos, exercise images and photos, social posts, the media
  kit) — is what a 512 MB server can decode, priced from the file's header before
  anything is decoded (SIDE_BUGS S-210; one 106 KB WebP took 983 MB before). Always at
  most 10,000 px a side and 40 MP. A camera (baseline) JPEG is taken up to 40 MP, and one
  too large to decode whole — colour over about 19 MP, CMYK over 12 — is decoded and
  STORED at half (or a quarter of) its width and height: a 24 MP phone photo comes back
  2856 x 2142. Anything else is refused over its own limit: a PNG about 33 MP (grey or
  palette 40, grey with alpha 11), a WebP 6.7 MP (4.8 with transparency), a progressive
  JPEG 10 to 13 MP in colour, an animated GIF 5.6 MP of canvas for two frames and less as
  frames are added; the media kit, which decodes again to grade and turn the picture,
  about 11 MP (WebP 5.6). The codes do not change: `image_too_many_pixels` (over a
  kind's own limit its sentence names that limit in megapixels) and
  `image_dimensions_too_large`.
  `DELETE` with no picture to remove: 404 `profile_picture_not_found`, keeping
  `message` and gaining `error` / `detail`; it was `not_found`, which a routing 404 also
  is (B-056).

**What wiring the nutrition screens found** (BACKEND_BUGS B-058..B-065, 2026-09-23).
Additive under §6: every new code replaces a status default the envelope filled in
(`conflict`, `bad_request`, `permission_denied`, `not_found`), never a code a view named.
One status moves: an ABSENT `component_id` / `meal_id` on `swap_food`, `rate_component`
and `event` is now `400 missing_fields` (it reached `get_object_or_404` and answered
`404 not_found`); the app never sends that request. The views' docstrings carry the full
contract.

- **Swap offers are what the plate would hold.** `POST /api/diet/v1/client/meals/interact/`
  `swap_food` without `chosen_food_id` gains `current` (`{food_id, quantity_grams,
  calories, protein, carbs, fat, fiber}`) and, per offer, `quantity_grams` (the portion
  the swap itself would serve — one rule for both), `serving`, `serving_ar`, `image_url`
  (null when none), `calories`, `protein`, `carbs`, `fat`, `fiber` (that portion's
  fibre, null when unknown — see "Foods store fibre" below), `tag`, `tag_delta` and
  `delta`. `tag` is a code: `more_calories` | `more_protein` | `fewer_calories` |
  `more_fiber` | `similar` (first match); `tag_delta` is the kcal, protein grams or fibre
  grams behind it, null for `similar`.
- **Codes where there were none.** Swap to a food already in that meal: `409
  food_already_on_plate`. Unknown `action` on `interact/` and `meals/{id}/complete/`: `400
  invalid_action`. Both routes: `403 clients_only`, `400 missing_fields` (with `fields`),
  `400 bad_component_id` / `bad_chosen_food_id` / `bad_meal_id` / `bad_is_liked` /
  `bad_body`, and `event` with another `kind`: `400 bad_kind`. A malformed id on
  `swap_food` / `rate_component` / `event` was a 500 and an absent one a 404; both are
  400 now. `GET /api/diet/v1/plans/tasks/{task_id}/` for a task the caller did not start
  keeps answering `404 not_found`, the code it has always carried (renaming it to
  `task_not_found` is the owner's call); its docstring now says so.
- **Arabic that is Arabic.** The meal payload (every view that returns a meal) gains
  `description_ar` and `display_description`; `description` stays the English column.
  The Arabic reason no longer carries an English cuisine word or «لوجبة الوجبة الخفيفة».
  Plans generated before 2026-09-23 have no recorded Arabic description: `description_ar`
  falls back to the English, as `name_ar` and `reason_ar` do.
- **Who wrote a plan.** `GET /api/diet/v1/my/diet-plans/` rows gain `generation_strategy`
  (`ENGINE` | `TRAINER`) and `author` (`{id, name}` of the coach, or null — also once the
  coach's account is closed or erased).
- **Food tags.** `GET /api/diet/v1/food/list/` rows gain `tags`: `levantine` | `western`,
  `high_protein`, `low_fat`, `low_calorie`, from the food's own columns against the EU
  nutrition-claim thresholds (`diet/services/food_tags.py`); `high_fiber` since
  2026-09-23 (below). No vegetarian tag.
- **Foods store fibre** (the owner, 2026-09-23; BACKEND_BUGS B-058, B-065). Additive:
  `FoodItem.fiber` is grams per 100 g, from USDA nutrient 1079 for the 100 USDA foods
  (the curated Levantine foods have none yet). **Null means not known, never 0 g** — a
  food whose fibre is unknown claims nothing.
  - Swap (`client/meals/interact/` `swap_food`): `current.fiber` and each offer's `fiber`
    are that portion's grams (per-100 g figure scaled by `quantity_grams`, one decimal),
    null when the food's fibre is unknown. `more_fiber` is now emitted: the offer's
    portion carries at least `SWAP_MORE_FIBER_MIN_G` (2 g) more fibre than the current
    one, both known. It comes after `more_calories`, `more_protein` and
    `fewer_calories` and before `similar`, so it only ever replaces what was `similar`;
    `tag_delta` is the fibre grams gained.
  - `GET /api/diet/v1/food/list/` rows gain `fiber` (per 100 g, null when unknown) and
    the tag `high_fiber` (spelled like `fiber` and `more_fiber`): at least 6 g per 100 g,
    the EU "high fibre" claim; never on unknown fibre. It comes last in `tags`, after
    the existing ones, whose order does not change.
  - **No vegetarian tag, and none is coming**: the owner decided against a vegetarian
    flag (2026-09-23). The app drops «نباتي».

**What wiring the trainer nutrition screens found: the read side** (BACKEND_BUGS
B-127..B-130, 2026-09-25; screens/14-trainer-nutrition). Additive under §6: two keys on
every catalogue row, one optional query parameter set, five new routes, one new table
(`diet/0078`); no route, key or `code` changed. Prefix `/api/diet/v1/`. The views'
docstrings carry the full contract (`diet/views.py FoodListView`,
`diet/trainer_reads.py`, `diet/saved_meals.py`).

- **Who may read a client.** `trainer/clients/{client_id}/…` and the catalogue's
  `for_client` answer the client's APPROVED trainer only (the approved relation;
  an admin is not a trainer): 403 `not_a_trainer`, then 403 `not_your_client` (no
  approved coaching, or no such client — one answer), then, like the chat, 403
  `coaching_frozen` / `coaching_unpaid` / `coaching_ended` with `trainer_id` while
  `users/coaching.py` says the coaching may not be used. A trainer not yet verified:
  403 `no_entitlement` (`HasDietAccess`, as on every diet trainer route).
- **A food names its allergens** (B-127). `food/list/` rows gain `allergens` — the
  14-tag vocabulary of `diet/allergens.py`, sorted — and `allergen_source`
  (`verified` | `inferred` | `unknown`; an empty list on `unknown` is "not known").
  One function (`food_allergen_tags`) decides it for the row, the planner's filter and
  the trainer write's `declared_allergen` refusal.
- **The picker for one client** (B-127). `food/list/?for_client=<id>` adds `for_client`
  to each row: `{excluded, excluded_allergens, excluded_terms, allergen_verdict,
  liked, disliked}` — `excluded` exactly when the trainer's meal write would refuse the
  food for that client. `&hide_conflicts=true` drops excluded and disliked rows,
  `&liked_only=true` keeps the liked ones; `count` follows. 400 `bad_for_client`,
  `bad_hide_conflicts`, `bad_liked_only`, `missing_fields` (`fields: ["for_client"]`,
  a flag without it).
- **The client's tastes** (B-128). `GET trainer/clients/{client_id}/preferences/` —
  `{client_id, has_preferences, allergies, allergens, allergy_terms, liked_foods,
  disliked_foods}`: the client's own text, its tags, the words the vocabulary does not
  know, and `{id, name, name_ar, image_url}` food rows. Reading writes nothing.
  An Arabic word the vocabulary does not know («الفراولة») is one of those words too
  (fix run, 2026-10-07): it is matched against a food's Arabic name, so it can appear
  in `allergy_terms`, in the picker's `excluded_terms` and, as `free:<word>`, in the
  plan check's `declared_allergens`. No field or code changed.
- **The client's week** (B-129). `GET trainer/clients/{client_id}/adherence/
  ?start_date=` — seven `days` (Saturday to Friday by default) with the client's own
  progress figures (ticked meals, calories and protein eaten and their targets, the
  meal kinds with nothing ticked), a `summary` over the closed days
  (`adherence_percent`, `average_protein_gap_g`, `missed_meals: [{meal_type, days}]`)
  and `current_plan` (the plan governing today, `generation_strategy`, `is_yours`,
  targets). 400 `bad_date`.
- **The automatic plan to start from** (B-129). `GET trainer/clients/{client_id}/
  automatic-plan/?from=&to=` — the client's newest active `ENGINE` plan that has
  started (`covers_today` says whether it is today's), with every meal and component
  (`food_id`, `quantity_grams` — what `trainer/meals/` takes back as `food_items`); at
  most 60 meals, `truncated`. `plan: null` when there is none. 400 `bad_date`.
- **Saved meals follow the trainer** (B-130). `GET, POST trainer/saved-meals/` and
  `GET, PUT, PATCH, DELETE trainer/saved-meals/{saved_meal_id}/` — the calling trainer's
  own library, `{id, name, meal_type, items, totals, allergens, created_at,
  updated_at}`; a write takes `{name, food_items: [{food_id, quantity}], meal_type?}`,
  validated by the meal write's own code (`no_food_items`, `bad_food_items`,
  `bad_food_item`, `bad_quantity`, `duplicate_food`, `unknown_food`) plus
  `missing_fields`, `bad_name`, `bad_meal_type`, `too_many_food_items` (over 30), 409
  `saved_meals_limit` (300 kept), 404 `not_found` (none, or another trainer's), 403
  `not_a_trainer`. Exported with and erased with the trainer's account.

**What wiring the progress screens found** (BACKEND_BUGS B-066..B-073, 2026-09-24).
Additive under §6: new fields and new codes; no code a view named changed. Two values
move: `volume_change_percent` can now be null, and a sent-but-invalid `exercise_id` on
`strength/` no longer answers the overview. The views' and serializers' docstrings carry
the full contract.

- **A goal has a start** (B-066, B-069). `GET/POST /api/analytics/goals/` rows gain
  `start_value` (null when none). `POST` accepts it and stores the goal AT it in the same
  write (`current_value = start_value`, 0%); without it a finite `metadata.start_value`
  is taken. A start that already reaches the target: 400 `validation_error`, field code
  `start_reaches_target`. Read-only after creation. `update_progress/` now measures the
  share of the way from start to target in EITHER direction (82 → 75 kg at 80 is 28.6%),
  0-100, and completes the goal only when the reading reaches the target in its
  direction; a `weight_loss` goal always aims down, and without a start its first
  reading becomes the start. A goal with no start is measured from 0 upward, as before.
  Migration `analytics/0005` backfills `start_value` from `metadata.start_value`, sets a
  never-moved goal at its start, recomputes its progress, and reopens a goal the old rule
  completed short of its target.
- **Weekly goals count themselves** (B-070). A `workout_frequency` goal's
  `current_value` is the number of `completed` workout sessions begun THIS week
  (Saturday to Friday, the home strip's week), `progress_percentage` that against the
  target capped at 100, and rows gain `week_start` / `week_end` (dates; null on other
  types). It never becomes `completed`. `update_progress/` on one: 400
  `goal_counted_from_workouts`.
- **The workouts list is newest first by a total order** (B-067):
  `GET /api/routine/workout-sessions/` orders by `start_time` then `id`, descending.
- **A first window is not a doubling** (B-068): `GET /api/routine/analytics/summary/`
  `volume_change_percent` is null when the previous window holds no volume (it was
  100.0).
- **The activity log** (B-071, B-072). `GET /api/analytics/activities/recent_activities/` takes `?activity_type=`, one
  type or a comma-separated list; an unknown name is 400 `bad_activity_type` with
  `field`. Since 2026-09-24 a meal writes ONE `meal_completed` row, when its last food is
  ticked, with `metadata` `{meal_id, name, name_ar, meal_type, date, calories}`, and a
  logged set writes no `exercise_completed` row; older rows are kept as written. Every
  activity row gains `workout`: on `routine_completed` `{session_id, routine_id, day,
  duration_minutes, volume_kg, sets}` read from the session, null otherwise.
- **An exercise id that is sent is read** (B-073). `GET /api/routine/analytics/strength/?exercise_id=`
  that is not a whole number: 400 `bad_exercise_id` with `field`; 0, a negative or any
  id naming no exercise the caller may read: 404 `not_found`, as a missing id always was.

**What wiring the AI screens found** (BACKEND_BUGS B-082..B-085, 2026-09-24). Additive:
two methods on routes, one new route and one new column; no `code` changed. The views'
docstrings carry the full contract.

- **The chat list pages by last activity** (B-082). `GET /api/ai/sessions/` is ordered
  by `updated_at` (every answered turn moves it), then id, newest first — the order the
  view always asked for; the cursor pagination it shared with the messages re-sorted it
  by `created_at`. A chat continued today is on the first page. Same page size, same
  `{next, previous, results}`, same row. `sessions/{id}/messages/` is unchanged.
- **What the assistant keeps** (B-083). `GET /api/ai/data/` →
  `{total_records, details: {messages, training_records, sessions, behavior_events,
  insights, cost_records}, oldest_session_at}` (ISO 8601 with offset, null with no
  chats). Gated exactly like `DELETE` on the same route: signed in, nothing else — no
  plan, no AI consent — so AiPrivacy can show the numbers after the plan lapses or
  consent is withdrawn. The counts are the ones `DELETE` reports as `details`, from one
  function, so what it says will go is what goes.
- **"Help us improve"** (B-084). `GET /api/ai/training-consent/` → `{granted, set_at}`;
  `POST {"granted": true | false}` answers the same body. Off by default; `set_at` null
  means never answered. Only the person's POST writes it; `granted` must be a JSON
  boolean (`400 validation_error` otherwise, and on a missing `granted`). Signed in is
  all it needs. It is NOT `ai-consent/`: that one lets the coach read the person's data
  at all and is versioned; this one only decides whether a copy of each answered turn is
  kept for training. Turning it off deletes the copies already kept and stops the next
  turn from keeping one, on an open socket too. Carried in the privacy export's
  `profile` (`ai_training_consent`, `ai_training_consent_set_at`) and reset by erasure.
- **`DELETE /api/ai/data/` has a written contract** (B-085). What it deletes (the six
  `details` kinds, nothing outside the assistant), `200 {message, deleted_records,
  details}` — `deleted_records` 0 is still 200 — in one transaction; refusals `401
  not_authenticated`, `405 method_not_allowed`. Unchanged behaviour.

**What wiring the community screens found** (BACKEND_BUGS B-074..B-081, B-086, 2026-09-24).
Additive under §6: new fields and one new route; every new `code` replaces a status
default (`bad_request`, `not_found`), never a code a view named. Three statuses move, on
requests the app does not send: an absent or non-numeric `user_id` on
`follows/follow_user/` and `follows/unfollow_user/` is `400 invalid_user_id` (a 404, and
a 500 for `"abc"`); following an INACTIVE account is `404 user_not_found` (it
succeeded); a non-numeric `{id}` on `POST /api/achievements/{id}/feature/` is `404
achievement_not_earned` (a 500). The views' docstrings carry the full contract.

- **One place, one rule** (B-086). `GET /api/social/challenges/` and `{id}/`
  `user_progress.rank`, every `leaderboard/` row and the home card's `challenge_rank.rank`
  now all annotate ONE function (`social/standing.py place()`), counted live: 1 + the
  participants strictly ahead — more progress, then an earlier join, then a lower
  participation id. `user_progress.rank` read the stored column and could disagree with
  the other two. Two people on the same number still hold different places (the
  earlier joiner first): the owner's decision of 2026-09-24 — ties share a place — is
  owed its change in that one function and in the tests that pin the current rule, and
  has not been made.
- **The leaderboard** (B-074). `leaderboard/` rows' `user` gains `first_name`,
  `last_name`, `user_type` (the trainer's ring) and `profile_picture` (absolute URL or
  null); the response gains `me` — the caller's own row, same shape, whether or not it
  made the list; null when they have not joined or dropped out — and `auto_counted`.
  `?limit=` 1..50 (default 10, the old cap). Blocked accounts still leave the list after
  the places are counted.
- **Counted progress** (B-075). A challenge gains `metric` — `manual` (default) |
  `volume_kg` | `sets` | `workouts` | `training_days`, writable on create and update —
  and read-only `auto_counted` (`metric != "manual"`). An auto-counted challenge's
  `current_value` is counted from the training the participant logs inside its window:
  on `join`, after every commit that writes or deletes a set, a progress row or a
  workout session, and for everyone when its `metric` or dates change while it runs
  (`social/challenge_progress.py`). `update_progress` refuses it: `400
  progress_auto_counted`. A manual challenge works as before.
- **Challenge codes** (B-076). `join/`: `400 challenge_full`. `update_progress/`:
  `400 not_participating`, `challenge_closed` (the codes `leave`/`join` already answer),
  `progress_auto_counted`, `progress_required`, `invalid_progress`; `negative_progress`
  and `progress_out_of_range` unchanged. `leave/` unchanged.
- **Challenges that cannot be scammed** (owner's decision F20, 2026-09-24). Additive;
  no new route. The views' docstrings (`ChallengeViewSet`, `join`, `leave`,
  `leaderboard`) carry the full contract.
  - Challenge rows (list, detail, create, update) gain, read-only: `ranked` (bool —
    `auto_counted` AND the creator is staff or a VERIFIED trainer (`trainer_is_verified`,
    set by an operator: anyone may sign up as a trainer); only a ranked challenge can
    award the Challenge Winner badge or anything a first place earns; a manual,
    self-reported challenge is never ranked), `counting` (null for a manual challenge,
    else the rules the count applies: `{"max_reps_per_set": 30,
    "max_weight_kg_per_set": 400, "min_sets_per_day": 3, "max_sets_per_day": 60,
    "max_volume_kg_per_day": 30000, "min_sets_per_workout": 3,
    "min_workout_minutes": 10, "max_workouts_per_day": 2, "late_hours": 72}` —
    `social/challenge_progress.py COUNTING`), `review_hours` (72, equal to `late_hours`: how
    long after the end the standing stays open for review and for the window's
    late-synced sets) and `winner_final` (bool: ended at least
    `review_hours` ago and the winner is settled).
  - What an auto-counted challenge counts: a set with 1..30 reps and 0..400 kg; at most
    60 sets and 30 000 kg per person per day; a training day needs 3 counted sets; a
    workout needs to be completed, with 3 counted sets, lasting 10 minutes, at most 2 a
    day, and only its sets trained on the session's start day count toward it; a set
    the server received, or whose weight or reps changed, more than 72 h after its
    training day ended does not count (`ExerciseSetLog.created_at` and
    `values_changed_at`, new; older rows count). The person's own
    journal, strength charts and adherence are unchanged: only the challenge filters.
  - `user_progress` and the `leaderboard/` `me` row gain `review_state` (`"clear"` |
    `"flagged"` | `"disqualified"`) and `review_reason` (null | `"day_over_threshold"`
    — a day above 45 sets, 20 000 kg or 2 workouts, flagged automatically |
    `"operator"`). Only the person themselves sees them; `leaderboard/` also gains
    `ranked`. Flagged: listed and placed normally, but a flagged first place wins
    nothing until an operator clears it. Disqualified (an operator's decision): left
    out of every place — `leaderboard/` rows, `user_progress.rank`, the home card's
    `challenge_rank`, the stored rank; places run 1..N over the rest — their `me` row
    is served with `rank: null`, and their number is still counted.
  - Codes: `join/` and `leave/` by a disqualified person: `403
    challenge_disqualified`. `update_progress/` unchanged. `PUT/PATCH` answers `400`
    with the top-level `code` (and the field error in `field_errors`):
    `challenge_over` — `metric`, `start_date`, `end_date` or `target_value` of a
    challenge that is over (ended or closed); `end_date_in_past` — an `end_date`
    earlier than now (to end early, close it with `status: "completed"`).
  - After the end, the challenge keeps counting its own window's sets that sync late
    (a phone offline on the last day) until `late_hours` after its end; a set
    received later never counts. The winner is settled once, by the hourly phase
    task, `review_hours` (72) after the end, so no late set can move a settled
    standing: the
    standing is re-ranked, and the person at place 1 is checked for the badge if the
    challenge is ranked and they are clear and reached the target (flagged in a ranked
    challenge: wait for the operator; an unranked or cancelled challenge never waits).
    An operator's clear covers the days above a threshold up to then; a later such day
    flags the person again. A creator closing a challenge early (`status: "completed"`) moves
    its `end_date` to the moment of the close. Operators clear or disqualify from the
    console (`ChallengeParticipation`, filter by `review_state`).
  - **A winner needs 5 active participants** (the owner, 2026-09-24). Challenge rows
    (list, detail, create, update) gain, read-only: `winner_min_participants` (int, 5 —
    `social/standing.py MIN_PARTICIPANTS_FOR_WINNER`) and `active_participants` (int,
    live: participants in the standing — not dropped, not disqualified, the account
    visible — whose counted number is above zero; a joiner who never trained does not
    count). A challenge with fewer active participants at the settlement settles with
    no winner: `winner_final` still becomes true, and nobody earns the Challenge Winner
    badge (or anything a first place earns) from it, even flagged first places are not
    waited on. The settlement records the count it decided on (the server-side column
    `Challenge.settled_active_participants`, not served), and the badge engine reads that
    record, so the badge is never awarded below the minimum, and a rival erasing their
    account after the settlement does not take a win back. No new code.
- **Another member's profile** (B-077). `GET /api/social/users/public-profile/{id}/`
  (and `by_username/`) gains `featured_achievements` (at most three badges the person
  featured: `{id, key, name, category, points, icon_url, badge_color, is_rare,
  earned_at}`, `id` the achievement's; a secret badge only on your own profile),
  `community_points` (the points of every badge earned — what
  `GET /api/achievements/leaderboard/` ranks) and `member_since` (`"YYYY-MM-01"`, month
  precision on purpose). Blocked and inactive accounts are still not served.
- **Badges keyed by the badge** (B-078). `GET /api/achievements/` rows (and `{id}/`)
  gain `is_featured` for the caller (false when not earned); `GET /api/achievements/my/`
  rows gain `achievement_id` beside their own `id` (the earned row's) — the id
  `feature/` takes. `feature/`: `404 achievement_not_earned`, `400
  featured_limit_reached`.
- **Follow codes** (B-079). `follows/follow_user/`: `400 invalid_user_id`, `400
  cannot_follow_yourself`, `404 user_not_found` (also for an inactive account),
  `403 user_blocked` unchanged. `follows/unfollow_user/`: `400 invalid_user_id`, `404
  user_not_found`; it still works on an inactive account. `POST /api/social/follows/`
  refuses an inactive `following_id` like a missing one (`validation_error`).
- **The share card** (B-080). `POST/PATCH /api/social/posts/` take
  `share_workout_session` (one of YOUR finished sessions) or `share_achievement` (a badge
  you earned, `/api/achievements/` id) — write-only, null clears; with no `post_type` the
  post becomes `workout` / `achievement` (on a PATCH too; clearing the share of such a
  post makes it `text`). Refusals: `validation_error` with field code
  `share_not_yours`, `share_workout_unfinished`, `share_badge_not_earned`,
  `share_badge_secret` or `share_one_thing`. Every post gains read-only `share`: null, `{"kind": "workout",
  "workout": {title, volume_kg, minutes}}` or `{"kind": "achievement", "achievement":
  {id, key, name, category, points, icon_url, badge_color, is_rare}}` — only what the
  card draws.
- **Find people** (B-081). New: `GET /api/social/users/public-profile/search/?q=` — case-
  insensitive prefix of the username, first name, last name or "first last"; `q` at
  least 2 characters (`400 query_too_short` with `min_length`). Lists active, verified
  `client` and `trainer` accounts other than the caller, never anyone blocked in either
  direction; exact username first, then username prefixes, then names. Paginated
  `{count, next, previous, results}`; a row is a subset of the public profile: `{id,
  username, user_type, first_name, last_name, profile_picture_url, followers_count,
  is_following, member_since}`. `by_username/` is unchanged (exact, case-sensitive).
  The plain list `GET /api/social/users/public-profile/` (nothing in the app calls it)
  lists exactly that set too, ordered by username, rows the full public profile: it
  paged every active account, staff, agents and unverified sign-ups included (the
  routes review, 2026-11-23). A profile still opens by id for any visible account.

**What the fix run changed in the community** (BACKEND_BUGS B-161, B-162; SIDE_BUGS
S-189, S-194, S-218, S-219; the owner's decisions of 2026-09-28). Additive: no route,
no `code` renamed. `ChallengeViewSet`'s docstring carries the full contract.

- **Two more counted metrics** (B-161). `metric` gains `exercise_reps` — the reps of
  ONE exercise, named by the new writable, nullable `exercise` (an exercise id the
  creator may read; served with read-only `exercise_name`), a day above 500 reps of it
  flagged for review — and `streak_days` — the CURRENT run of consecutive days with at
  least one counted set (today, not over yet, breaks nothing, nor does the window's
  last day once the window is over: the run stands as it stood at the end instant; any
  other day without one breaks it; the hourly phase task recounts it). Refusals on
  create and update, `400
  validation_error` with the field code in `field_errors.exercise`:
  `exercise_required` (`exercise_reps` with no exercise) and `exercise_not_found`. An
  `exercise` sent with any other metric is stored null. «عدد التمارين» is `workouts`.
  A private exercise (`created_by` set) is accepted only in its own author's
  `my_clients` challenge — anywhere else, a client naming their coach's included, it
  is `exercise_not_found`, and so is a PATCH moving such a challenge's `audience` back
  to `community` (the wave-1 review, R-3 and R-11: nobody outside the coach's clients
  can log a set of it, and strangers were served its name).
- **Who may join** (B-162). Challenge rows gain writable `audience`: `community`
  (default) | `my_clients`. Only a trainer creator may set `my_clients` (`400
  validation_error`, field code `audience_trainers_only`). Such a challenge is shown
  only to its creator, staff, the creator's clients (an approved relation; a frozen
  coaching counts) and anyone already in it (a client whose coaching ends stays): for
  anyone else it is absent from the list and its detail and actions answer 404,
  `join/` answers `403 challenge_clients_only`, and `POST /api/v1/reports/` naming it
  answers `404 target_not_found`.
- **Fresh lists** (S-189, S-194). The challenges list is still cached 120 s per person,
  but a challenge an operator hides, a creator's edit, close or delete, and anyone's
  join or leave now reach every person's list at once.
- **A total order** (S-218). The post list (`ordering` default and any `?ordering=`),
  the comment list's `?ordering=` and the AI chat's `sessions/{id}/messages/` all end
  their order in `-id`: rows sharing a timestamp or a like count no longer repeat or
  vanish across pages. Same rows, same default order otherwise.
- **Text caps** (S-219). A post's `content` takes at most 5000 characters, a comment's
  2000 (create and edit): `400 validation_error`, field code `content_too_long`.

**What wiring the train screens found** (BACKEND_BUGS B-106, B-109, B-110, B-112,
2026-09-24). Additive under §6: new keys on existing answers, one query parameter, two
new codes; no route, key or `code` changed. One new column (`routine/0028`,
`ExerciseSetLog.personal_record`, the verdict a set was answered with; false on rows
written before it). The views' and serializers' docstrings carry the full contract.
Prefix `/api/routine/`.

- **The coach's review in one read** (B-106). `GET workout-sessions/{id}/?expand=sets`
  answers the session as before plus `sets`, `planned` and `routine_name`. `sets[]`:
  `{id, set_number, weight, reps, rest_time, rpe, notes, date, personal_record,
  exercise: {id, name}, prescription}`, grouped by exercise in the day's order, then
  by `set_number`; `prescription` is that exercise's row of the day, `{id, sets, reps,
  weight, rest_time, order}`, or null when the day does not prescribe it.
  `personal_record` is the verdict the client was answered with when the set was
  logged. `planned[]` is the whole day's prescription, untouched exercises included:
  `{id, exercise: {id, name}, sets, reps, weight, rest_time, order, notes}`. The cost
  is fixed — one query more than the plain detail, whatever the sets. Readable by the
  person who trained, an admin and a trainer with an APPROVED relation (the one
  predicate; 404 `not_found` otherwise, as the plain detail). Any other `expand` value:
  400 `bad_expand` with `field: "expand"`. Without `expand` the detail is unchanged;
  the list is never expanded.
- **A synced set keeps its rest** (B-109). `set-logs/sync/` rows take `rest_time`
  (seconds) and store it exactly as `POST set-logs/` does, read by that route's own
  field. A value the single POST refuses (not a whole number) is the row's error
  `rest_time_out_of_range`.
- **A re-sent batch learns what landed** (B-110). `set-logs/sync/` `created` and
  `duplicate` rows gain `weight` and `reps` as STORED, and a `duplicate` row's
  `personal_record` is now the verdict the first answer gave (it was always false); a
  key sent twice in one batch answers its twin's verdict. Nothing is celebrated again.
- **The best a record beat** (B-112). Beside `personal_record: true`, `POST set-logs/`,
  the `set-logs/sync/` rows and the `set-logs/bulk-create/` rows carry `previous_best:
  {weight, reps, date}` — the heaviest set logged before it (among equal weights the
  most reps, then the latest; an earlier set of the same batch counts): exactly the
  record it beat (`strength.record_beaten`). Null when the set is not a record, and on
  a replay, a PATCH or a list read.
- **The records of a finished workout** (BACKEND_DELTA item 12, 2026-09-27). The answer
  that FINISHES a session — `PATCH/PUT workout-sessions/{id}/` moving `status` to
  `completed`, or `POST workout-sessions/` of one already `completed` — carries
  `personal_records`, one entry per exercise that set a record among the sets logged
  in that session: `{exercise: {id, name}, set_log_id, weight, reps, date,
  previous_best: {weight, reps, date} | null, gain_kg | null}` — the session's heaviest
  record set, beside the best that stood before the session's first record of it. Read
  from the verdicts the logging answers gave (never re-judged). `[]` when none; absent
  on every other answer (a re-sent finish, a replay, a read). `analytics/summary/`
  `prs` was already real (B-013).

**What the review of the routine-logging build found** (2026-09-24). No route, key or
`code` changed. One new column (`routine/0029`, `ExerciseSetLog.previous_best`, what a
record beat, stored with the verdict; null on rows written before it). Prefix
`/api/routine/`.

- **The review reads the plan the day closed with.** `workout-sessions/{id}/?expand=sets`
  answered `planned` and each `prescription` from the routine AS IT IS NOW, so the coach
  editing the client's copy the next morning rewrote what the workout was asked to be,
  and disagreed with the day's own outcome. Once the day is closed (finishing the
  session closes it), both come from the plan frozen at the close: `sets`, `reps`,
  `weight`, `order` as they were then (`sets` at least one, as the day counted it), and
  a row deleted since is still listed with its old `id`; `rest_time` and `notes` are the
  row's current ones, null and "" for a deleted row. A day still open reads the routine
  as it is now. Still one query more than the plain detail (one more when the day holds
  no row any more, one more when a deleted row names an exercise nobody logged).
- **A duplicate's `previous_best` is the one first answered.** A `set-logs/sync/`
  `duplicate` row answered the stored verdict beside a `previous_best` recomputed at the
  retry, so an earlier set corrected or deleted in between answered «+(-10) kg», or a
  record with nothing beaten. It is now stored on the set when the record is judged and
  answered as stored.
- **A raced key is never someone else's set.** Two sends of one `client_uuid` racing,
  one of them another person's, answered that person's set (its weight, reps and
  record). That row now answers `idempotency_conflict`, exactly as a key found up front
  does.

**Anonymous AI training copies** (the owner's decisions 1-3 of 2026-09-24, P0). No
route, field or `code` changes. What "help us improve" keeps is now what the app says it
keeps, and each view's docstring states it in full.

- **What a copy is.** While `training-consent/` is on, each answered turn leaves one
  copy (`AITrainingData`). The copy has no link to the account, the chat or the message,
  and holds no name, username, e-mail, phone number or account id. The text keeps what
  was said, with e-mail addresses, phone numbers (Arabic-Indic digits too), links,
  @handles and the person's own names removed; a name a tool told the coach (the
  trainer's, for one) is removed too. Anything else the person typed, a weight or an
  injury included, stays as typed. Tool output loses every id and name, and the profile
  tools' answers become ranges. The profile is ranges and codes only: age band
  (`<18`, `18-24` … `65+`), height band (10 cm), weight band (10 kg), BMI category,
  gender, activity level, goal, training goal, the injury areas chosen from the list
  (never the injury description), how many routines are active, and the diet goal with
  a 250 kcal calorie band. The copy is stored under a keyed pseudonym (HMAC-SHA256 of the
  user id with a server secret, `AI_TRAINING_PSEUDONYM_KEY`, which production requires).
  Nobody without the secret can reverse it, and it is used only to find the person's
  copies to delete them, or to put a later 👍/👎 on the right one.
- **Deleted with the person.** Switching `training-consent/` off, `DELETE /api/ai/data/`,
  `DELETE /api/privacy/erase/` and deleting the account each delete every copy kept for
  that person, found by the pseudonym. `GET /api/ai/data/` `details.training_records`
  counts them the same way.
- **Deleted after 365 days at most.** Every copy is written with `retain_until`
  (`AI_TRAINING_RETENTION_DAYS`, default 365; the server refuses to start with more). A
  daily job (`ai_assistant.tasks.purge_expired_training_data`) deletes a copy once that
  date passes.
- **The export** (`GET /api/privacy/export/`) gives the person
  `sections.ai_training_data`: every copy exactly as stored, `{created_at, retain_until,
  user_context_snapshot, user_message, tools_called, tool_results, ai_response,
  user_feedback}`, without the pseudonym. The list is empty when nothing is kept.
- **Rows already kept** were converted by migration `ai_assistant 0009`, which cannot be
  reversed: pseudonym from the old user id, the snapshot reduced to bands, the free text
  and the names dropped, the text scrubbed. Migration `0010` then drops the three FK
  columns.

**A suspended account leaves every public surface; weekly goals take their target from
the plan; social refusals name their codes** (the owner's decisions 8, 5 and 9,
2026-09-24). No new route. Additive under §6 except where marked: new fields, and new
`code` values where a refusal answered a status default. The views' docstrings carry
the full contract.

- **Who is shown** (decision 8, P0). ONE predicate, `users/visibility.py`: a person is
  publicly visible when their account is active; a suspended or erased account is not.
  Nothing is deleted — the rows stay, they are not SHOWN — and reinstating the account
  shows everything again. Operator views keep every account.
  - Challenge standings: a suspended participant holds NO place, exactly like one who
    dropped out: not in `leaderboard/` rows, never the `me` row, not counted ahead of
    anyone, so the places close up behind them (1, 2, 3 — a block still leaves the gap,
    a suspension does not) in `leaderboard/`, `user_progress.rank`, the home card's
    `challenge_rank.rank` and its `participants`. The stored `rank` of every RUNNING
    challenge the person is in is rewritten at the moment `is_active` changes, either
    way (`social/presence.py`); a finished challenge's stored ranks are the record. The
    Challenge Winner badge is never awarded to an account that is not active.
  - `GET /api/achievements/leaderboard/`: not on the board and not counted in
    `user_rank`; the places close up. The cached board is dropped at the suspension.
  - Posts and comments by such an account leave the feed, the post list, search,
    `?author=`, the detail route (404 `not_found`), likes and replies; `comments_count`
    no longer counts their comments.
  - `follows/followers/`, `follows/following/`, `GET follows/` and a profile's
    `followers_count` / `following_count` leave them out. `unfollow_user/` still works.
  - A challenge they created is still listed, with `creator: null` (the app already
    omits "hosted by" when `creator` is null).
  - Trainers: never in the roster, matches or trainer lists (as before); a client's
    `POST /api/auth/client/request-trainer/` for one is `404 not_found` (it created a
    pending request), and `GET /api/auth/client/trainer-invites/` no longer lists their
    invitations.
  - Already true and unchanged: the public profile and `by_username/` (404), people
    search (only active, verified `client`/`trainer` accounts — decision 7 keeps
    staff, top-up agents and unverified signups out), following one (`404
    user_not_found`).
- **Weekly goals** (decision 5). A `workout_frequency` goal's `target_value` is
  calculated at read time, like its count: the person's program's training days THIS
  week (the home's `week_stats.workouts.planned`), else their profile's
  `training_days_per_week`. New read-only field `target_source`: `"program"` |
  `"profile"` | `"saved"` (neither is available now: the target it was last calculated
  at) on a `workout_frequency` goal, null on every other type. `POST
  /api/analytics/goals/` may leave `target_value` out for that type. Refusals, on POST
  and PATCH/PUT: `400 goal_target_from_plan` with `expected_target` (number) and
  `target_source` — a sent target that differs; `400 goal_target_unavailable` — no
  program asking for work this week and no training days on the profile (a new weekly
  goal cannot be created then; the app sends the person to set their training days).
  `update_progress/` still answers `goal_counted_from_workouts`. Every other type still
  requires `target_value` (field code `required`). A weekly goal's `target_value`, and
  therefore its `progress_percentage`, can change from one week to the next.
- **Social codes** (decision 9). Endpoint → new codes:
  `GET users/public-profile/by_username/` → `400 username_required`, `404 not_found`
  (named explicitly; it was the default); `PATCH/PUT/DELETE` a post, comment or
  challenge that is not yours → `403 not_owner`; `PATCH/PUT/DELETE follows/{id}/` of a
  follow you did not make → `403 not_your_follow`; `POST follows/` → `400
  already_following` (a repeated follow; it was a 500) and the field code
  `cannot_follow_yourself` on `following_id`; `GET notifications/?page=` → `400
  cursor_paginated` (was `validation_error`; `field_errors.page` is still there, code
  `cursor_paginated`); comments → field codes `comment_cannot_move` (on `post` /
  `parent_comment`) and `reply_not_in_thread` (on `parent_comment`); challenges → field
  code `status_follows_dates` on `status`; `POST comments/` on a post you cannot read →
  `404 not_found` (named explicitly). Every other social refusal already named its
  code; a 404 from a detail route stays `not_found`.

**Every diet, analytics and erasure refusal names its own code** (the owner, 2026-09-24,
decision 9: "give generic API refusals their own specific error codes"). Each refusal
below answered only its status default (`bad_request`, `permission_denied`,
`unprocessable`) and now names the reason, beside the same `error` / `detail`; §6 makes
each a new code, not a changed one, so an app that does not know it yet reads it as the
default it replaces. A 404 that means "no such object, or not yours" stays `not_found`,
now written by the view; a serializer's field errors stay `validation_error` with
`field_errors`. No code a view already named has changed. Each view's docstring lists all
of its codes.

| Endpoint | New `code`s |
|---|---|
| `diet/v1/food/list/` | 400 `bad_category`, `bad_search` (a NUL character in it) |
| `diet/v1/food/search/` | 400 `missing_fields` (`fields: ["q"]`), `bad_q`, `bad_query` |
| `diet/v1/food/import/` | 400 `missing_fields` (`fields: ["api_id"]`), `bad_body`; 403 `admins_only` |
| `diet/v1/preferences/` | 400 `missing_fields` (`fields: ["food_id"]`), `invalid_action`, `bad_food_id`, `bad_local_ratio`, `bad_allergies`, `bad_body`, `bad_<list>` for `liked_foods` / `disliked_foods` / the five `*_choices`; 404 `no_preferences` (DELETE with nothing ever saved; it was `not_found`) |
| `diet/v1/preferences/food-category/` | 400 `missing_fields` (with `fields`), `bad_meal`, `bad_macro`, `bad_food_id`, `bad_body` |
| `diet/v1/preferences/food-category/{food_id}/` | 400 `missing_fields` (`fields: ["meal", "macro"]`), `bad_meal`, `bad_macro`, `ambiguous_slot`, `bad_body` |
| `diet/v1/plans/generate/` | 400 `bad_date`; 403 `trainers_cannot_generate` |
| `diet/v1/plans/generate-rule/` | 400 `bad_date`; 403 `trainers_cannot_generate`; 422 `infeasible_calories` (the async route's code for the same refusal) |
| `diet/v1/trainer/templates/` | 403 `not_a_trainer` |
| `diet/v1/trainer/diet-plans/` | 400 `missing_fields` (with `fields`), `bad_client_id`, `bad_template_id`, `bad_date`, `bad_body`, `bad_macro_split` (also `trainer/diet-plans/review/`, BACKEND_BUGS B-137); 403 `not_a_trainer` |
| `diet/v1/trainer/meals/`, `.../{meal_id}/` | 400 `missing_fields` (with `fields`), `bad_diet_plan_id`, `bad_date`, `bad_body`; 403 `not_a_trainer` |
| `diet/v1/nutrition/plan/{plan_id}/`, `diet/v1/meals/{meal_id}/components/`, `diet/v1/plan/{plan_id}/meals-with-ingredients/` | 403 to a trainer: `not_plan_owner`, `not_assigned_client` or `plan_superseded` (the trainer service's own reason), `not_assigned_view` for any other |
| `diet/v1/client/meals/{meal_id}/` | 403 `clients_only` |
| `analytics/sessions/end_session/` | 400 `missing_session_id` |
| `analytics/dashboard/platform_stats/` | 403 `admins_only` |
| `privacy/erase/` (DELETE) | 400 `confirmation_required` (the `preview` rides along as before) |

Also: a non-numeric `client_id` / `template_id` / `diet_plan_id` on the trainer routes,
and a non-numeric `food_id` on `DELETE preferences/`, answered 500; each is now 400
`bad_<field>`. The three analytics sentences above were English-only and are now
translated.

**Every routine refusal names its own code** (the owner, 2026-09-24, decision 9). Each
refusal below answered only its status default (`bad_request`, `permission_denied`,
`not_found`, `conflict`) and now names its reason — a new `code` value under §6, so a
client that does not know one still reads the default it replaces. No status, key or
named code changed; `error` / `detail` are as before (now translated where they were
English). Prefix `/api/routine/`.

| Endpoint | New codes |
|---|---|
| `DELETE exercises/{id}/` | 409 `exercise_has_history` |
| `POST exercises/create-with-image/` | 400 `missing_fields` (+ `fields`, `field`; minted as `field_required` and unified before it shipped, B-092) |
| `DELETE routines/{id}/` | 409 `routine_has_history` |
| `POST routines/{id}/assign_to_client/` | 400 `client_id_required`, `not_your_client`, `already_assigned`; 404 `client_not_found`; 500 `routine_assign_failed` |
| `POST routines/{id}/unassign_from_client/` | 400 `client_id_required`, `not_your_client`, `not_assigned`; 404 `client_not_found`; 500 `routine_unassign_failed` |
| `POST routines/{id}/update_progress/` | 400 `day_and_status_required`, `bad_day`, `day_out_of_range`, `day_has_no_exercises`; 403 `routine_not_yours` |
| `GET routines/my_clients_progress/` | 403 `not_a_trainer` |
| `POST routine-progress/` | 403 `routine_not_yours` |
| `PATCH/PUT/DELETE set-logs/{id}/` | 403 `set_not_yours` (a trainer on a client's set) |
| `POST set-logs/` | 400 `set_log_exists` |
| `GET set-logs/my-progress/` | 403 `clients_only` |
| `POST set-logs/bulk-create/` | 400 `routine_day_date_required`, `bad_number` (+ `field`), `sets_out_of_range` (+ `max`), `weight_out_of_range`, `reps_out_of_range`, `bad_date`, `date_in_future`, `date_too_old`, `day_out_of_range`, `day_has_no_exercises`; 403 `routine_not_yours`; 404 `routine_not_found`. `errors[]` rows gain `code`: `set_log_exists`, `set_write_failed` |
| `POST user-exercise-progress/bulk-complete/` | the same, less the four number codes (its `errors[]` rows already carried `code`) |
| `GET user-exercise-progress/daily-summary/` | 400 `bad_date` |
| `POST workout-sessions/` | 403 `not_your_client` (a trainer naming someone else's `user`); field code `routine_not_yours` on `routine` (was `invalid`) |
| `PATCH/PUT/DELETE workout-sessions/{id}/` | 403 `session_not_yours` (a trainer on a client's session) |
| `GET analytics/summary/`, `completion/`, `streaks/`, `adherence/`, `strength/`, `trends/` | 403 `user_not_accessible`; 404 `user_not_found` (`summary`, `streaks`, `adherence`, `strength`); 400 `bad_period` (`summary`, `trends` — `adherence` already had it) |
| `GET analytics/admin_dashboard/` | 403 `not_a_trainer` |
| `POST templates/{id}/generate/` | 400 `client_id_required`, `bad_date`, `end_before_start`, `bad_days`, `days_out_of_range` (+ `max`); 403 `not_your_client` (single `client_id` too); 404 `client_not_found` |
| `POST templates/{id}/copy/` | 400 `template_not_public`; 400 `validation_error` with `field_errors.goal` when the source is a template stored before the closed goal list whose text reads as none of the four (2026-10-07; one that reads as a code is copied with that code) |
| `GET templates/my_templates/` | 403 `not_a_trainer` |
| `DELETE templates/{id}/` | 409 `template_listed_for_sale` — a routine product of yours is in review or published on it; retire it first (plan step 10) |
| `POST/DELETE exercises/{id}/image/` | 400 `image_required`; 404 `no_image` (DELETE, the exercise has no picture; gains `error`) |
| `POST/DELETE exercises/{id}/add-media/` | 400 `media_items_required` / `media_ids_required`; a 207 DELETE gains `errors_detail[] = [{media_id, code, message}]`, `code` `media_not_on_exercise` or `media_delete_failed` |
| `GET trainer/client-progress/{id}/` | 403 `not_your_client` |
| `GET trainer/client-progress/recent/`, `attention/` | 403 `not_a_trainer` |

- `not_found` is now NAMED (not filled in) where a 404 means only "no such object, or
  not yours": the exercise media routes, `analytics/strength/?exercise_id=` and
  `trainer/client-progress/{id}/`. A body's `routine_id` naming nothing is
  `routine_not_found` and a body's `client_id` `client_not_found`, so the app can tell
  the object it named from the route it called.
- A date shaped like a date and not one (`2026-02-30`) was a 500 on `bulk-create`,
  `bulk-complete` and `templates/{id}/generate/`, and failed a whole `set-logs/sync/`
  batch; it is `bad_date` (per row on `sync/`).
- Still `permission_denied`, deliberately: the ROLE refusals of the permission classes
  (§2 "wrong role" — a client on a trainer-only viewset, S-117) and the two named ones
  (`routines/{id}/copy/`, a non-owner's exercise media write). Still `validation_error`:
  serializer field errors, `?page=`, and `int_param` numbers — `field_errors` carries
  the specifics.

**Every refusal names its own code: accounts, coaching requests, media, chat** (the
owner, 2026-09-24, decision 9). Additive under §6: each code below replaces a status
default the envelope filled in (`bad_request`, `permission_denied`, `not_found`,
`conflict`); no code a view named changed, and no status moved. A 404 that only means
"no such object, or not yours" now names `not_found` itself (same value). The views'
docstrings carry the full tables.

| Endpoint | New codes |
|---|---|
| `POST /api/auth/token/logout/` | 400 `refresh_token_required` (with `field`), 400 `refresh_token_invalid` |
| `POST /api/auth/verify-otp/` | 400 `email_and_otp_required` |
| `POST /api/auth/resend-otp/` | 400 `email_required` (with `field`) |
| `POST /api/auth/forgot-password/confirm/` | 400 `reset_token_invalid` (no such token), 400 `reset_token_expired` (used, or its 15 minutes are over) |
| `POST /api/auth/device-token/` | 400 `token_required` (with `field`) — the code `DELETE` already answers |
| `trainer/profile/` (GET, POST), `trainer/clients/`, `trainer/assign-client/`, `trainer/unassign-client/`, `trainer/pending-requests/`, `trainer/respond-to-request/`, `trainer/client-profile/` (list and `{id}/`) | 403 `not_a_trainer` for a caller who is not a trainer (`client-profile/`: nor an admin) — the code the routine and mediakit trainer routes answer (minted as `trainers_only` and unified before it shipped, B-092) |
| `client/profile/` (GET, POST), `client/available-trainers/`, `client/request-trainer/`, `client/unassign-trainer/`, `client/cancel-trainer-request/`, `client/respond-to-invite/`, `client/trainer-invites/`, `client/request-status/` | 403 `clients_only` for a caller who is not a client — the code `/api/v1/coaching/current/` and `/api/v1/trainers/matches` already answer |
| `POST /api/auth/trainer/assign-client/` | 400 `client_id_required` (with `field`), 400 `invalid_note` / `note_too_long` (the invitation note, B-093), 400 `already_with_this_trainer` (the code the client side answers for an approved pair; minted as `already_your_client` and unified before it shipped, B-092), 400 / 409 `request_already_pending` (the code the client side answers for the same state), 404 `not_found` |
| `POST /api/auth/trainer/unassign-client/` | 400 `client_id_required` (with `field`), 400 `not_your_client`, 404 `not_found` |
| `GET /api/auth/trainer/client-profile/{id}/` | 403 `not_your_client`, 404 `not_found` |
| `POST /api/auth/client/unassign-trainer/` | 400 `no_assigned_trainer` |
| `POST /api/auth/client/cancel-trainer-request/` | 400 `no_pending_request` |
| `GET /api/v1/trainers/{id}/` | 404 `not_found`, now named (same value) |
| `POST /api/v1/content/media/video/` | 403 `media_not_yours` (visible, not the caller's to change), 403 `media_use_trainer_route` (`owner_type=trainer_exercise` goes through `/api/v1/trainer/exercise-media/`); 404 `not_found` now named |
| `GET /api/v1/content/media/exercise/{id}/`, `/api/v1/trainer/exercise-media/` | 404 `not_found`, now named (same value; the body is byte-identical) |
| `POST /api/v1/chat/conversations/start/` | 400 `invalid_body` now reaches the app: it was documented, but raised as DRF's `ValidationError`, whose code never reaches the envelope, so the app read `validation_error` |

Sign-in: `POST /api/auth/login/` and `POST /api/auth/token/` keep `400 validation_error`
for a refusal of the whole request (the regression gate pins it), and the reason is now
`field_errors.non_field_errors[0].code`: `invalid_credentials` (`/login/`: wrong address
or password), `email_not_verified` (the right password on an unverified address, with
`requires_verification` and `email`) and `credentials_required` — all three were DRF's
generic `invalid`. `/token/`'s unverified refusal answered `detail: "True"` (the
sentence sat under a `detail` key the handler does not read as a field); it now answers
the sentence, in the shape `/login/` answers. A wrong password on `/token/` stays `401
no_active_account` (simplejwt's own).

Left as named, because a named code never changes: `POST
/api/notifications/message-client/` still answers 403 `permission_denied` for a caller
who is not a trainer (the view names it).

**Every wallet and subscription refusal names its own code** (the owner, 2026-09-24,
decision 9). Additive under §6: each code below replaces a status default (`bad_request`,
`not_authenticated`, `permission_denied`, `conflict`, `unprocessable`, `throttled`) or the
handler's `validation_error` on a refusal that is not about a field; no status changes,
no request does anything different, no code a view named changes, and the message is
kept beside the code. The views' docstrings carry the full tables.

| Endpoint | New codes |
|---|---|
| every `wallet/agent/*` route | `403 agents_only` (not an agent) |
| `wallet/admin/*`, `subscription/v1/admin/management/` | `403 admins_only` (not an operator) |
| `POST wallet/agent/topup/` (signed) | `401 agent_auth_invalid` — one code for a missing, stale or mismatched `X-Agent-Auth`, an unknown key and a bad signature; `403 agent_ip_not_allowed` |
| `POST wallet/agent/topup/`, `.../topup/proxy/` | `403 agent_not_active`; `400 target_not_a_client`; `429 daily_limit_exceeded` / `monthly_limit_exceeded` (the agent's cap in that currency — not the rate limiter, and `Retry-After` does not lift it) |
| `POST wallet/agent/topup/`, `.../topup/proxy/` (plan step 9) | `429 per_transaction_limit_exceeded` (more than one top-up may be), `429 receivable_ceiling_exceeded` (the cash the agent holds for the platform, unsettled, would pass their ceiling; a settlement lifts it); all four cap refusals carry `limit`, `used` (null for the per-top-up cap), `remaining`, `currency` (B-124); `.../topup/proxy/` also `403 agent_key_revoked` (the agent revoked their key and has not paired a new one, B-117). A suspended or erased client is `404 not_found`, as a missing one (B-118) |
| top-ups, `POST wallet/client/transfer/`, `POST wallet/admin/reversal/` | `409 request_in_flight` (the first request with this `idempotency_key` is still running — the code `users` answers for the same fact); `422 idempotency_key_reused` (the caller's own key was already spent on a different request of theirs; retrying never helps, and resending under a new key could move money twice — NOT the set, workout and chat writes' 409 `idempotency_conflict`, where the id is the other person's and a new id is the fix) |
| `POST wallet/client/transfer/` | `403 clients_only` (the code the diet and coaching routes answer); `400 transfer_refused` (the ledger refused, nothing moved); `400 content_not_allowed` (the `note`, BACKEND_BUGS B-163); `403 user_blocked` (either of the two has blocked the other; nothing moved — the review of wave 1, 2026-10-07) |
| `POST wallet/admin/reversal/` | `400 insufficient_balance` (the wallet that received the money no longer holds it), `400 currency_mismatch` (a wallet changed currency), `400 reversal_refused` — these three were one `bad_request` |
| `POST subscription/v1/subscriptions/` | `400 subscription_exists` (one per user: `change-plan/`, `upgrade/` or `renew/` instead); a `plan_id` naming no active plan stays `validation_error`, its field code now `invalid_plan` (was `invalid`) |
| `PATCH subscription/v1/subscriptions/{id}/` | stays `validation_error` (the view named it); on an inactive subscription `field_errors.non_field_errors[0].code` is `subscription_inactive` (was `invalid`) |
| `POST subscription/v1/subscriptions/{id}/cancel/` | `400 not_cancellable` (already cancelled or expired); a bad `reason`/`immediate` is `400 validation_error` with `field_errors`. Both were the bare serializer errors with `bad_request`; the serializer's keys (`non_field_errors`, ...) stay at the top level |
| `POST subscription/v1/admin/management/` | `400 missing_fields`, `400 invalid_user_or_plan`, `400 subscription_exists`, `400 invalid_trial_days`. Hardened 2026-11-23 (the manager's ruling): `403 admins_only` unless the caller is staff with `subscription.change_subscription` (the console's own grant); an inactive plan or an id that is not one is `invalid_user_or_plan`; `trial_days` is a whole number from 1 to the plan's `duration_days` (default 7, or the plan's length when shorter), anything else `invalid_trial_days`, never a 500; recorded as `source: operator_grant` with an admin log entry naming the operator |
| `POST subscription/v1/access/check/` | `400 invalid_features` (`features` is not a list) |
| `POST subscription/webhook/{gateway_name}/` | `400 webhook_not_verified`, `400 webhook_rejected` — both were empty bodies; a gateway reads only the status, which is unchanged |

Left as they were, on purpose: `gateway_error` (400) on the Pay, renew and upgrade routes
also covers an amount outside the gateway's bounds and a buyer with no email or phone —
splitting it would change a code a view named, so it waits for the owner. A 404 whose only
meaning is "no such object, or not yours" stays `not_found`.

**Getting stronger** (the owner, 2026-09-20). Read from sets already logged; nothing is
collected for it.

- `GET analytics/strength/?exercise_id=&user_id=` — per exercise: `direction`
  (`rising` | `steady` | `easing`), `change_percent`, `best`, `latest`, `sessions`, and a
  `projection`. With `exercise_id`: the per-training-day `points` and a `share` headline.
  Without it: `exercises`, `top_movers`, `plateaus`, `rising_count`, `tracked_count`,
  `headline`.
- A `projection` is a RANGE and only when there is enough behind it: 3 training days of
  that exercise over at least 2 weeks, else `{available: false, reason:
  "insufficient_data", needs: {...}}`; and nothing older than 3 weeks is projected
  forward — a trail gone cold answers `{available: false, reason: "stale",
  days_since_last}`. When available: `from_estimated_1rm`, `low`, `high`, `horizon_days`
  (14), `per_week_kg`, `confidence`, `assumes_sets_done_percent`. It never projects a
  decline (a falling trend "holds around where it is"), never claims a health outcome
  (no calories, weight, muscle or "faster results"), and is identical on every build.
- An exercise the person has never trained answers the same KEYS with empty values
  (`latest`, `best` null, `sessions` 0, `share.kind` `no_data`) — never a missing field.
  `best` is the heaviest set of the window, the same meaning the record push uses.
- Sets longer than 12 reps are counted as work (adherence, `volume_kg`, `sets`) but do
  not set a one-rep max: Brzycki says nothing useful past that, and a day of nothing but
  long sets has no strength point.
- **`top_set`** — «أفضل مجموعة» (the owner, F3, 2026-09-24): the set with the highest
  weight × reps, among equals the heavier weight. Only sets with both a weight and reps
  compete; a set longer than 12 reps does (volume is what it is). Every `points` entry
  carries `top_set: {weight, reps, volume_kg}` for that day (null when no set of the day
  has both); every overview `exercises` row, and the one-exercise answer, carries
  `top_set: {weight, reps, volume_kg, date}`, the best such set of the 8-week window
  (among equal sets the most recent day), or null. `best`, `best_weight` and `best_reps`
  keep their meanings. Read in the same query as the points.
- `exercise_id` obeys the exercise visibility rule: another trainer's private exercise
  answers 404 `not_found` with the same body as a missing id (the answer
  `exercises/{id}/add-media/` gives). An exercise the person has actually trained is
  always readable.
- `POST set-logs/` answers `personal_record: true` for a set that beats everything logged
  before it for that exercise (null when reading a list), and sends the `personal_record`
  push. The first set of an exercise is never a record. `POST set-logs/bulk-create/`
  answers `personal_record` on each row of `results` and sends the same push.
- The home week (`v1/analytics/recent-progress/`) carries `strength.headline`,
  `rising_count` and `tracked_count`.
- The trainer sees it too: `trainer/client-progress/{id}/` gains `strength`, the roster's
  `this_week` gains `trend` (`rising` / `easing` / `steady` counts), and
  `trainer/client-progress/attention/` ranks the clients who need a look, each reason
  carrying its numbers (`frozen`, `ended`, `unpaid`, `never_started`, `quiet`,
  `slipping`, `missing_sets`).
- Weekly pushes: `weekly_recap` to the client and `trainer_weekly_digest` to the trainer,
  Sunday evening; a paused or ended coaching is left out of the recap.

**Reps are required** on a new set log (`POST set-logs/`, `set-logs/bulk-create/`,
`set-logs/sync/`): 400 `reps_required` on the `reps` field. `as_prescribed: true` instead
of a number uses the coach's reps for that exercise on that routine day (and their weight
when none is sent); the prescription is found through the set's session, or from `routine`
and `day` in the body. 400 `no_prescribed_reps` when the coach set none.

**How hard it felt, and what to change next** (BACKEND_DELTA item 11).

- `WorkoutSession.effort_rating` — 1 (easy) to 5 (all I had), null while unanswered.
  Sent on `POST workout-sessions/`, on the PATCH that completes one, or on any later
  PATCH of `workout-sessions/{id}/`, so a client can rate a workout the next morning.
  ONLY the person who trained may set it: a trainer or admin answering for them gets
  400 `effort_not_yours` on the field, and outside 1-5 it is 400 `effort_out_of_range`
  (the database carries the same bound). It is read back on the session, on
  `workout-sessions/{id}/` and on each `recent_activity` row of
  `trainer/client-progress/{id}/`.
- `GET analytics/strength/?exercise_id=` gains `progression`, and the coach's
  `trainer/client-progress/{id}/` `strength` gains it per exercise plus a flat
  `progression` list. `{code, effort: {typical, rated_sessions}, adherence_percent,
  direction, sessions, latest, next, needs}`. `code` is the closed set `add_weight`,
  `add_reps`, `hold`, `deload`, `not_enough_data`; `next` is the one number it means
  (`{weight_kg}` or `{reps}`), null for `hold` and `not_enough_data`; `needs` says what
  is still missing and is null otherwise. The rule, one sentence per code, is in
  `routine/progression.py`. It makes no health claim and is identical on every build.

**Editing and deleting a set** (BACKEND_DELTA item 15). `PATCH` and `DELETE
set-logs/{id}/` belong to the person whose set it is, and to an admin; a trainer reads a
client's sets and no longer writes them — they did not lift them. Only `weight`, `reps`,
`notes` and `rpe` may change: `user_exercise_progress`, `workout_session`, `set_number`
and `date` answer 400 `set_immutable` on that field when changed (re-sending the same
value is a no-op). Both are refused with 403 `coaching_frozen` / `coaching_unpaid` /
`coaching_ended` while the coaching is unpaid or over, judged on the set's own date. A
delete recomputes what the set was counted in — the exercise's completed sets, the day's
`RoutineProgress`, and a closed day's `DayOutcome` — so deleting a set can turn a
complete day short and break a streak.

**What wiring the review and the rest found** (BACKEND_BUGS B-122, B-123, 2026-09-24).
Additive under §6: two keys on an existing answer and one refusal; no route, key or
`code` changed. Prefix `/api/routine/`.

- **A rest is never negative** (B-122). `rest_time` is a whole number of seconds, 0 or
  more: a negative answers 400 `validation_error` with `field_errors.rest_time[0].code`
  `rest_time_out_of_range` on `POST set-logs/` and `PATCH set-logs/{id}/`, and the row's
  error `rest_time_out_of_range` on `set-logs/sync/` (one field, read by both routes).
  No upper bound beyond the column's own: a measured rest is not capped.
- **What a reviewed record beat** (B-123). Each `sets[]` row of
  `workout-sessions/{id}/?expand=sets` gains `previous_best` — `{weight, reps, date}` of
  the best the record beat, as stored on the set with the verdict (`routine/0029`), the
  same value the logging answer gave; null when the set is not a record — and
  `client_uuid`, the app's own key for the set (null for a set logged without one).

**A progress row cannot be moved.** `PATCH user-exercise-progress/{id}/` answers 400
`progress_immutable` on `exercise` or `date`, and `PATCH routine-progress/{id}/` answers
it on `routine`, `day` or `date`: the row IS that key, the authorisation was decided from
it on create, and moving it rewrote a finished day's history. Re-sending the value a row
already holds changes nothing and is allowed, so a PUT of the whole object still works.

**Syncing what was logged offline** (BACKEND_DELTA item 14). `POST set-logs/sync/` takes
`{"sets": [...]}` — at most 200 entries, 400 `batch_too_large` (carries `max`), 400
`sets_required` for a body with no list. A phone with no signal has no server
`UserExerciseProgress` id, so each entry names what it does know: `client_uuid`
(required — the retry key), `exercise_id` (required), `date`, `set_number`, `weight`,
`reps` (or `as_prescribed`), `routine_id`, `day`, `workout_session_id`, `notes`, `rpe`,
`rest_time` (B-109). The server resolves or creates the progress row from `(you, exercise, date)`. Idempotent
per `client_uuid`, exactly as the single POST: a re-sent batch writes nothing and answers
the same ids. Partial success — each entry answers `status` `created`, `duplicate` or
`error` with a `code` (`bad_row`, `client_uuid_required`, `idempotency_conflict`,
`bad_date`, `date_in_future`, `date_too_old`, `exercise_required`, `exercise_not_found`,
`session_not_found`, `routine_not_found`, `reps_required`, `reps_out_of_range`,
`no_prescribed_reps`, `weight_out_of_range`, `set_number_out_of_range`,
`rpe_out_of_range`, `rest_time_out_of_range`, `invalid_set`, or the coaching refusal
with `trainer_id`) — and one bad row never fails the batch. The whole answer is
`{results, count, created, duplicates, errors}`, and a set that beat everything before
it carries `personal_record: true` (with `previous_best`, B-112) and sends the push. A
`created` or `duplicate` row carries the stored `weight` and `reps`, and a `duplicate`
the verdict its first answer gave (B-110).

**`exercisesetlogs/` is gone.** `ExerciseSetLogViewSet` was registered under two
prefixes and is now registered under `set-logs/` only, the spelling the app and the rest
of this document use. This is a REMOVAL, not an additive change: every
`/api/routine/exercisesetlogs/...` path — the list, `{id}/`, `bulk-create/`,
`my-progress/`, `sync/` — now answers 404. Replace the prefix with `set-logs/`; nothing
else about those endpoints changed. No shipped build calls either spelling.

**`set-logs/my-progress/` is paginated.** It answered a BARE JSON ARRAY of every set the
caller had ever logged, in both shapes. It now answers the project envelope
`{count, next, previous, results}`, 25 a page, `?page_size=` up to 100 — `results` holds
what the array used to hold, whether or not `group_by=exercise` is sent. A client
reading the old shape must take `.results`. `workout-sessions/` answers the same
envelope (it already did, from the project default) and now answers a bad `?page=` with
400 naming the parameter instead of 404, like every other list.

**One day of one routine.** `GET routine-exercises/?routine=<id>&day=<n>` — the same
filters `template-exercises/?template=&day=` already had. The list stays scoped to
routines you were assigned or wrote, so a filter can only narrow it. Both are whole
numbers: a routine id that does not exist answers the same empty page as somebody
else's (it answered 400 `Select a valid choice`, which told a caller which ids were
real), and a value that is not a whole number answers 400 on that parameter.

**A progress row names an exercise you may read.** `POST user-exercise-progress/`:
`exercise` must be one the exercise list shows you, or one you already have progress
on — anything else answers 400 `validation_error` with `does_not_exist` on `exercise`,
exactly as an id that does not exist does (it took any id, and the list then nested the
whole exercise). `date` must be inside the training window the sync already applies:
400 `date_in_future` or `date_too_old` on `date`. `POST set-logs/` answers the same two
codes on `date` for a set whose day is outside it. `streaks/` and the adherence streak
never count a day after today.

**`user-exercise-progress/bulk-complete/` row errors carry a `code`.** Each `errors[]`
entry is `{exercise, error, code}`: `progress_exists` or `progress_write_failed`.

**Exercise images and photos.**
- `POST exercises/create-with-image/` is for a trainer or an admin, like
  `POST exercises/`: anyone else answers 403 `permission_denied`. A fault answers 500
  `exercise_create_failed` and leaves nothing behind — no exercise, no media, no file.
- A refused upload on `create-with-image/` (`image`, `media_photos`) and on
  `exercises/{id}/image/` answers 400 with the upload guard's own `code`, as the profile
  picture does: `file_too_large`, `invalid_mime_type`, `unreadable_image`,
  `unsupported_format`, `image_dimensions_too_large`, `image_too_many_pixels`, and the
  guard's other refusals; `invalid_image` when it names none. It answered `bad_request`.
- `exercises/{id}/image/` obeys the exercise visibility rule: another trainer's private
  exercise answers 404 `not_found`, byte-identical to a missing id (it answered 403).
  403 `permission_denied` is only for an exercise you can see and do not own.
- A photo uploaded through `create-with-image/` is served in `media[].content`,
  `thumbnail` and `add-media/`'s `media_items[].content` as a URL signed when it is
  read. It expires like every media URL: read it again, never keep it. (It was stored
  signed, and died an hour after the upload on object storage.) Removing the media row
  or the exercise removes the file.
- A write through `add-media/` refreshes the cached exercise list, detail and
  `add-media/` GET at once (they were stale for up to five minutes).

**A workout that is still running** is `active` AND begun within the last twelve hours
(`routine/sessions.py`). Past that it was forgotten, not left running:
`workout-sessions/?status=active` does not list it, home does not offer to continue it,
and the nightly `routine.abandon_forgotten_sessions` writes `abandoned` on the row —
a value the schema has always had and nothing ever wrote. The sets logged inside an
abandoned session are untouched and still count everywhere they counted before; only
the session stops claiming a workout is in progress. `end_time` stays null, because
nobody knows when the person stopped.

**Routine writes and reads corrected on 2026-09-23** (SIDE_BUGS S-067..S-126, branch
`fx/rttr`). Two NEW codes, nothing renamed:

- `PATCH`, `PUT` and `DELETE workout-sessions/{id}/` belong to the session's owner and an
  admin; a trainer answers 403 (they still read it; a trainer's PATCH that also fails
  validation answers that 400 first, as before — `effort_not_yours`). `DELETE` removes the sets logged in
  the session and recomputes what they were counted in, and is refused with 403
  `coaching_frozen` / `coaching_unpaid` / `coaching_ended` while the coaching is unpaid
  or over, judged at the session's start.
- A session's `status` only moves forward: `active` → `completed` or `abandoned`. A PATCH
  that changes a `completed` or `abandoned` status answers 400 **`session_status_final`**
  on `status`; re-sending the status it holds is a no-op. `duration` is null for a
  session with no end that is not running (abandoned, or active and begun more than
  twelve hours ago) — it was "now minus start" and grew for ever.
- `POST set-logs/` by a trainer naming a client's progress row or session answers 403
  **`set_not_yours`** — the rule that already refused a trainer's edit and delete. An
  admin keeps the write. `reps` is 1-1000 on the single POST and PATCH, as on
  `bulk-create/` and `sync/` (0 was accepted).
- `PATCH set-logs/{id}/` of a set from an earlier day works; omitting `date` there no
  longer means "today" (it answered 400 `set_date_mismatch` for every earlier set).
- `set-logs/sync/`: a `duplicate` row is recomputed again, so a retry repairs a send that
  died before its recount. `set-logs/my-progress/` lists sets newest first (`date`
  descending, then newest written).
- `analytics/trends/` `completion_trend` is bucketed by the training `date`, and its
  `period` is a date (`YYYY-MM-DD`) like `volume_trend`'s; it was `updated_at`, a datetime.
- `analytics/admin_dashboard/` is paginated over clients by id — `{dashboard, count, next,
  previous}`, 25 a page, `?page_size=` up to 100. `dashboard` keeps its key and row shape.
- `trainer/client-progress/{id}/` `recent_activity[].routine_name` is in the coach's
  language (`Routine.translations`), as every other routine name.
- `trainer/client-progress/recent/` `last_session` gains `duration_seconds` (BACKEND_BUGS
  B-095, additive): seconds as a decimal string, exactly `client-progress/{id}/`
  `recent_activity[].duration`. `last_session.duration` stays as legacy display text
  (`"47.0 minutes"`), now in the reader's language; never parse it.
- A day that completed itself from its sets moves back from `completed` in
  `routine-progress/` when those sets are deleted; a day the person closed (`completed`,
  `confirmed_unlogged`, `bulk_complete`, or `routine-progress/`) stays completed. The
  nightly sweep records a day as `unclosed` only when that routine day itself has work.

**The trainer's program builder** (BACKEND_BUGS B-102..B-105, B-107, B-108, B-111;
design `13-trainer-programs`, 2026-09-24). Additive: every key below is new, no key
changed meaning, no code renamed. The docstrings named are the contract.

- Exercise rows (`exercises/`, `exercises/create-with-image/`, and every nested
  `exercise`) gain `is_mine` (the READER wrote it), `created_by_id` (null for the shared
  catalogue), `name_ar`, `name_en` (both readable and writable; `name` still reads the
  reader's language with the other as fallback and still writes the reader's
  language), `equipment` (`barbell` · `dumbbell` · `cable` · `machine` · `bodyweight`,
  or null) and `equipment_display`. A create needs a name in any of `name`, `name_ar`,
  `name_en`. A bad `equipment` is 400 `invalid_choice` on the field
  (`exercises/`) or 400 `equipment_invalid` (`create-with-image/`).
  `ExerciseSerializer`, `ExerciseCreateWithImageView`.
- `routine-exercises/` rows gain `superset_group` (1-20 or null; same number, same
  routine and day = one superset), `load_mode` (`kg` default · `percent` · `rpe`) and
  `load_value` (null in kg; 1-100 in percent; 1-10 in steps of 0.5 in rpe). `weight`
  keeps its meaning, the kilograms. Codes on the field: `superset_group_out_of_range`,
  `load_value_required`, `load_value_out_of_range`, `load_value_not_allowed`,
  `invalid_choice`. `RoutineExerciseSerializer`.
- Routines gain `created_by_id` (the author; `created_by` stays display text) and
  `day_names` (`{"<day>": "<name>"}`, writable, MERGED by day: null or "" removes one
  day's name, a day not sent keeps it). Codes on `day_names`: `day_names_invalid`,
  `day_out_of_range`, `too_long`. `routines/{id}/copy/` keeps day names, supersets and
  load modes. `RoutineSerializer`.
- `POST routines/{id}/copy_week/ {from_week, to_week, load_factor}` — one week written
  onto another EMPTY week in one transaction, loads x factor rounded to 0.5 kg (a
  percentage too; an RPE copied as written; factor 1 copies exactly). A filled target
  week is refused, never replaced (409 `week_not_empty`). Only the routine's author:
  403 `not_routine_owner`. Codes: `week_invalid`, `week_out_of_range`, `same_week`,
  `week_empty`, `load_factor_invalid`, `load_factor_out_of_range`, `load_out_of_range`.
  `RoutineViewSet.copy_week`.
- Templates gain `used_by_count`: distinct clients on routines generated from the
  template, for its author and an admin; null for anyone else.
  `RoutineTemplateSerializer`.
- `GET injury-map/` — `{version, areas: [{code, muscles}]}`: each `injury_areas` code
  with the `target_muscle` keys it argues with. A warning, never a refusal; no
  lighter-load number. `InjuryMapView`.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/routine/` | auth | `APIRootView` |
| `GET` | `/api/routine/analytics/adherence/` | auth | `AnalyticsViewSet` |
| `GET` | `/api/routine/analytics/strength/` | auth | `AnalyticsViewSet` |
| `GET` | `/api/routine/analytics/admin_dashboard/` | auth | `AnalyticsViewSet` |
| `GET` | `/api/routine/analytics/completion/` | auth | `AnalyticsViewSet` |
| `GET` | `/api/routine/analytics/streaks/` | auth | `AnalyticsViewSet` |
| `GET` | `/api/routine/analytics/summary/` | auth | `AnalyticsViewSet` |
| `GET` | `/api/routine/analytics/trends/` | auth | `AnalyticsViewSet` |
| `GET, POST` | `/api/routine/exercises/` | IsAdminOrOwnerOrReadOnly | `ExerciseViewSet` |
| `POST` | `/api/routine/exercises/create-with-image/` | IsTrainerOrAdmin | `ExerciseCreateWithImageView` |
| `DELETE, GET, POST` | `/api/routine/exercises/{exercise_id}/add-media/` | auth | `ExerciseAddMediaView` |
| `DELETE, POST` | `/api/routine/exercises/{exercise_id}/image/` | auth | `ExerciseImageUploadView` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/exercises/{id}/` | IsAdminOrOwnerOrReadOnly | `ExerciseViewSet` |
| `GET` | `/api/routine/injury-map/` | auth | `InjuryMapView` |
| `GET, POST` | `/api/routine/routine-exercises/` | IsAdminOrOwnerOrReadOnly | `RoutineExerciseViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/routine-exercises/{id}/` | IsAdminOrOwnerOrReadOnly | `RoutineExerciseViewSet` |
| `GET, POST` | `/api/routine/routine-progress/` | (IsTrainerOrAdmin&IsClientOrAssignedTrainer) | `RoutineProgressViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/routine-progress/{id}/` | (IsTrainerOrAdmin&IsClientOrAssignedTrainer) | `RoutineProgressViewSet` |
| `GET, POST` | `/api/routine/routines/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `GET` | `/api/routine/routines/my_clients_progress/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/routines/{id}/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/assign_to_client/` | IsTrainerOrAdminForAssignment | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/copy/` | auth (the routine's trainer) | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/copy_week/` | auth (the routine's trainer) | `RoutineViewSet` |
| `GET` | `/api/routine/routines/{id}/schedule/` | auth (the client it is assigned to) | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/unassign_from_client/` | IsTrainerOrAdminForAssignment | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/update_progress/` | auth | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{pk}/assign_to_client/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{pk}/unassign_from_client/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `GET, POST` | `/api/routine/set-logs/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `POST` | `/api/routine/set-logs/bulk-create/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `GET` | `/api/routine/set-logs/my-progress/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `POST` | `/api/routine/set-logs/sync/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/set-logs/{id}/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `GET` | `/api/routine/template-exercises/` | auth | `RoutineTemplateExerciseViewSet` |
| `GET` | `/api/routine/template-exercises/{id}/` | auth | `RoutineTemplateExerciseViewSet` |
| `GET, POST` | `/api/routine/templates/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `GET` | `/api/routine/templates/my_templates/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `GET` | `/api/routine/templates/public_templates/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/templates/{id}/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `POST` | `/api/routine/templates/{id}/copy/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `POST` | `/api/routine/templates/{id}/generate/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `GET` | `/api/routine/trainer/client-progress/attention/` | IsTrainerOrAdmin | `TrainerClientProgressViewSet` |
| `GET` | `/api/routine/trainer/client-progress/recent/` | IsTrainerOrAdmin | `TrainerClientProgressViewSet` |
| `GET` | `/api/routine/trainer/client-progress/{client_id}/` | IsTrainerOrAdmin | `TrainerClientProgressViewSet` |
| `GET, POST` | `/api/routine/trial-workout/` | auth | `TrialWorkoutView` |
| `GET, POST` | `/api/routine/user-exercise-progress/` | auth | `UserExerciseProgressViewSet` |
| `POST` | `/api/routine/user-exercise-progress/bulk-complete/` | auth | `UserExerciseProgressViewSet` |
| `GET` | `/api/routine/user-exercise-progress/daily-summary/` | auth | `UserExerciseProgressViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/user-exercise-progress/{id}/` | auth | `UserExerciseProgressViewSet` |
| `GET` | `/api/routine/v1/analytics/recent-progress/` | auth | `RecentActivityProgressView` |
| `GET, POST` | `/api/routine/workout-sessions/` | auth | `WorkoutSessionViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/workout-sessions/{id}/` | auth | `WorkoutSessionViewSet` |

### Social

**Trainers are members of the community** (Road to 10 S3.8, the owner's D11,
2026-11-22). A trainer's token is accepted on every route of this section and on
`/api/v1/reports/`, exactly as a client's: the feed and the post list, posting, editing
and deleting their own posts and comments, liking, commenting, following, the people
search, and creating, editing, closing, joining, logging and leaving a challenge and
reading its `leaderboard/` (`tests/test_road10_s38_trainer_community.py` exercises each
with a trainer's token). A trainer may also set `audience: "my_clients"`. What a trainer
may not do answers the same coded refusals as anyone: 403 `not_owner` (someone else's
post, comment or challenge), 403 `challenge_clients_only` (another coach's clients-only
challenge). No route refuses by role, and nothing differs by build.

**How many challenges one member hosts at once** (Road to 10 S3.4, the owner's 3 of
2026-11-22). Any member may still create a challenge, but one member hosts at most
`CHALLENGE_HOST_LIMIT` (3) at once: created by them and not over (upcoming or running;
one an operator hid does not count). The next `POST challenges/` answers 409
`{"error", "code": "challenge_host_limit", "limit", "hosting"}` (new code), checked
before the body, and nothing is written; closing one (`PATCH {"status": "completed"}`)
or its end frees the place. Staff are not limited. Nothing differs by build. The
platform's own starter challenge is made by an operator with `manage.py
create_starter_challenge --creator <staff username>` (DEPLOY_CHECKLIST.md §6).

`POST challenges/{id}/leave/` removes the caller's participation, recounts
`participants_count` and closes the gap in the standing. Codes: `left_challenge` (200),
`not_participating` (400, you had not joined), `challenge_closed` (400, the challenge
is over or was cancelled — a finished board does not change, and the Challenge Winner
badge keys on `rank=1`). The same `challenge_closed` `join` already answers with.

`ChallengeParticipation.rank` is now written for EVERY participant of a running
challenge, including the ones at zero, ordered `-current_value`, then `joined_at`, then
`id` — the order `challenges/{id}/leaderboard/` and the home card's `challenge_rank`
already read. Places run 1..N with no gaps and no nulls, and they stop changing once
the challenge ends. The one exception: a participation whose `status` is `dropped`
holds no place — its `rank` is null, it is not listed on `leaderboard/`, and it is not
counted ahead of anyone on the home card — so the person behind it moves up.

`DELETE comments/{id}/` takes the comment, and every reply under it, off the post's
stored `comments_count`.

`POST follows/` and `POST follows/follow_user/` answer 403 `user_blocked` when either
person has blocked the other.

`achievements/`, `achievements/{id}/` and `achievements/user_achievements/` read the
`achievements` app — the badges the engine actually awards, with the ids
`/api/achievements/` uses — in the same response shape as before. They read tables
nothing wrote, so `user_achievements/` was always empty. A secret badge is listed only
to someone who has earned it.

A challenge an operator hid (the console's hide action, after a report — `target_type`
`challenge` on `/api/v1/reports/`) is served to nobody, its creator included: it leaves
the list, and its detail, `join`, `leave`, `update_progress` and `leaderboard` answer
404. The home card stops showing it too.

`comments_count` on a post is the number of comments THIS viewer can open: the stored
counter less the ones hidden by moderation and the ones by accounts they have blocked.
It therefore equals the row count of `GET comments/?post={id}` for the same viewer.

A post, comment or challenge whose text holds a term on the banned-terms list answers
400 `content_not_allowed` on create and edit, naming the field in `field_errors`; see
"The banned-terms filter" under Safety above (2026-09-27).

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/social/` | auth | `APIRootView` |
| `GET` | `/api/social/achievements/` | auth | `AchievementViewSet` |
| `GET` | `/api/social/achievements/user_achievements/` | auth | `AchievementViewSet` |
| `GET` | `/api/social/achievements/{id}/` | auth | `AchievementViewSet` |
| `GET, POST` | `/api/social/challenges/` | IsOwnerOrReadOnly | `ChallengeViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/social/challenges/{id}/` | IsOwnerOrReadOnly | `ChallengeViewSet` |
| `POST` | `/api/social/challenges/{id}/join/` | IsOwnerOrReadOnly | `ChallengeViewSet` |
| `GET` | `/api/social/challenges/{id}/leaderboard/` | IsOwnerOrReadOnly | `ChallengeViewSet` |
| `POST` | `/api/social/challenges/{id}/leave/` | IsOwnerOrReadOnly | `ChallengeViewSet` |
| `POST` | `/api/social/challenges/{id}/update_progress/` | IsOwnerOrReadOnly | `ChallengeViewSet` |
| `GET, POST` | `/api/social/comments/` | IsOwnerOrReadOnly | `CommentViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/social/comments/{id}/` | IsOwnerOrReadOnly | `CommentViewSet` |
| `POST` | `/api/social/comments/{id}/like/` | IsOwnerOrReadOnly | `CommentViewSet` |
| `GET, POST` | `/api/social/follows/` | IsFollowParticipant | `UserFollowViewSet` |
| `POST` | `/api/social/follows/follow_user/` | IsFollowParticipant | `UserFollowViewSet` |
| `GET` | `/api/social/follows/followers/` | IsFollowParticipant | `UserFollowViewSet` |
| `GET` | `/api/social/follows/following/` | IsFollowParticipant | `UserFollowViewSet` |
| `POST` | `/api/social/follows/unfollow_user/` | IsFollowParticipant | `UserFollowViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/social/follows/{id}/` | IsFollowParticipant | `UserFollowViewSet` |
| `GET` | `/api/social/notifications/` | auth | `NotificationViewSet` |
| `POST` | `/api/social/notifications/mark_all_read/` | auth | `NotificationViewSet` |
| `GET` | `/api/social/notifications/unread_count/` | auth | `NotificationViewSet` |
| `GET` | `/api/social/notifications/{id}/` | auth | `NotificationViewSet` |
| `POST` | `/api/social/notifications/{id}/mark_read/` | auth | `NotificationViewSet` |
| `GET, POST` | `/api/social/posts/` | IsOwnerOrReadOnly | `PostViewSet` |
| `GET` | `/api/social/posts/feed/` | IsOwnerOrReadOnly | `PostViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/social/posts/{id}/` | IsOwnerOrReadOnly | `PostViewSet` |
| `POST` | `/api/social/posts/{id}/like/` | IsOwnerOrReadOnly | `PostViewSet` |
| `GET` | `/api/social/users/public-profile/` | auth | `PublicUserProfileViewSet` |
| `GET` | `/api/social/users/public-profile/by_username/` | auth | `PublicUserProfileViewSet` |
| `GET` | `/api/social/users/public-profile/search/` | auth | `PublicUserProfileViewSet` |
| `GET` | `/api/social/users/public-profile/{id}/` | auth | `PublicUserProfileViewSet` |

### Checkout sessions (the web payment hub, payments spec §5)

How the web hub (channel `web`; `apk` may use it too) buys the two digital goods, a plan
(`PLATFORM_SUB`) and a routine product (`ROUTINE_PRODUCT`), and pays through ShamCash.
A session IS a payment (`subscription.Payment`): its `id` is the payment's, so "I paid" is
`POST /api/subscription/v1/payments/{id}/reconcile/` and the step history is
`GET /api/subscription/v1/payments/{id}/status/` (both now answer a product session's
buyer too). The views' docstrings (`subscription/checkout_views.py`) are the full
contract.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET, POST` | `/api/v1/checkout/sessions/` | auth | `CheckoutSessionsView` |
| `GET` | `/api/v1/checkout/sessions/{id}/` | auth | `CheckoutSessionDetailView` |

The collection answers without the final slash too (`/api/v1/checkout/sessions`), as the
spec names it.

**Open** — `POST {"money_type", "item_id"}`: ONE open session per user per item; asking
again answers the same session and reference (200, `already_open: true`; new: 201). A plan
session is the subscription's one checkout, opened by the Pay button's own call (or the
Renew button's while cover runs on that plan) — the same checkout `POST v1/gateways/`
hands back. A product session prices the program in the caller's currency now and fixes
the commission its sale is split at; confirmed, it delivers the caller's own copy and
pays the trainer exactly as a wallet purchase (`buy/`) does, and a push says so
(`routine_product_purchased`, `routine_product_sold`); expired or rejected, it delivers
nothing. **Read** — `GET` the open ones (`{"results": [...]}`), or one by id in any
state: `status` is `open | needs_review | confirmed | rejected | expired | refunded`
(`payment_status` is the payment's own), with `amount`, `currency`, `reference`,
`expires_at`, `payment_target` (`{adapter: "manual_transfer", gateway, account_id, amount,
currency, note, payment_url}` — transfer exactly `amount` to `account_id` with `note`),
`review`, and `result` once paid (`{subscription_id}` or `{purchase_id, purchase_status,
routine_id}`).

**Differs by build.** Opening: `web` and `apk` only (`play` / `ios`: 403
`not_available_on_channel`, before anything is read; no channel: 400
`app_channel_required`). Reading works on every build; a build that may not buy the
session's kind of money gets no `amount`, `reference` or `payment_target`.

Codes: `app_channel_required` (400), `not_available_on_channel` (403), `validation_error`
(400; `field_errors.money_type[0].code` `required` / `invalid_choice` — coaching and
wallet money are never sold here — or `field_errors.item_id`), `invalid_plan` (400),
`payment_not_required` (400), `plan_change_while_active` (409), `checkout_open` (409,
`payment_id`, `status`, `expires_at`), `price_changed` (409, `expected_amount`,
`expected_currency`), `not_renewable` (400), `not_found` (404), `own_product` (403),
`clients_only` (403), `already_owned` (409, `purchase_id`, `routine_id`),
`product_not_for_sale` (409), `exchange_rate_missing` / `exchange_rate_stale` (409),
`gateway_unavailable` (503), `currency_not_chargeable` (503), `gateway_error` (400),
`throttled` (429). A review of a product session can carry the reasons `already_owned`
and `product_unavailable` besides the plan's: money arrived for a program that cannot be
delivered, and is handed back.

### Subscriptions & Payments

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/subscription/` | auth | `APIRootView` |
| `POST` | `/api/subscription/v1/access/check/` | auth | `SubscriptionAccessView` |
| `GET, POST` | `/api/subscription/v1/admin/management/` | IsAdminUser | `SubscriptionManagementView` |
| `GET` | `/api/subscription/v1/features/` | auth | `SubscriptionFeatureViewSet` |
| `GET` | `/api/subscription/v1/features/{id}/` | auth | `SubscriptionFeatureViewSet` |
| `GET, POST` | `/api/subscription/v1/gateways/` | auth | `PaymentGatewayView` |
| `GET` | `/api/subscription/v1/payments/` | auth | `PaymentViewSet` |
| `GET` | `/api/subscription/v1/payments/{id}/` | auth | `PaymentViewSet` |
| `GET` | `/api/subscription/v1/payments/{payment_id}/receipt/` | auth | `PaymentReceiptView` |
| `POST` | `/api/subscription/v1/payments/{payment_id}/reconcile/` | auth | `PaymentReconcileView` |
| `GET` | `/api/subscription/v1/payments/{payment_id}/status/` | auth | `PaymentStatusView` |
| `DELETE, GET, PUT` | `/api/subscription/v1/payer-address/` | auth | `PayerAddressView` |
| `GET` | `/api/subscription/v1/plans/` | public | `SubscriptionPlanViewSet` |
| `GET` | `/api/subscription/v1/plans/available/` | public | `SubscriptionPlanViewSet` |
| `GET` | `/api/subscription/v1/plans/{id}/` | public | `SubscriptionPlanViewSet` |
| `GET, POST` | `/api/subscription/v1/subscriptions/` | auth | `SubscriptionViewSet` |
| `GET` | `/api/subscription/v1/subscriptions/current/` | auth | `SubscriptionViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/subscription/v1/subscriptions/{id}/` | auth | `SubscriptionViewSet` |
| `POST` | `/api/subscription/v1/subscriptions/{id}/cancel/` | auth | `SubscriptionViewSet` |
| `POST` | `/api/subscription/v1/subscriptions/{id}/change-plan/` | auth | `SubscriptionViewSet` |
| `POST` | `/api/subscription/v1/subscriptions/{id}/pay-from-wallet/` | auth | `SubscriptionViewSet` |
| `GET, POST` | `/api/subscription/v1/subscriptions/{id}/upgrade/` | auth | `SubscriptionViewSet` |
| `POST` | `/api/subscription/v1/subscriptions/{id}/renew/` | auth | `SubscriptionViewSet` |
| `GET` | `/api/subscription/v1/subscriptions/{id}/usage/` | auth | `SubscriptionViewSet` |
| `GET` | `/api/subscription/v1/usage/` | auth | `SubscriptionUsageViewSet` |
| `GET` | `/api/subscription/v1/usage/today/` | auth | `SubscriptionUsageViewSet` |
| `GET` | `/api/subscription/v1/usage/{id}/` | auth | `SubscriptionUsageViewSet` |
| `POST` | `/api/subscription/webhook/{gateway_name}/` | public | `PaymentWebhookView` |

### Wallet

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/wallet/admin/alerts/suspicious/` | auth, IsAdmin | `AdminSuspiciousActivityView` |
| `GET` | `/api/wallet/admin/audit/export/` | auth, IsAdmin | `AdminAuditExportView` |
| `POST` | `/api/wallet/admin/reversal/` | auth, IsAdmin | `AdminReversalView` |
| `POST` | `/api/wallet/agent/apikey/create/` | auth, IsAgent | `AgentApiKeyCreateView` |
| `POST` | `/api/wallet/agent/apikey/ensure/` | auth, IsAgent | `AgentApiKeyEnsureView` |
| `POST` | `/api/wallet/agent/apikey/revoke/` | auth, IsAgent | `AgentApiKeyRevokeView` |
| `GET` | `/api/wallet/agent/apikey/status/` | auth, IsAgent | `AgentApiKeyStatusView` |
| `GET` | `/api/wallet/agent/ledger/` | auth, IsAgent | `AgentLedgerView` |
| `GET` | `/api/wallet/agent/me/` | auth, IsAgent | `AgentMeView` |
| `GET` | `/api/wallet/agent/receipts/{reference_id}/` | auth, IsAgent | `AgentReceiptView` |
| `GET` | `/api/wallet/agent/settlements/` | auth, IsAgent | `AgentSettlementListView` |
| `POST` | `/api/wallet/agent/topup/` | auth, IsAgent | `AgentTopUpView` |
| `POST` | `/api/wallet/agent/topup/proxy` | auth, IsAgent | `AgentTopUpProxyView` |
| `GET` | `/api/wallet/agents/` | auth | `AgentDirectoryView` |
| `GET` | `/api/wallet/balance/` | auth | `WalletBalanceView` |
| `POST` | `/api/wallet/client/transfer/` | auth | `ClientTransferToTrainerView` |
| `GET` | `/api/wallet/currency/` | auth | `WalletCurrencyView` |
| `PUT` | `/api/wallet/currency/` | auth | `WalletCurrencyView` |
| `GET` | `/api/wallet/transactions/` | auth | `WalletTransactionsView` |
| `GET` | `/api/wallet/transactions/summary/` | auth | `WalletTransactionsSummaryView` |
| `GET` | `/api/wallet/transactions/{reference_id}/` | auth | `WalletTransactionDetailView` |

### root

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/` | auth | `APIRootView` |

---

## 8. Removed at the freeze

These existed before 2026-09-02 and are **gone**. They were duplicates or non-JSON, and
no client had been built against them.

| removed | use instead |
|---|---|
| `/api/diet/api/**` (24 paths) | the identical `/api/diet/v1/**` path |
| `/api/diet/preferences/food-category/**` | `/api/diet/v1/preferences/food-category/**` |
| `/api/diet/generate/`, `/api/diet/generate-plan/` | `/api/diet/v1/plans/generate/` — the removed pair served a session-authenticated **HTML page** and answered a mobile client with a `302` to a login page |

Push `data.type` values `like`, `comment`, `follow` and `achievement` were renamed to
their event types (`post_liked`, `comment_created`, `user_followed`,
`achievement_awarded`).
