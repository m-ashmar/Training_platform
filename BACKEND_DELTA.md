# Backend delta for the v2 designs

Source: `yalla_gym/design/yalla-gym-design/README.md`, section "Backend changes these
designs assume", plus `docs/BuildGuide.md` and `docs/payment-spec.md` in the same pack
(`Untitled.rtf` there is a copy of the payment spec). Mapped against branch
`spec/payments-compliance` on 2026-09-19.

Rules for every item: additive only (no route, field or `code` renamed or removed), a
new route goes into `API_CONTRACT.md` in the same commit, a view's docstring is its
contract, every user-facing string goes through gettext with an Arabic entry in
`locale/ar/LC_MESSAGES/django.po`, and behaviour may differ by build (`X-App-Channel`,
`X-App-Version`) and by the user's own data only. Price display follows
`policy.services.can_show_price`; purchasing follows `require_purchase_allowed`.

## Status

| # | Item | Size | Status |
|---|---|---|---|
| 1 | Capabilities endpoint + `X-App-Channel` | - | DONE before this work (plan step 2) |
| 2 | Media slots + operator console | XL | Images, object storage (GCS) and VIDEO DONE; HLS and SVG TODO |
| 3 | Date of birth at sign-up, minimum age | M | DONE |
| 4 | Onboarding fields: goal, place, days, injury areas | M | DONE |
| 5 | Trainer proof stats | L | DONE |
| 6 | Trainer matching with reasons (+ roster and profile endpoints) | L | DONE |
| 7 | Per-trainer exercise media library | L | TODO (needs 2) |
| 8 | Two-way chat: voice, image, video, program cards, report, block | XL | TODO = plan step 8 |
| 9 | Server-driven home + day timeline | L | TODO (needs 2, 8 for voice) |
| 10 | Note to the trainer on a request | S | DONE (with BACKEND_BUGS B-008) |
| 11 | Effort rating 1-5, progression suggestion | L | TODO |
| 12 | Personal records (always 0 today) | M | `prs` DONE (BACKEND_BUGS B-013); records on the completion response TODO |
| 13 | Intro story content + remote theme override | M | Story media DONE (item 2); theme TODO |
| 14 | Offline set logging idempotency (build guide section 6) | M | DONE (BACKEND_BUGS B-003) |
| 15 | Owner may edit/delete a saved set (build guide section 6) | M | TODO |
| 16 | Coaching by the month, commission on every month (owner decision 3) | L | DONE |
| 17 | Templates: one template, a private personalised copy per client (owner decision 16) | M | DONE |
| 18 | Renewal reminder 3 days ahead; unpaid or ended = frozen (read-only) (owner decision 17) | L | DONE |
| 19 | Keep the missed work: planned vs done per day, wired into analytics (owner, 2026-09-20) | L | DONE |
| 20 | Trainer pipeline gaps found while mapping it (2026-09-20) | M | LISTED |
| 21 | Expected progress: strength trend, projection, coach early warning (owner, 2026-09-20) | XL | DONE |

## 1. Capabilities endpoint and channel header

- **Today:** `GET /api/v1/config/capabilities` (`policy/views.py`), flags per channel and
  version (`policy/services.py:flags_for`), store-locked flags, change log, console page.
  `require_purchase_allowed` guards the coaching request and invite-accept paths.
- **Must change:** nothing for the designs. Open follow-ups stay in
  `PAYMENTS_COMPLIANCE_PLAN.md` (the AI coach on the WebSocket).
- **Found while mapping:** every legacy trainer payload sends `trainer_hourly_rate` to iOS,
  whose `can_show_coaching_prices` is false (`users/views.py` public and client directories,
  request-status, invites). The new `/api/v1/trainers/` endpoints omit the price where the
  build may not show it; retrofitting the legacy five is an owner decision (below).

## 2. Media slots

- **Today (before):** no slot concept. `CustomUser.profile_picture`, `Exercise.image`,
  `ExerciseMedia` (URL text), diet `image_url` strings. No renditions, no WebP, no blur, no
  EXIF orientation fix, no video, no object storage (`SignedMediaStorage` on a Fly volume).
- **Built:** app `mediakit/`.
  - `slots.py`: the build guide's slot list with ratio, minimum size and grade. Trainer
    kit `trainer.portrait|wide|cutout|story|avatar` (required for a complete kit),
    `trainer.loop|intro` (video), `trainer.signature` (SVG); `exercise.poster|thumb`
    (+ `loop|cue` video); `dish.top`; `program.cover`; `app.story.1-8`, `app.banner`,
    `app.empty.1-12`.
  - `MediaAsset` (owner type + id + slot, unique), with `person` FK for trainer-owned rows
    (privacy registry: deleted with the account, files removed after commit).
  - Upload: the platform's upload guard, EXIF orientation, ratio (2%) and minimum-size
    checks, black-and-white grade (cut-out keeps alpha), WebP 1x/2x, a ~300-byte blur
    data URI, average brightness. Refusal codes: `media_wrong_ratio`, `media_too_small`,
    `media_needs_transparency`, `media_unsupported_format`, `media_kind_not_supported_yet`,
    `media_unknown_slot`, `media_unknown_owner`.
  - Serialized as `media: {"<slot>": {url_1x, url_2x, blur, focal: [x, y], brightness,
    alt_ar, alt_en}}`; an empty slot is absent. Lists load it in one query (`media_map`).
  - Console: `Media slots` page (upload, focal point, alt text, preview; owner and slot
    are fixed once saved) and `Media slots > kits/` (each trainer's completeness).
  - `GET /api/v1/content/media`: the app's own slots (intro story, banner, empty states).
  - Trainer objects in `/api/v1/trainers/...` carry `media`.
- **Built (2026-09-20, `build/storage`) — object storage and video:**
  - `MEDIA_BACKEND` = `local` | `gcs`, chosen by the environment and by no code branch.
    `gcs` is `training_platform/gcs_storage.py` (`django-storages[google]`, V4 signed
    GET URLs, private objects, credentials through `get_secret`); `local` is the
    filesystem, which `enforce_production_safety()` now REFUSES in production. One URL
    function for both: `training_platform.media_storage.media_url()`.
  - The `/data` permission bug is fixed: the volume is mounted over the path the image
    chowned, so appuser met a root-owned mount and NO upload could be written at all —
    not even the multipart spool. `docker-entrypoint.sh` chowns after the mount and
    drops privileges; the image no longer sets `USER`.
  - `MediaAsset` gained `poster`, `hls_url`, `duration_seconds` and `processing_state`
    (migration `0002_media_video_fields`, additive, no backfill). A video slot
    serializes as `{video_url, poster_url, hls_url, duration_seconds, width, height,
    state, alt_*}`.
  - `POST /api/v1/content/media/video/` (validates by CONTENT: container sniffed from
    the bytes, 64 MB and 90 s caps) and `GET /api/v1/content/media/exercise/{id}/`,
    both under the exercise library's own visibility rule. The 15 MB body cap has one
    named exception, `settings.LARGE_UPLOAD_PATHS`.
  - `mediakit.fill_video_metadata` fills the poster, duration and dimensions with
    ffprobe/ffmpeg WHEN THEY EXIST and marks the asset `ready` either way (ffmpeg is
    not in the image — an owner decision, see below); `mediakit.sweep_pending_videos`
    (hourly) finishes anything whose task was lost.
  - Still owed here: **the owner must create the bucket and the service account** and
    set `MEDIA_BACKEND`, `GS_BUCKET_NAME`, `GS_PROJECT_ID`, `GS_CREDENTIALS_JSON`
    (DEPLOY_CHECKLIST.md). Images stay in the same storage as video — there is no
    per-slot backend. The operator console still uploads video through this API route
    rather than a signed PUT straight to GCS; that matters only above the 64 MB cap.
- **Still to do, in order:**
  1. Video slots: transcode to HLS 360p/540p/720p (GCP Transcoder API writing back to
     the same bucket, or ffmpeg in a worker). `hls_url` exists and stays empty until
     something writes a playlist name into it; the app plays `video_url` meanwhile. The
     poster is already cut by `mediakit/tasks.py` wherever ffmpeg is installed.
  2. `trainer.signature`: an SVG sanitiser (allow-list of elements and attributes).
  3. `media` on `ExerciseSerializer` (with item 7), on routines/templates (`program.cover`,
     with routine products, plan step 10) and on meals via `Recipe` (`dish.top`). Each
     needs its list view to call `media_map` once, and cached routes to bump their version
     when a `MediaAsset` of that owner type changes (`training_platform/cache_config.py`).
  4. Optional data migration of `profile_picture` -> `trainer.avatar` and `Exercise.image`
     -> `exercise.poster` (the legacy fields stay).

## 3. Date of birth and minimum age

- **Today (before):** `age` integer (validators 13-100) read by BMR, calorie targets (no
  deficit under 18), onboarding completeness, trainer views, the AI context. No DOB.
- **Built:**
  - `CustomUser.date_of_birth` (nullable, no backfill: an age cannot become a date).
    While set, `age` is derived from it on every save and refreshed nightly by
    `users.refresh_ages_from_birth_dates` (00:10), so every existing reader of `age` stays
    right without changing.
  - `MINIMUM_AGE_YEARS = 14` (owner's decision; the design pack says 16).
  - `POST /api/auth/register/` accepts `date_of_birth` (ISO date) and validates it when
    sent. Codes on `field_errors.date_of_birth[0].code`: `under_minimum_age`,
    `date_of_birth_in_future`, `date_of_birth_implausible` (more than 100 years ago). The
    same rules apply on `POST /api/auth/user/update/` and `/api/auth/client/profile/`, and
    to any legacy `age` write below 14 (`under_minimum_age`). Once a DOB is set, a written
    `age` is ignored and the date cannot be removed (`date_of_birth_required`). Those two
    profile endpoints now raise their validation errors through the API envelope, so each
    field error carries its `code` (field keys stay at the top level).
  - Trainers see `age`, never the date (`ClientProfileViewSerializer` unchanged there).
- **Required at sign-up** since the owner's decision 2 (2026-09-19): omitting it answers
  `field_errors.date_of_birth[0].code = required`. No build that omits it was released;
  the shared test helper sends one, with the reason.

## 4. Onboarding fields

- **Today (before):** first/last name, height, weight, gender, `activity_level` (5 codes,
  DB check), free-text `client_goals`, encrypted `specific_injury`. No goal code, place,
  days per week or injury areas.
- **Built** on `CustomUser`, written through the existing
  `POST /api/auth/user/update/` and `POST /api/auth/client/profile/` (no new route):
  - `training_goal`: `build_muscle | fat_loss | strength | general_fitness`. When set,
    `resolve_fitness_goal()` reads it (muscle/strength -> Gain, fat_loss -> Lose,
    general_fitness -> Maintain) before the free-text `client_goals`.
  - `training_place`: `gym | home`. `training_days_per_week`: 1-7.
  - `injury_areas`: `shoulder | lower_back | knee | neck | wrist | ankle | hip | other`,
    encrypted at rest like `specific_injury` (special-category data); `none` is sent as
    `[]`. The free-text note stays `specific_injury`.
  - Activity (OnbActivity's four levels) maps onto the existing codes: beginner ->
    `Sedentary`, light -> `Light`, moderate -> `Moderate`, advanced -> `Active`.
  - `onboarding_missing`: an additive list naming what is still unanswered; the meaning of
    `onboarding_completed` does not change.
  - The approved trainer's view of a client gains the four fields
    (`GET /api/auth/trainer/client-profile/{id}/`).
- **Still to do:** the reminder wheel offers minutes (00/15/30) but reminders fire hourly
  (`workout_reminder_hour`, beat at minute 0): needs `workout_reminder_minute` and a
  quarter-hourly beat. `preferred_language` is settable since BACKEND_BUGS B-005.

## 5. Trainer proof stats

- **Today (before):** `trainer_is_verified` (operator-set), `trainer_is_available`
  (trainer toggle). No request/response timestamps; relation rows are deleted on
  unassign, cancel and erasure; no program-completion record.
- **Built:**
  - `TrainerClientRelation.requested_at` / `responded_at`, stamped on every request,
    re-request, invitation, approval and decline (inside the existing row lock).
  - `TrainerRequestOutcome`: append-only, written in the same transaction as the answer,
    so the history survives the relation being deleted. Privacy registry: deleted with
    either party, and purged after 180 days (the statistic reads 90).
  - `users/trainer_proof.py`, computed on read, one query per statistic for a whole page:
    `typical_response_minutes` (median over the last 90 days of client requests the
    trainer answered, `null` below 3 answers), `response_sample_size`,
    `clients_completed_program` (clients who completed a session for every day of a
    routine this trainer wrote for them), `accepting_clients`, `verified`.
  - Served as `proof` on every `/api/v1/trainers/` card.
- **Still to do:** include chat reply times once two-way chat exists (item 8); a capacity
  limit (`max_clients`) if the owner wants one; verification evidence is plan step 6.

## 6. Trainer matching, roster and profile

- **Today (before):** `GET /api/auth/trainers/public/` and `/client/available-trainers/`
  (accepting trainers only, `?search=`, id order, no proof, no media, price to every
  build). No detail-by-id, no ranking.
- **Built** (`users/trainer_directory.py`, `users/urls_v1.py`):
  - Trainer-side codes on `CustomUser`: `trainer_goal_focus` (goals + `nutrition`),
    `trainer_injury_focus`, `trainer_training_places`, writable on
    `POST /api/auth/trainer/profile/`. Until a trainer fills `trainer_goal_focus`, it is
    inferred from the free-text `trainer_specializations` (English and Arabic keywords).
  - `GET /api/v1/trainers/matches` (client only): ranked accepting trainers with up to
    three reasons, each `{code, text: {ar, en}}`. Codes: `goal_match`,
    `injury_experience`, `training_place`, `program_days`, `fast_response`,
    `completed_programs`, `experience`. First place needs a complete media kit when any
    candidate has one (build guide section 5).
  - `GET /api/v1/trainers/` roster (`?focus=`, `?verified=true`, `?search=`), public,
    including trainers not accepting (flagged).
  - `GET /api/v1/trainers/{id}/` profile: card + bio, certifications, library preview.
  - Every card: `coaching_price` only when `can_show_price(COACHING_1TO1)`; no email.
- **Still to do:** "featured trainers" curation for home (item 9); reviews are not in the
  designs.

## 7. Per-trainer exercise media library

- **Today:** `ExerciseMedia` has no owner; a trainer's own video needs a private copy of
  the exercise, which forks progress. `ExerciseAddMediaView.get` returns media for any
  exercise id to any user (IDOR, see below).
- **Must change:** `MediaAsset` owner `trainer_exercise` keyed by (trainer, exercise), a
  resolution rule (routine author inside a routine, else the approved trainer, else the
  global asset), `media` + `media_source` on `ExerciseSerializer`, trainer library
  endpoints under `/api/v1/trainer/exercise-media/`, cache-version bump for
  `/api/routine/exercises/`.
- **Order:** after item 2's video pipeline.

## 8. Two-way chat (plan step 8)

- **Today:** one-way `POST /api/notifications/message-client/`, stored only as a
  `Notification` (none when the client muted `custom`). No message model, no attachments,
  no audio anywhere, no report/block, no throttle, deprecated echo-only `ws/social/`.
- **Must change:** new app: `Conversation`, `Message` (text | voice | image | video |
  program_update, `transcript`, `client_uuid` idempotency), `MessageAttachment` (media
  pipeline, audio allow-list, inline audio serving), read state, `MessageReport`,
  `UserBlock`; `message-client` also writes a `Message`; FCM `chat_message` event; a WS
  consumer that re-checks auth per frame. Access from the approved relation only.
- **Order:** models + privacy registration, text, image, voice, video, program cards,
  report/block, live WS.

## 9. Server-driven home

- **Today:** no home endpoint; each section's source exists separately (analytics
  summary/streaks, diet progress and advice, notifications, challenges, public templates).
- **Must change:** `GET /api/v1/home` returning `{sections: [{type, id, data}]}` from the
  closed set in the build guide, each section built independently so a locked or failing
  one is dropped, days in the user's `preferred_timezone`, `ready_programs` and
  `media_banner` filtered by the capability flags. Optional `HomeSection` table for
  operator ordering. `trainer_message` voice needs item 8.

## 10. Note to the trainer on a request

- **Built:** `TrainerClientRelation.client_note` (encrypted, max 500). The request accepts
  `note` (and `message`, the name older builds used), replaces it on a re-sent request,
  and clears it when the trainer re-invites. Shown on the trainer's pending list
  (client-initiated rows only) and on the client's request-status. Codes `note_too_long`,
  `invalid_note`. No new route.

## 11. Effort rating and progression

- **Today:** no session rating (per-set RPE 1-10 exists), no progression logic.
- **Must change:** `WorkoutSession.effort_rating` 1-5 (nullable, check constraint),
  writable on a completed session, surfaced on the trainer's client-progress payloads;
  then a computed-on-read `progression` per routine exercise with codes
  `ready_to_increase | keep | reduce | insufficient_data`. The rule is an owner decision.

## 12. Personal records

- **Today:** `prs = 0` is a literal in `GET /api/routine/analytics/summary/`.
- **Must change:** compute on read from `ExerciseSetLog` (ignore reps defaulted to 10 and
  bodyweight sets), return `personal_records` on the completing PATCH of a workout
  session (only on the transition), then make `prs` real.

## 13. Intro story and remote theme

- **Story media:** DONE through item 2 (`app.story.N` on `/api/v1/content/media`). Story
  text, if it must be editable, is a small `IntroStory` table (order, title/body ar+en).
- **Theme:** `GET /api/v1/config/theme` -> `{id, version, valid_until, tokens}`, chosen by
  channel and version only; `ThemeOverride` + change log; the server refuses token names
  outside `tokens.json` and text/background pairs under 4.5:1 so a bad edit cannot ship.

## 14. Offline set logging

- **Built (B-003):** `client_uuid` (unique) on `WorkoutSession` and `ExerciseSetLog`: the
  first POST with it answers 201, a retry 200 with the same row, someone else's id 409
  `idempotency_conflict`. A session's `start_time` is writable on create (at most 30 days
  back), so a workout synced later keeps its clock; `end_time` is checked against it.
- **Still to do:** log a set by `exercise` + session instead of a server progress id, and
  a batch `sync/` endpoint.
- **Before:** no idempotency key and no unique constraint on `ExerciseSetLog`; a retried
  POST duplicated. A single set needs a server `UserExerciseProgress` id up front.
- **Must change:** nullable `client_uuid` + partial unique index on `WorkoutSession` and
  `ExerciseSetLog`, replay returns the original row, log by `exercise` + session, optional
  batch `sync/`.

## 15. Editing a saved set

- **Today:** clients get 403 on PATCH/DELETE of their own set; deletes never recompute
  progress.
- **Must change:** `post_delete` recalculation first, then allow the owner, with an edit
  window code (`set_locked`).

## 16. Coaching by the month

- **Before:** one fee per relationship, held on accept, paid 100% to the trainer at the
  first program; no commission anywhere; the design says "per month".
- **Built** (owner decision 3, `users/coaching.py`):
  - `CoachingPeriod`: one row per month (price, commission rate copied at charge time,
    commission, trainer share, the three ledger references, start/end). A money record:
    kept on erasure, outlives the relation.
  - Month 1: held on accept as before; at the first program it is split — trainer
    `price - commission`, platform `commission` (`COACHING_COMMISSION_RATE`, 20%, to the
    new platform revenue wallet) — and the month starts.
  - Renewal: hourly beat `users.renew_coaching_periods` charges the next month from the
    client's wallet when the last one ends, at the price the client agreed to, split the
    same way, starting where the last ended. Idempotent (row lock + unique start per pair).
  - Unpaid: the coaching FREEZES (item 18; this replaced "3 days' grace, then it
    ends"). Paid months are not refunded.
  - `GET /api/v1/coaching/current/`: status `awaiting_first_program | active | frozen |
    unpaid`, the latest month, `renews_at`, `renewal_reminder`, `frozen_since`, and the
    price by build (`grace_until` kept, always null; `payment_due` no longer sent).
  - A trainer cannot end a paid month the client is in (owner): unassign answers 409
    `coaching_month_active`; `at_period_end: true` sets `ends_at_period_end` and the
    renewal ends the coaching instead of charging (`ending: true` on the status).
- **Still to do:** operator-editable rates and double-entry ledger accounts (plan step 4);
  a client "stop renewing" switch (today leaving is the way to stop).

## 17. Templates become private copies

- **Before:** `templates/{id}/generate/` made one routine for one client, never settled
  the coaching's first month (a program delivered from a template left the coaching
  `awaiting_first_program` and the trainer unpaid), answered 500 on a bad customisation
  or a template row repeated, and let a trainer generate for themselves.
- **Built** (`routine/assignment.py`): `routine_from_template` writes a private copy (both
  languages in `Routine.translations`, `source_template` recorded — a copy, not a link);
  `assign_routine` puts one client on it under a row lock, refuses a routine someone else
  trained on, and settles month 1 for both assign paths. Generate takes `client_ids`
  (1-50, all or nothing: 403 `not_your_client`), `assign: false` (an unassigned draft to
  personalise with `routine-exercises/` and then assign), `name`; codes
  `bad_customization`, `invalid_client_ids`, `template_empty`, `client_ids_need_assign`,
  `generate_too_large` (clients x days above 1000).
  Template writes refuse `template_day_out_of_range`, `template_duplicate_row`,
  `exercise_retired`. A trainer can copy their own template. `routines/?source_template=`.
  Both admins refuse a second client on a routine; the console's duplicate copies days.
  Migration `routine/0022_routine_source_template`.
- **Owner's call, not built:** whether another trainer may generate from someone's
  PUBLIC template (today yes: public means a library, and the copy is theirs).

## 18. Reminders and the freeze

- **Coaching** (`users/coaching.py`): `remind_upcoming` tells every client 3 days before a
  paid month ends (push `coaching_renewal_soon`, `renewal_reminder` on the status). An
  unpaid month sets `CoachingPeriod.frozen_at`: the relation stays, the trainer's
  routines stay readable, and `training_refusal` answers 403 `coaching_frozen` on every
  training write (sessions, set logs, bulk logs, progress, update_progress,
  bulk-complete); the same writes answer `coaching_ended` after the coaching ends, so
  leaving no longer escapes the freeze; `coaching_unpaid` while support has returned the
  month of a standing relation. Training inside a paid month still syncs (a session by
  its start, a dated row by any paid part of its day, and up to the failed renewal).
  Reminders at 0, 3, 7, 14 and 30 days (`frozen_notices`); the hourly run charges again
  as soon as the wallet covers it and that month starts when paid. Workout reminders
  skip routines that cannot be started. A pair with no paid month (free, legacy) is not
  affected. Migration `users/0043_coaching_period_frozen`.
- **Platform plan** (`subscription/`): `Subscription.is_frozen` / `access`; the eight diet
  reads (plans, meals, nutrition, progress) answer 200 to a frozen subscriber whose plan
  had diet, every use route keeps `no_entitlement`; `subscriptions/current/` gains
  `access` and `renewal_due`, `access/check/` gains `frozen`. Daily task
  `subscription.send_renewal_reminders` (17:35 UTC): `subscription_expiring` 3 days
  ahead, `subscription_expired` on days 0, 3, 7, 14, 30. No price or link in any text.
- **Trainer erasure** (decision 15): refused with 409 `trainer_has_active_clients` while
  any relation is approved; the preview reports `active_clients`; no hard delete from
  either admin.
- **Owner's call, not built:** whether a freeze ever ends by itself (today it lasts until
  paid, the client leaves, or the trainer ends it); whether a lapsed PLATFORM plan
  should also freeze routines (today routines follow the coaching only); an instant
  unfreeze on top-up (today within the hour).

## 19. Keep the missed work (planned vs done)

- **Before:** the 409 `unlogged_sets` counted the missing sets and threw the numbers away;
  `confirm_unlogged` stored only `RoutineProgress.status = 'completed'`, so a day closed
  short was indistinguishable from a perfect one in streaks, completion, the roster and
  the achievements. Reps were stored as 10 when the app sent none, so "reps missed" could
  never be trusted.
- **Built** (`routine/outcomes.py`, model `routine.DayOutcome`, migration `routine/0023`):
  one row per (user, routine, day, date), written whenever a day closes — `update_progress`
  (`completed` / `skipped` / `confirmed_unlogged`), `bulk-complete`, a session finished on
  that day, the recalculation completing it by itself (`auto`), and the nightly sweep for a
  day opened and never closed (`unclosed`, `routine.close_open_training_days`, beat 01:15;
  days nobody was allowed to train are skipped). The plan is frozen at closing time, so a
  coach's later edit never rewrites last week; sets synced afterwards still count.
  Registered in `training_platform/privacy/sources.py`.
- **The owner's rules (2026-09-20):**
  - A day closed with sets missing, or skipped, does NOT count as a training day and
    BREAKS the streak: `analytics/streaks/` counts only complete days and answers
    `last_break` (date, routine, day, missed sets, reason). The achievements' workout
    streak excludes the same dates (`achievements/engine.py`). Days closed before this
    existed fall back to their status, with `approximate: true`.
  - Reps must be entered: `POST set-logs/` and `set-logs/bulk-create/` answer 400
    `reps_required`. `as_prescribed: true` fills in the coach's reps for that exercise on
    that routine day (and the prescribed weight when none is sent); 400
    `no_prescribed_reps` when the coach set none.
- **Wired into:** `analytics/adherence/` (new route), the home week
  (`v1/analytics/recent-progress/` — which also answered 500 to everyone until now: it
  annotated `day` over `WorkoutSession.day`), `routine-progress` rows (`outcome`, one
  subquery per page, no N+1), the update_progress answer, the trainer's
  `client-progress/{id}/` and roster `this_week`, and the AI assistant's progress tool.
- **Honest copy only:** share of planned sets and reps done, planned volume left where a
  weight was prescribed, the trend against last week. Never calories, muscle, fat loss or
  "faster results".
- **Not built, owner's call:** step 2 of the projection idea below (item 21).

## 21. Expected progress: trend, projection, and a coach who notices (the owner, 2026-09-20)

The owner: "build expected enhancements in progress for the user based on his goal and
enhancements in the body — read how his weight grows in chest while keeping the set volume
and expect how stronger he will be there in two weeks", wired everywhere it matters,
including the app, to make users happy and make them share it. Built on item 19: the day
outcomes are what make a projection honest, because they say how much of the plan was
actually done.

### What it is

| Step | What a person sees | Where it comes from |
|---|---|---|
| 1. Strength trend | "Chest press 60 kg x 8 -> 70 kg x 8 in 6 weeks, +16%", the date of each personal best, which exercises are rising, flat or slipping | `ExerciseSetLog`: the best set of each training day, an estimated one-rep max, the day's volume |
| 2. Projection | "On this trend, in 2 weeks you are around 72-75 kg x 8 — if you keep finishing about 90% of your sets" | A robust trend over the last 8 weeks of weekly bests, a band from the residuals, conditioned on `DayOutcome` adherence |
| 3. The coach notices | A ranked "needs attention" list with the numbers: adherence fell against their own 4-week average, no training for N days, strength slipping, coaching frozen, a program never started | `DayOutcome` + sessions + `users/coaching.py` state |

### Improvements on top (mine, and why)

- **Personal-record push** the moment someone beats their best on an exercise. The most
  shareable moment in the app, and the data is already there.
- **Weekly recap** to the client and **weekly digest** to the coach, Sunday evening: what
  the week held, what is worth a look. This is what brings people back.
- **A share block**: the trend endpoint returns a short bilingual headline the app can draw
  on a card, so sharing needs no second backend pass.

### Built (2026-09-20)

`routine/strength.py` — no new table; everything reads `ExerciseSetLog` and the day
outcomes of item 19.

- `points()` one entry per training day of an exercise: the best set, its estimated
  one-rep max (Brzycki, the one formula this repo has), and the day's volume. Sets
  re-sent without the app's id are collapsed by set number, so a retry cannot inflate a
  day; sets with no weight or reps (a run, a whole day logged at once) carry no strength
  and are left out.
- `trend()` direction (`rising` / `steady` / `easing`), change since the first day in the
  window, the personal best, and the per-week movement.
- `projection()` a Theil-Sen line (the median of pairwise slopes — one deload week cannot
  steer it, which least squares would) over each week's MEDIAN training day, with a band
  from the residuals' median absolute deviation and a floor of 2%. Silent until 3
  training days spread over 2 weeks: `{available: false, reason: "insufficient_data",
  needs: {...}}`. A flat or easing trend never projects a decline — the range holds
  around where the person is.
- `personal_best()` / `is_personal_record()` — heavier than every earlier set, the same
  definition `analytics/summary`'s `prs` has always used. One product, one meaning.

Wired into: `GET analytics/strength/` (overview and per exercise, with `share`), the home
week (`strength.headline`), the trainer's client view (`strength`) and roster
(`this_week.trend`, from each client's own first and last session), the new
`trainer/client-progress/attention/`, and the AI assistant's progress tool. `POST
set-logs/` answers `personal_record` and sends the push. Weekly, Sunday evening:
`weekly_recap` to clients and `trainer_weekly_digest` to trainers
(`routine.send_weekly_progress`). A client whose coaching is paused or over gets no recap
and is listed to the coach as that, not as "slipping".

### Rules this is built under

- No new tables if reads over a bounded window plus the existing private cache are enough.
- Silent until there is enough data: a projection needs at least 3 training days of that
  exercise spread over at least 2 weeks; otherwise `insufficient_data` with what is missing.
- A range, never a single number, and never a scary one: a flat or downward trend reads as
  "holding steady", with the missed sets named as the lever.
- NO health-outcome claims (calories, weight, muscle, fat, "faster results"), no medical
  advice, no price or purchase wording in any push. Arabic first, every string through
  gettext. Identical on every build.
- Deterministic maths (a weighted linear fit), not ML: testable, explainable to a client
  and a coach, and honest about its own uncertainty.

### App-level wiring (what each screen reads)

| Screen | Reads |
|---|---|
| Home | `v1/analytics/recent-progress/`: `adherence` (item 19) plus a `strength` headline and its projection when there is one |
| Progress / exercise detail | `analytics/strength/?exercise_id=` — the per-day series, the deltas, the personal best, the projection, the share headline |
| Progress overview | `analytics/strength/` — top movers, plateaus, personal bests this period |
| Coach: client | `trainer/client-progress/{id}/` — the same trend and adherence for that client |
| Coach: roster | `trainer/client-progress/recent/` — per client: this week, trend arrow, risk flag |
| Coach: needs attention | `trainer/client-progress/attention/` — the ranked list with reasons |
| Notifications | `personal_record`, `weekly_recap`, `trainer_weekly_digest` |

### Reviewed and repaired (2026-09-20, commit 8e04eb2)

Ten reviewers read items 19-21 with no memory of writing them. What they found and what
the next session must not undo:

| Was wrong | Now |
|---|---|
| The sweep closed only `in_progress` days, so a day where every exercise got some sets but none finished was never recorded | Swept when the day carries work; an untouched day is still never a miss |
| Sets were counted as set-log ROWS (only `client_uuid` is unique) | Distinct set numbers, in `outcomes.day_work` AND in `UserExerciseProgress.completed_sets` |
| A day skipped and later trained stayed incomplete for ever | `skipped` yields to a working reason once work exists |
| A projection was drawn from points of any age | `STALE_AFTER_DAYS = 21`, else `reason: "stale"` |
| A falling trend's floor was the declining forecast | The floor is the band below today |
| `analytics/strength?exercise_id=` read any exercise | `visible_exercises_for`, plus "an exercise you trained is yours to read" |
| `personal_best` excluded only the judged row | `pk__lt`: everything logged before it |
| `refusals_for` and `training_refusal` disagreed after a client left and came back | Both read the APPROVED relation's months |
| A 20-rep set set the one-rep max | `MAX_REPS_FOR_ESTIMATE` decides; long sets are work, not strength |

Traps for the next session: the home cache key carries the language, so
`training_platform/signals.recent_progress_cache_keys` is the only place that knows how to
bust it. `accessible_user_ids` answers None for an admin — that means "everyone", not
"nobody". A test that patches `training_refusal` is testing the patch; seed a
`CoachingPeriod` with its `relation` set, the way `open_period` writes one.

## 20. Trainer pipeline gaps (found while mapping it, 2026-09-20)

Security (sent to their own sessions): a client can move a routine-progress row to any
routine and date; `POST/PATCH /api/routine/exercises/` skips the image content check;
~~`GET exercises/{id}/add-media/` shows any exercise's media~~ — FIXED 2026-09-20: all
three methods resolve the id through `routine.permissions.visible_exercises_for` (the rule
the list already used, now shared) and answer an invisible exercise exactly as a missing
one; writes stay owner-or-admin (`tests/test_exercise_media_visibility.py`).
Product gaps: a trainer cannot
retire an exercise (`is_active` not writable) though the 409 says to; deleting an unlogged
exercise cascades it out of every routine and template; no "save routine as template";
template rows cannot hold weight, notes or duration; `assign_to_client` accepts an empty
or retired routine and pays month 1 for it; `Routine.days` has no upper bound; the three
assign 400s share one code; clients are not told when a routine is unassigned or edited,
or when a diet plan is written for them; the trainer cannot see a client's freeze, diet
adherence, or whether the activity level was answered; `my_clients_progress` is a stub
and `trainer/clients/` follows the pointer, not the relation; a diet plan never settles
month 1; a coached client needs their own diet subscription to open the trainer's plan;
a client who generates a plan silently retires the trainer's plan; unverified trainers
are refused on diet but not on routines; routine days are ordinal, not weekdays.

## Migration-safe order

Every step is additive: new tables, or new nullable/constant-default columns (metadata
only on Postgres), no backfill that invents data, no rename.

1. `mediakit/0001` (new table). Done.
2. `users/0040`: DOB, onboarding fields, trainer focus codes, relation timestamps,
   `TrainerRequestOutcome`. Done.
3. Object storage + volume fix (settings, no schema).
4. Media video/SVG pipeline (columns on `MediaAsset` for `hls_url`, `poster`, `duration`).
5. Item 10 note + item 16 `CoachingPeriod`: `users/0041`. Done.
6. Item 14 `client_uuid` columns + partial unique indexes (before any real data).
7. Item 11 `effort_rating` (+ check constraint).
8. Item 12 (no schema if computed on read).
9. Item 7 trainer exercise media (owner type only, no new table).
10. Item 8 chat app (new tables).
11. Item 9 home (no schema; optional `HomeSection`).
12. Item 13 theme (new tables).
13. Item 17 `routine/0022` (`Routine.source_template`, nullable FK). Done.
14. Item 18 `users/0043` (`CoachingPeriod.frozen_at` nullable, `frozen_notices` default 0). Done.

## Owner's decisions (2026-09-19)

| # | Decision | Answer | Done |
|---|---|---|---|
| 1 | Minimum age | 14 (the Dob screen's "16" is the app's to change) | `MINIMUM_AGE_YEARS = 14` |
| 2 | Require the date of birth at sign-up | Yes | `required=True`, code `required` |
| 3 | Coaching "per month" | Yes, and the commission comes off every renewal too | Item 16 |
| 4 | Hide the coaching price from store builds on the legacy payloads | Not answered; the spec's own acceptance item ("no price strings reach store builds") decided it | Value null on those builds, key kept (BACKEND_BUGS B-007) |
| 5 | Match unverified trainers | Keep both | Unchanged: ranked below verified |
| 6 | Grade dish photos black and white | No, food keeps its colour | `dish.top` `mono=False` |
| 7 | Where videos live | Google Cloud Storage | Item 2 step 1; bucket + service account owed |

| 8 | A trainer who ends coaching mid-month | A trainer cannot end a paid month the client is in | `trainer/unassign-client/` 409 `coaching_month_active`; `at_period_end: true` stops the renewal instead |
| 10 | Suspended account at sign-in | Tell them, after the right password | 403 `account_disabled` (BACKEND_BUGS B-004) |
| 11 | Completing a day with unlogged sets | Allowed, but warn first | 409 `unlogged_sets` until `confirm_unlogged` (B-011) |
| 12 | Notification settings | The backend's call | Settings govern pushes; `all` is a master switch; the inbox always keeps the row (B-014) |
| 13 | Routines | Private, coach to one client; a trainer lists a few public routines on their page | One client per routine; clients never see each other; `public_routines` on the profile (B-001) |
| 14 | Client rate limit | 1500 an hour | `training_platform/middleware.py` |
| 15 | A trainer deletes the account mid-month | Never while he has active clients | 409 `trainer_has_active_clients` (item 18) |
| 16 | Templates | Reusable; each client gets a private, personalised copy | Item 17 |
| 17 | When a month or plan ends | Remind 3 days before; after it ends the last diet plan and the routine stay visible and cannot be used; keep asking | Item 18 |
| 9 | Grace before unpaid coaching ends | Replaced by decision 17: no grace, it freezes | Item 18 |
| 24 | A day closed with sets missing | It must not count, and it breaks the streak, with the day pointed at (2026-09-20) | Item 19: `analytics/streaks/` `last_break`, achievements too |
| 25 | Reps on a logged set | Must be entered; a button uses the coach's reps (2026-09-20) | Item 19: `reps_required`, `as_prescribed` |
| 22 | A plan from an unanswered activity level | Refuse, and tell the client to choose it (2026-09-20) | 400 `activity_level_required`; trainer plans `client_activity_level_required` (BACKEND_BUGS B-027) |
| 23 | A picture sent to `user/update/` | Refuse it (2026-09-20) | 400 `profile_picture_read_only` (BACKEND_BUGS B-026) |

## Decisions still owed

| # | Decision | What the code does now |
|---|---|---|
| 18 | Does a freeze ever end by itself (e.g. after 90 days)? | It lasts until paid, the client leaves, or the trainer ends it |
| 19 | Should a lapsed platform plan also freeze routines? | No: routines follow the coaching they came from |
| 20 | May another trainer generate from someone's public template? | Yes (public = library) |
| 21 | A client "stop renewing" switch for coaching | Not built; leaving is the way to stop |
