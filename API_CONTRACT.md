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
     {username, email, password1, password2, phone_number, user_type, date_of_birth}
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
| `trainer_assignment_request` | `trainer_id` |
| `trainer_unassignment` | `trainer_id` |
| `client_request_received` | `client_id` |
| `client_request_approved` | `trainer_id` |
| `client_request_rejected` | `trainer_id` |
| `client_request_cancelled` | `client_id` |
| `client_unassigned_trainer` | `client_id` |
| `routine_assignment` | `related_object_id`, `related_object_type` |
| `session_completed` | `related_object_id`, `related_object_type` |
| `session_reminder` | `reason` (`scheduled` \| `drift`), plus `routine_id`+`day` when scheduled, or `days_since_last_workout` when drift |
| `progress_milestone` | `milestone` (e.g. `streak-7`, `sessions-100`) |
| `custom` | `message_id`, `trainer_id` — a message from the user's trainer |
| `chat_message` | `conversation_id`, `message_id` — a message in the trainer–client chat. Sent only when the recipient has opened that conversation since the last `chat_message` about it, so a burst of messages is one push |
| `coaching_renewed` | `trainer_id` — a new month of coaching was charged from the wallet |
| `coaching_renewal_soon` | `trainer_id` — the paid month ends in a few days |
| `coaching_payment_due` | `trainer_id`, `status` (`frozen`) — the month ended unpaid, so the coaching is paused; repeated on each reminder step while it stays paused |
| `coaching_ended` | `user_id` — the other person. The grace period ran out unpaid; sent to both |
| `coaching_finished` | `user_id` — the other person. Coaching ended for a reason other than money; sent to both |
| `subscription_expiring` | `subscription_id`, `ends_at` (ISO 8601) |
| `subscription_expired` | `subscription_id`, `ended_at` (ISO 8601) |
| `checkout_expired` | `payment_id` — the checkout was not completed in time |
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
| `ai_consent_required` | every `/api/ai/` route except `DELETE data/`, and the socket | The AI coach has not been allowed to read this person's data |

A turn that ends in an `error` frame is **not** charged against the daily message
allowance; each turn closes with `{"type": "rate_limit", "remaining", "resets_at"}`
carrying what is actually left.

A `status` frame is `{"type": "status", "content", "code"}`. Render the `code`; the
translated `content` is there for a build that predates it. The set, in the order a
turn produces them: `connected` (on connect), `getting_ready`, then one per pass of the
tool-calling loop — `thinking`, `checking_data`, `analyzing`, `looking_deeper`,
`almost_done` (the last repeats if the loop runs longer).

| Method | Path | Auth | View |
|---|---|---|---|
| `DELETE` | `/api/ai/data/` | auth | `GDPRDataDeleteView` |
| `POST` | `/api/ai/feedback/` | auth, HasAIAdviceAccess | `FeedbackView` |
| `GET` | `/api/ai/sessions/` | auth, HasAIAdviceAccess | `ChatSessionListView` |
| `GET` | `/api/ai/sessions/{session_id}/` | auth, HasAIAdviceAccess | `ChatSessionDetailView` |
| `GET` | `/api/ai/sessions/{session_id}/messages/` | auth, HasAIAdviceAccess | `ChatMessageListView` |

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
| `POST` | `/api/auth/forgot-password/` | public | `PasswordResetRequestView` |
| `POST` | `/api/auth/forgot-password/confirm/` | public | `PasswordResetConfirmView` |
| `POST` | `/api/auth/forgot-password/verify/` | public | `PasswordResetVerifyView` |
| `GET` | `/api/auth/health/` | public | `HealthCheckView` |
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

### Chat

Text messages between a trainer and the client they coach — text only in this pass (no
voice, image, video or program card). Exactly one conversation per pair. Nothing here
differs by build.

Reading and writing are decided separately (the owner, 2026-09-19). Being in the
conversation is what lets you READ it, for ever; writing asks
`chat.permissions.may_message`, and a coaching that is frozen, unpaid or ended refuses
the send with the same `code` (and `trainer_id`) every training write answers with. The
conversation row carries `can_send` and `refusal` so the app can explain a closed
composer without posting first. A conversation that is not the caller's is **404**, never
403.

`POST .../messages/` may carry `client_uuid`, the app's own id for the message: the first
send answers 201, a retry answers **200** with the same message, and an id already used
by the other person in that thread answers 409 `idempotency_conflict`. `DELETE
messages/{id}/` tombstones the sender's own message — the row stays in the thread with
`deleted: true` and an empty `body`, and nothing is ever hard-deleted.

Live: `ws://host/ws/chat/{conversation_id}/?token=<access JWT>`. Client frames
`{"type": "message.send", body, client_uuid?}` and `{"type": "message.read"}`; server
frames `message.new`, `message.read`, `message.deleted` and
`{"type": "error", code, detail}`. A socket send runs the same checks and the same
serializer as the REST route. Close codes: 4001 no usable token, 4004 not your
conversation.

Push: `chat_message`, keyed on the conversation, sent only when the recipient has opened
that thread since the last time they were told about it — ten messages in a minute are
one push.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/chat/conversations/` | auth | `ConversationListView` |
| `POST` | `/api/v1/chat/conversations/start/` | auth | `ConversationStartView` |
| `GET, POST` | `/api/v1/chat/conversations/{id}/messages/` | auth | `ConversationMessagesView` |
| `POST` | `/api/v1/chat/conversations/{id}/read/` | auth | `ConversationReadView` |
| `DELETE` | `/api/v1/chat/messages/{id}/` | auth | `MessageDeleteView` |

### Diet

After the platform plan ends (the owner, 2026-09-19) the subscriber is **frozen**: the
routes marked `HasDietAccessOrFrozenRead` keep answering 200 (the plans and progress stay
readable), every other diet and AI route answers 403 `no_entitlement` as before. The app
tells "ended" from "never subscribed" by `access` on `subscriptions/current/`
(`active | frozen | inactive`), and shows the renewal reminder while `renewal_due` is
true (3 days before the end). Pushes: `subscription_expiring` (3 days before),
`subscription_expired` (on the day it ends, then after 3, 7, 14 and 30 days). No price and
no link in either, on any build.

No plan is generated from an activity level the client never chose (the owner,
2026-09-20): `plans/generate/` and `plans/generate-rule/` answer 400
`activity_level_required` (`field: "activity_level"`) until it is written, and a trainer's
plan without `daily_calories` answers 400 `client_activity_level_required`.

`advice/latest/` is **not** an AI route, despite the `has_ai_advice` entitlement's
name: the sentence is composed by `diet/tasks.py` from the client's goal, their plan's
calorie target and their BMI, so it carries the diet guard and neither `HasAIAdviceAccess`
nor the `ai_consent_required` guard. Everything the AI coach actually answers is under
`/api/ai/` and carries both.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/diet/v1/advice/latest/` | auth, HasDietAccess | `DailyAdviceView` |
| `POST` | `/api/diet/v1/client/meals/interact/` | auth, HasDietAccess | `ClientMealInteractionView` |
| `GET` | `/api/diet/v1/client/meals/{meal_id}/` | auth, HasDietAccessOrFrozenRead | `ClientMealDetailsView` |
| `POST` | `/api/diet/v1/client/meals/{meal_id}/complete/` | auth, HasDietAccess | `MealCompletionView` |
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

### Home (server-driven)

The screen the app opens on, and the fourth layer of the design canvas's build guide.
The server answers an ORDERED list of typed sections and the app renders what it is
handed: it must cope with any subset, in any order, and SKIP a `type` it does not know,
which is what lets an old build survive a new section.

`{"role", "as_of", "refresh_after_seconds", "sections": [{"type", "data"}]}`. A section
is `{"type", "data"}` and nothing else, and one with nothing to say is left out rather
than sent empty. `role` is the account's shell (`client`, `trainer`, `agent`, `admin` —
`admin` is the operator); only the client shell is designed, so the other three get
`"sections": []` today rather than a refusal.

The closed set of types: `workout_hero`, `week_stats`, `nutrition_today`,
`trainer_message`, `ai_advice`, `challenge_rank`, `find_trainer_hero`,
`setup_checklist`, `featured_trainers`, `ready_programs`, `media_banner`. The two home
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
per user, per build and per language for 120 s and retired at once when a workout
session is started, changed or finished, a day closes, a chat message arrives or is
taken back, the client reads the thread, a block is made or lifted, or the client's
coaching relation changes.

Additive in `workout_hero`: an open session begun while the coaching was paid answers
`cta: {"state": "continue", "session_id"}` after the coaching freezes, because the
server still accepts its sets and its finish (judged at the session's start). Additive
in `trainer_message`: `message.deleted` (bool). A newest message its sender took back
keeps the card with `body: ""` and `deleted: true`, as the conversation list shows it.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/home` | auth | `HomeView` |

### Notifications

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
which is not built yet). Every media URL is signed and expires after an hour
(`MEDIA_SIGNED_URL_TTL`): use what the API just returned, never a stored copy.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/content/media/` | public | `AppMediaView` |
| `POST` | `/api/v1/content/media/video/` | auth (owner of the target, or admin) | `VideoUploadView` |
| `GET` | `/api/v1/content/media/exercise/{id}/` | auth | `ExerciseMediaView` |

`POST /api/v1/content/media/video/` is `multipart/form-data`: `file`, `slot`,
`owner_type`, `owner_id`, optional `alt_ar`/`alt_en`. It is the one route whose body may
exceed 15 MB (cap `MEDIA_VIDEO_MAX_BYTES`, 64 MB; duration cap `MEDIA_VIDEO_MAX_SECONDS`,
90 s). The container is decided by the FILE'S BYTES, never by its name or its
`Content-Type`: MP4/MOV and WebM only. Codes: `media_file_required`,
`media_unknown_slot`, `media_unknown_owner`, `media_slot_is_not_video`,
`media_not_a_video`, `media_video_too_large`, `media_video_too_long`, plus
`payload_too_large` (413) from the body cap. Answers on both spellings, with and without
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
   prescribed it is the one demonstrating it;
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
`media_wrong_ratio`, `media_too_small`, `media_unsupported_format`), `not_a_trainer`
(403) and `not_found` (404). A catalogue asset, another coach's asset, an asset on an
exercise the caller may not see and an id that does not exist all answer the same 404,
byte for byte.

`POST /api/v1/content/media/video/` refuses `owner_type=trainer_exercise` with 403: the
coach's library has one way in, so the cap lives in one place.

### Trainers (design v2)

One card shape for all three (`users/trainer_views.py`): `proof` (platform-verified
numbers), `media`, and `coaching_price` + `money_type` only on a build that may show a
coaching fee. Matches carry up to three `reasons`, each `{code, text: {ar, en}}`.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/trainers/` | public | `TrainerRosterView` |
| `GET` | `/api/v1/trainers/matches/` | auth (client) | `TrainerMatchesView` |
| `GET` | `/api/v1/trainers/{id}/` | public | `TrainerDetailView` |

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
the month's end, or an account was suspended). A trainer cannot end a paid month the
client is in (`trainer/unassign-client/` answers 409 `coaching_month_active`;
`at_period_end: true` stops the renewal instead), and cannot delete the account while
coaching anyone (`DELETE privacy/erase/` answers 409 `trainer_has_active_clients`).

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

Templates: a template is reused for any number of clients and every client gets a
private copy (`templates/{id}/generate/`: `client_id`, or `client_ids` for several, or
`assign: false` for an unassigned draft to personalise and then `assign_to_client`).
`routines/?source_template=<id>` lists the copies made from one template.

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/coaching/current/` | auth (client) | `CurrentCoachingView` |

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
`diet_plan`, `exercise`, `social_post` or `comment`; `reason` is `spam`, `harassment`,
`nudity`, `violence`, `scam`, `impersonation` or `other`; `note` is the reporter's own
words, 1000 characters at most.

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
Neither can follow the other: `user_blocked` (403) on `POST /api/social/follows/` and
`POST /api/social/follows/follow_user/`, with no push sent. A follow made before the
block is kept, hidden from both, and shows again when the block is lifted.
Blocking does not hide a challenge from either of them, but a blocked person is
dropped from its leaderboard — with the ranks left as they were, so the board still
reads 1, 2, 4.

Every count agrees with the list it summarises, for the viewer reading it:
`followers_count`, `following_count` and `posts_count` on a public profile, and
`likes_count` and `comments_count` on a post and a comment, all exclude the rows a
block hides. So two people looking at the same post can be shown different numbers,
and neither number ever exceeds what that person can actually fetch.

In the app's "report and block" flow, call `reports/` FIRST: what a blocked person
wrote is no longer visible to the reporter, and therefore no longer reportable.

**AI consent.** `GET ai-consent/` → `{granted, policy_version, granted_at,
revoked_at}`; never asked reads as all-null with `granted: false`, which is how the
app tells "not asked yet" from "said no" (`revoked_at` set). `POST ai-consent/`
`{granted, policy_version}` returns the same body; `policy_version` is required when
granting and ignored when revoking.

Until it is granted, the AI coach refuses: `403 ai_consent_required` on every
`/api/ai/` route except `DELETE /api/ai/data/` (deleting what the assistant holds has
to work after consent is withdrawn — that is the commonest reason to ask), and over
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
can_view_entitlements, can_view_wallet, can_transfer_wallet}` — all booleans but `channel`.

The flags are enforced by the server, not only rendered by the app:

| `code` | Status | When |
|---|---|---|
| `app_channel_required` | 400 | The capabilities endpoint, or any endpoint that opens a checkout or moves wallet money, called without a known `X-App-Channel` |
| `not_available_on_channel` | 403 | `POST v1/subscriptions/` (paid plan), `POST v1/subscriptions/{id}/renew/`, `POST v1/gateways/`, `POST wallet/client/transfer/`, `POST wallet/agent/topup/proxy`, and asking or accepting a trainer who charges, from a build whose flag for that kind of money is off |

On a build with `can_show_prices: false` the plan endpoints omit `price`, `subscriptions/current/`
omits `plan_price`, and `GET v1/gateways/` answers an empty list. Additive fields:
`money_type` on payments and wallet transactions (`PLATFORM_SUB`, `COACHING_1TO1`,
`ROUTINE_PRODUCT`, `WALLET_MOVE`) and `source` on subscriptions (`shamcash`, `agent_wallet`,
`operator_grant`, `trial`, reserved: `apple_iap`, `google_play`; blank until entitled).

### Remote theme (a skin without a release)

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/v1/config/theme/` | public | `ThemeView` |

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

A theme carries no copy and no links, by construction: there is no field in this response
that holds a sentence or a URL. That is how the store rule — remote themes may not carry
prices, purchase buttons or payment links into `play` or `ios` builds — is met.

`ETag: "<version>"`, `Cache-Control: public, max-age=0, must-revalidate`,
`Vary: X-App-Channel, X-App-Version`. Send the tag back as `If-None-Match` and the repeat
fetch is `304 Not Modified` with no body.

| `code` | Status | When |
|---|---|---|
| `app_channel_required` | 400 | No `X-App-Channel`, or one this server does not know |

### Privacy (GDPR)

| Method | Path | Auth | View |
|---|---|---|---|
| `DELETE, GET` | `/api/privacy/erase/` | auth | `PersonalDataEraseView` |
| `GET` | `/api/privacy/export/` | auth | `PersonalDataExportView` |

### Routines & Workouts

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
  `ai_advice`, `priority_support`, `max_meals_per_day`, `max_routines`). Only
  `plan_price` is withheld from a build that may not show prices.
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
  purchase never consults the rate and is never refused for this.
- **A coaching price is in the client's currency too.** `GET /api/v1/coaching/status/`
  gains `coaching_currency` beside `coaching_price`; the price is what the client will
  actually be charged, and both are null together when the rate needed to quote it is
  missing or stale. STILL THE PRICE OF RECORD, in the TRAINER's currency and with no
  unit beside it: `trainer_hourly_rate` on the trainer lists and `coaching_price` on a
  trainer card — see the gap noted in `SIDE_BUGS.md`.
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
`reps` (or `as_prescribed`), `routine_id`, `day`, `workout_session_id`, `notes`, `rpe`.
The server resolves or creates the progress row from `(you, exercise, date)`. Idempotent
per `client_uuid`, exactly as the single POST: a re-sent batch writes nothing and answers
the same ids. Partial success — each entry answers `status` `created`, `duplicate` or
`error` with a `code` (`bad_row`, `client_uuid_required`, `idempotency_conflict`,
`bad_date`, `date_in_future`, `date_too_old`, `exercise_required`, `exercise_not_found`,
`session_not_found`, `routine_not_found`, `reps_required`, `reps_out_of_range`,
`no_prescribed_reps`, `weight_out_of_range`, `set_number_out_of_range`,
`rpe_out_of_range`, `invalid_set`, or the coaching refusal with `trainer_id`) — and one
bad row never fails the batch. The whole answer is `{results, count, created,
duplicates, errors}`, and a set that beat everything before it carries
`personal_record: true` and sends the push.

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
routines you were assigned or wrote, so a filter can only narrow it.

**A workout that is still running** is `active` AND begun within the last twelve hours
(`routine/sessions.py`). Past that it was forgotten, not left running:
`workout-sessions/?status=active` does not list it, home does not offer to continue it,
and the nightly `routine.abandon_forgotten_sessions` writes `abandoned` on the row —
a value the schema has always had and nothing ever wrote. The sets logged inside an
abandoned session are untouched and still count everywhere they counted before; only
the session stops claiming a workout is in progress. `end_time` stays null, because
nobody knows when the person stopped.

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
| `POST` | `/api/routine/exercises/create-with-image/` | auth | `ExerciseCreateWithImageView` |
| `DELETE, GET, POST` | `/api/routine/exercises/{exercise_id}/add-media/` | auth | `ExerciseAddMediaView` |
| `DELETE, POST` | `/api/routine/exercises/{exercise_id}/image/` | auth | `ExerciseImageUploadView` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/exercises/{id}/` | IsAdminOrOwnerOrReadOnly | `ExerciseViewSet` |
| `GET, POST` | `/api/routine/routine-exercises/` | IsAdminOrOwnerOrReadOnly | `RoutineExerciseViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/routine-exercises/{id}/` | IsAdminOrOwnerOrReadOnly | `RoutineExerciseViewSet` |
| `GET, POST` | `/api/routine/routine-progress/` | (IsTrainerOrAdmin&IsClientOrAssignedTrainer) | `RoutineProgressViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/routine-progress/{id}/` | (IsTrainerOrAdmin&IsClientOrAssignedTrainer) | `RoutineProgressViewSet` |
| `GET, POST` | `/api/routine/routines/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `GET` | `/api/routine/routines/my_clients_progress/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/routines/{id}/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/assign_to_client/` | IsTrainerOrAdminForAssignment | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/copy/` | auth (the routine's trainer) | `RoutineViewSet` |
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
| `GET, POST` | `/api/routine/user-exercise-progress/` | auth | `UserExerciseProgressViewSet` |
| `POST` | `/api/routine/user-exercise-progress/bulk-complete/` | auth | `UserExerciseProgressViewSet` |
| `GET` | `/api/routine/user-exercise-progress/daily-summary/` | auth | `UserExerciseProgressViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/user-exercise-progress/{id}/` | auth | `UserExerciseProgressViewSet` |
| `GET` | `/api/routine/v1/analytics/recent-progress/` | auth | `RecentActivityProgressView` |
| `GET, POST` | `/api/routine/workout-sessions/` | auth | `WorkoutSessionViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/workout-sessions/{id}/` | auth | `WorkoutSessionViewSet` |

### Social

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

`comments_count` on a post is the number of comments THIS viewer can open: the stored
counter less the ones hidden by moderation and the ones by accounts they have blocked.
It therefore equals the row count of `GET comments/?post={id}` for the same viewer.

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
| `GET` | `/api/social/users/public-profile/{id}/` | auth | `PublicUserProfileViewSet` |

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
| `GET` | `/api/subscription/v1/plans/` | public | `SubscriptionPlanViewSet` |
| `GET` | `/api/subscription/v1/plans/available/` | public | `SubscriptionPlanViewSet` |
| `GET` | `/api/subscription/v1/plans/{id}/` | public | `SubscriptionPlanViewSet` |
| `GET, POST` | `/api/subscription/v1/subscriptions/` | auth | `SubscriptionViewSet` |
| `GET` | `/api/subscription/v1/subscriptions/current/` | auth | `SubscriptionViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/subscription/v1/subscriptions/{id}/` | auth | `SubscriptionViewSet` |
| `POST` | `/api/subscription/v1/subscriptions/{id}/cancel/` | auth | `SubscriptionViewSet` |
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
| `GET` | `/api/wallet/agent/apikey/status/` | auth, IsAgent | `AgentApiKeyStatusView` |
| `POST` | `/api/wallet/agent/topup/` | auth, IsAgent | `AgentTopUpView` |
| `POST` | `/api/wallet/agent/topup/proxy` | auth, IsAgent | `AgentTopUpProxyView` |
| `GET` | `/api/wallet/agents/` | auth | `AgentDirectoryView` |
| `GET` | `/api/wallet/balance/` | auth | `WalletBalanceView` |
| `POST` | `/api/wallet/client/transfer/` | auth | `ClientTransferToTrainerView` |
| `GET` | `/api/wallet/currency/` | auth | `WalletCurrencyView` |
| `PUT` | `/api/wallet/currency/` | auth | `WalletCurrencyView` |
| `GET` | `/api/wallet/transactions/` | auth | `WalletTransactionsView` |
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
