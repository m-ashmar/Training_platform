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
| 2 | Media slots + operator console | XL | Images DONE; video, SVG, object storage TODO |
| 3 | Date of birth at sign-up, minimum age | M | DONE |
| 4 | Onboarding fields: goal, place, days, injury areas | M | DONE |
| 5 | Trainer proof stats | L | DONE |
| 6 | Trainer matching with reasons (+ roster and profile endpoints) | L | DONE |
| 7 | Per-trainer exercise media library | L | TODO (needs 2) |
| 8 | Two-way chat: voice, image, video, program cards, report, block | XL | TODO = plan step 8 |
| 9 | Server-driven home + day timeline | L | TODO (needs 2, 8 for voice) |
| 10 | Note to the trainer on a request | S | TODO |
| 11 | Effort rating 1-5, progression suggestion | L | TODO |
| 12 | Personal records (always 0 today) | M | TODO |
| 13 | Intro story content + remote theme override | M | Story media DONE (item 2); theme TODO |
| 14 | Offline set logging idempotency (build guide section 6) | M | TODO |
| 15 | Owner may edit/delete a saved set (build guide section 6) | M | TODO |

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
- **Still to do, in order:**
  1. Object storage (S3/R2 behind `USE_EXTERNAL_MEDIA_STORAGE`) and the volume permission
     fix for uploads (`/data` is root-owned at runtime; `FileSecurityValidator` calls
     `makedirs` on every upload). Video cannot pass the 15 MB body cap: needs presigned
     direct upload.
  2. Video slots: transcode to HLS 360p/540p/720p + poster (ffmpeg, a worker that can read
     the file). Serialized as an additional `hls_url` + `poster` in the same slot object.
  3. `trainer.signature`: an SVG sanitiser (allow-list of elements and attributes).
  4. `media` on `ExerciseSerializer` (with item 7), on routines/templates (`program.cover`,
     with routine products, plan step 10) and on meals via `Recipe` (`dish.top`). Each
     needs its list view to call `media_map` once, and cached routes to bump their version
     when a `MediaAsset` of that owner type changes (`training_platform/cache_config.py`).
  5. Optional data migration of `profile_picture` -> `trainer.avatar` and `Exercise.image`
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
- **Not yet required at sign-up**, to keep `register/` additive: a caller that omits the
  date still gets an account (and `onboarding_missing` lists `date_of_birth`). Requiring
  it is decision 2 below; the change is `required=True` on
  `CustomRegisterSerializer.date_of_birth` plus the shared test helper.

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
  quarter-hourly beat. `preferred_language` still cannot be set (plan step 5).

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

- **Today:** `ClientRequestTrainerView` reads `message` and drops it
  (`users/views.py`, "TODO: Add request message support").
- **Must change:** `TrainerClientRelation.note` (encrypted text, max 500), accept `note`
  (and `message` as an alias), reset it on a re-request, return it on the trainer's
  pending list (client-initiated rows only) and on request-status. No new route.

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

- **Today:** no idempotency key and no unique constraint on `ExerciseSetLog`; a retried
  POST duplicates. A single set needs a server `UserExerciseProgress` id up front.
- **Must change:** nullable `client_uuid` + partial unique index on `WorkoutSession` and
  `ExerciseSetLog`, replay returns the original row, log by `exercise` + session, optional
  batch `sync/`.

## 15. Editing a saved set

- **Today:** clients get 403 on PATCH/DELETE of their own set; deletes never recompute
  progress.
- **Must change:** `post_delete` recalculation first, then allow the owner, with an edit
  window code (`set_locked`).

## Migration-safe order

Every step is additive: new tables, or new nullable/constant-default columns (metadata
only on Postgres), no backfill that invents data, no rename.

1. `mediakit/0001` (new table). Done.
2. `users/0040`: DOB, onboarding fields, trainer focus codes, relation timestamps,
   `TrainerRequestOutcome`. Done.
3. Object storage + volume fix (settings, no schema).
4. Media video/SVG pipeline (columns on `MediaAsset` for `hls_url`, `poster`, `duration`).
5. Item 10 note (one column).
6. Item 14 `client_uuid` columns + partial unique indexes (before any real data).
7. Item 11 `effort_rating` (+ check constraint).
8. Item 12 (no schema if computed on read).
9. Item 7 trainer exercise media (owner type only, no new table).
10. Item 8 chat app (new tables).
11. Item 9 home (no schema; optional `HomeSection`).
12. Item 13 theme (new tables).

## Decisions owed by the owner

| # | Decision | What the code does now |
|---|---|---|
| 1 | Minimum age: owner said 14 on 2026-09-19, the design pack's Dob screen says 16 | 14 (`MINIMUM_AGE_YEARS`); the Dob copy must match |
| 2 | Require the date of birth at sign-up (breaks callers that omit it; none are live) | Optional, validated when sent |
| 3 | The design prices coaching "per month" (ل.س شهرياً); the server charges one fee per relationship | Unchanged; the app must not say "monthly" until coaching periods exist |
| 4 | Hide `trainer_hourly_rate` from iOS on the five legacy trainer payloads | Not changed (additive rule); new endpoints hide it |
| 5 | Are unverified trainers matched? | Yes, ranked below verified ones; the card says `verified: false` |
| 6 | `dish.top` graded black and white like everything else | Yes (brand rule "ember is the only colour") |
| 7 | Video storage/CDN provider and budget (HLS) | Not built |
