# Backend bugs found while building the app

A shared notebook between two sessions.

- **The app session** (works in `yalla_gym/`) adds rows while wiring screens to the real
  API. It fills: ID, Severity, Where, What happens, What the app does meanwhile. It never
  edits the last two columns and never edits backend code.
- **The backend session** reads this file, fixes, and fills **Status** and **Backend note**
  (commit, or why not). It does not edit the other columns; if a row is wrong, say so in the
  note and set `invalid`.

Status values: `open` · `in progress` · `fixed` (give the commit) · `wontfix` · `invalid` ·
`needs owner` (a decision is owed first).
Severity: **P0** breaks a Phase 1 flow or leaks data · **P1** visible to users, has a client
workaround · **P2** wrong but harmless today.

Every row was read from the code (file:line at commit `af6bf00` + working tree of
2026-09-19) and, from B-020 on, reproduced from the app. Missing features the designs need
are not bugs: they are in `BACKEND_DELTA.md` and `yalla_gym/app/PLAN.md` §8.

Last updated by the app session: 2026-09-19. Backend notes: 2026-09-19.

## Open

| ID | Sev | Where | What happens | What the app does meanwhile | Status | Backend note |
|---|---|---|---|---|---|---|
| B-001 | P0 | `routine/serializers.py:196-199,249-258` · `routine/views.py:528-532` | `GET /api/routine/routines/` and `/{id}/` give a CLIENT caller `assigned_to`, `assigned_usernames` and `client_count` of every other client sharing the routine. Same leak for a trainer who is not the creator but is approved for one assignee (`TrainerRoutineSerializer` lists all assignees). | Drops the three fields when decoding. The data is still on the wire. | needs owner | Owner decision owed: for plain clients should `client_count` be 1 (recommended: matches their own filtered entry), the real number, or null? Keep the keys (additive). No fix was started. |
| B-002 | P0 | `users/file_security.py:244-278` (`_scan_for_malicious_content`) via `POST /api/auth/user/profile-picture/` | Ten raw byte patterns (one is the two bytes `<%`) are searched in the whole file, so 55–97% of genuine JPEGs are refused with `400 bad_request` "Potentially malicious content detected" (measured with the repo's Pillow: 512 px q85 → 36/40 refused). The error has no code of its own. | Crops, downscales to 512 px, scans its own bytes and re-encodes in a loop until clean; photo stays optional. | fixed | Image uploads no longer run the raw byte-pattern scan: every image path decodes the file and stores only re-encoded pixels, so nothing of the upload's bytes reaches storage (documents keep the scan). `create-with-image` stored the ORIGINAL `media_photos` bytes under the client's filename — now the re-encoded image under a server name. `profile-picture` refusals carry their own `code` (`file_too_large`, `unsupported_format`, `image_dimensions_too_large`, `invalid_image`, …). `tests/test_backend_bugs.py::test_b002_*`. Commit `95fad76`. |
| B-003 | P0 | `routine/` set logs and sessions: `POST /api/routine/set-logs/`, `POST /api/routine/workout-sessions/` | No client id, no `Idempotency-Key`, no unique constraint: a retried POST (timeout after the server wrote) creates a duplicate set and doubles volume. `WorkoutSession.start_time` is `auto_now_add` and not writable; `end_time` must be within 12 h of it, so a workout logged offline and synced later gets the wrong clock. The design requires offline logging. | Strictly serial outbox; sends a `client_key` (ignored today); reads `set-logs/?workout_session=` before retrying; sends `end_time = server start + measured elapsed`. | open | Next backend batch (`BACKEND_DELTA.md` item 14): `client_uuid` + partial unique index on sessions and set logs, replay returns the first row, writable start/end for offline sessions. |
| B-004 | P1 | `users/views.py` `CustomTokenObtainPairView` / `users/utils.py:342-368` | `POST /api/auth/token/` never records a failed attempt, so the 5-failure lockout only guards `/api/auth/login/`. A suspended account answers `401 no_active_account`, identical to a wrong password, so the app cannot show "account unavailable" at sign-in. The lockout itself is `400 validation_error` with no code or seconds. | Shows "wrong e-mail or password" for both; shows Unavailable only on `401 user_inactive` / `403 account_disabled`; runs a local 15:00 countdown. | open | |
| B-005 | P1 | `users/models.py:223-228` `preferred_language` | No serializer reads or writes it (default `en`). OTP and reset e-mails and every push are English for Arabic users. | Sends `Accept-Language` only. | open | |
| B-006 | P1 | `training_platform/middleware.py` rate limiter `_get_user_type` | The tier is read from the session user, which DRF fills later, so every bearer-token caller (the whole mobile app) is tiered `anonymous`. | Caches per block, backs off on 429. | open | |
| B-007 | P1 | `users/views.py` legacy trainer payloads (`client/available-trainers/`, `trainers/public/`, `client/request-status/`, `user/update/`, invites) | `trainer_hourly_rate` is sent to every build; only the new `/api/v1/trainers/*` cards strip the price by capability flag. A store build must not be sent coaching prices it may not show. | Draws a fee only through one price widget behind `can_show_coaching_prices`; never parses the legacy field into UI. | fixed | Key kept, value `null` on a build whose `can_show_coaching_prices` is off (`ios`) or that sends no channel: `trainers/public/`, `client/available-trainers/`, `client/request-status/` (`trainer_hourly_rate`, new `agreed_rate`), `client/trainer-invites/` (`agreed_rate`). `user/update/` keeps a trainer's OWN rate (their setting, not an offer). `/api/v1/trainers/*` omit the key. `test_b007_*`. Commit `95fad76`. |
| B-008 | P1 | `users/views.py` `request-trainer` | Five different refusals share `400 bad_request`; the insufficient-balance `402` also arrives with `code: bad_request`; the body field `message` (note to the trainer) is read and discarded; `agreed_rate` and `responded_at` are not on the wire; `requested_at` is the row's creation, not the re-request. | Derives the nine request-bar states from five calls; reads `required`/`balance` from the 402 body. | fixed | Codes: `trainer_id_required`, `invalid_note`, `note_too_long`, `trainer_not_accepting`, `already_has_trainer`, `request_pending_elsewhere`, `already_with_this_trainer`, `request_already_pending`, 402 `insufficient_balance` (all in the view docstring). `note` (or `message`) kept, encrypted, max 500; shown on `trainer/pending-requests/` (client-initiated rows) and `client/request-status/`; replaced on a re-sent request. request-status gains `responded_at`, `agreed_rate` (by build), `note`; `requested_at` is now this request's time. `test_b008_*`. Commit `95fad76`. |
| B-009 | P1 | `users/trainer_relations.py` `_move_hold` | Settlement pays the trainer 100% of the hold; the owner's number is 80/20 (CLAUDE.md). | None needed client-side. | fixed | Coaching is monthly now (owner): every month — the first and each renewal — pays the trainer 80% and the platform 20%, at the rate copied onto the month. `users/coaching.py`, `GET /api/v1/coaching/current/`, `BACKEND_DELTA.md` item 16. `tests/test_coaching_months.py`. Commit `95fad76`. |
| B-010 | P1 | `users/` public roster | A trainer is listed (and enters matching) as soon as the e-mail is verified, with `trainer_is_available` defaulting to true, so an empty profile sorts first (ordered by id). | New trainer accounts created from the app send `trainer_is_available=false`. | open | |
| B-011 | P1 | `routine/views.py` `update_progress`, `bulk-complete` | A client can `POST status=completed` for a day with no logs and the streak counts it. `bulk-complete` ignores `routine_id`/`day` when deciding the day's status: the post-save receiver walks every active routine containing the exercise. | Uses `bulk-complete` only for "log the whole day" and never posts `status=completed` itself. | open | |
| B-012 | P2 | `routine/serializers.py` `RoutineExerciseSerializer.Meta.fields` | `RoutineExercise.weight` exists on the model but is neither readable nor writable through the API (and template generation never copies it), so a prescribed working weight cannot exist. | Weight ruler starts from the last logged weight. | open | |
| B-013 | P2 | `routine/views.py:1409` analytics summary | `prs` is the constant `0`. | Hides the personal-record card. | open | |
| B-014 | P2 | `notifications/` preferences | The `all` row is stored and read by nothing; `event_type` is free text, so a typo answers 201 and mutes nothing. `is_enabled=false` also suppresses the in-app row. | Per-kind rows only; master switch = delete the device token. | open | |
| B-015 | P2 | `users/serializers.py` register `phone_number` | `\d` matches any Unicode digit, so `+٩٦٣…` is stored as typed. `forgot-password/verify` accepts Arabic-Indic digits in `otp_code`, fails the HMAC and burns an attempt. | Normalises digits to Western before sending. | fixed | Arabic-Indic and Persian digits are made Western: the phone is stored as `+963…`, and an OTP typed in Arabic digits is the same code (sign-up verify and forgot-password verify). `test_b015_*`. Commit `95fad76`. |
| B-016 | P2 | `social` notifications list (`NotificationCursorPagination`) | Cursor ordering is `-created_at` only; the `-id` tiebreak is discarded, so rows with the same timestamp can repeat or drop between pages. `data.trainer_id` is a string, not an int. | De-duplicates by id. | open | |
| B-017 | P2 | `users/views.py` `forgot-password/confirm` | A successful reset does not clear the sign-in lockout counter, so the user can be locked out right after resetting. Returns no tokens. | Sends the user to SignIn with the e-mail prefilled. | open | |
| B-018 | P2 | `diet` `GET /api/diet/v1/advice/latest/` | Returns the newest advice row ever written, not today's; stale advice survives after a plan ends; language is frozen at generation. | Hides advice older than 24 h by `generated_at`. | open | |
| B-019 | P2 | `users/serializers.py:108` and `CustomTokenObtainPairSerializer.validate` comments | Comments say "10 failures"; the code locks at 5 (`users/utils.py:351`). `users/serializers.py:90` has the typo `raiser`. | — | fixed | `raiser` fixed in `1c1b12d`; the two comments now say 5 failures. Commit `95fad76`. |

## Found while wiring screens (reproduced from the app)

| ID | Sev | Where | What happens | What the app does meanwhile | Status | Backend note |
|---|---|---|---|---|---|---|

## Closed

| ID | Closed as | Note |
|---|---|---|
