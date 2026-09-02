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
     {username, email, password1, password2, phone_number, user_type}
  -> 201 {user}                       account created INACTIVE, OTP emailed

POST /api/auth/verify-otp/   {email, otp_code}
  -> 200 {message, user, access, refresh}     account activated

POST /api/auth/resend-otp/   {email}
  -> 200 (always — see anti-enumeration)
```

Note `password1`/`password2` — not `password`. `user_type` is one of
`client`, `trainer`, `agent`.

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
POST /api/auth/forgot-password/         {email}                       -> 200 (always)
POST /api/auth/forgot-password/verify/  {email, otp_code}             -> 200 {reset_token}
POST /api/auth/forgot-password/confirm/ {reset_token, new_password}   -> 200
```

The `reset_token` is a single-use opaque string, **valid 15 minutes**, and is never stored in
readable form server-side — only a keyed hash of it is. It cannot be recovered from a
database dump.

**On successful reset every outstanding refresh token for that user is blacklisted.** The
app must treat a post-reset `401 token_not_valid` as "log in again", not as an error.

### Anti-enumeration

`forgot-password` and `resend-otp` always return `200` with the same message whether or
not the email exists. Do not use them to check account existence — the response is
identical by design.

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

`title`/`body` arrive in the FCM `notification` block, already rendered in the
recipient's language.

Registered but **not currently emitted** — do not build UI for these yet:
`session_reminder`, `progress_milestone`, `custom`.

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
| OTP verification | 5 attempts per email, then locked 1 hour |

A `429` carries `Retry-After` (seconds) and `code: "rate_limited"`.

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
| `DELETE, GET, PATCH, PUT` | `/api/analytics/activities/{id}/` | auth | `UserActivityViewSet` |
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
| `POST` | `/api/auth/client/request-trainer/` | auth | `ClientRequestTrainerView` |
| `POST` | `/api/auth/client/unassign-trainer/` | auth | `ClientUnassignTrainerView` |
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
| `GET` | `/api/auth/trainers/stats/` | public | `PublicTrainerClientStatsView` |
| `GET` | `/api/auth/user/details/` | auth | `UserDetailsView` |
| `DELETE, POST` | `/api/auth/user/profile-picture/` | auth | `ProfilePictureUploadView` |
| `GET, POST` | `/api/auth/user/update/` | auth | `UpdateUserDetailsView` |
| `POST` | `/api/auth/verify-otp/` | public | `OTPVerificationView` |

### Diet

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/diet/v1/advice/latest/` | auth, HasDietAccess | `DailyAdviceView` |
| `POST` | `/api/diet/v1/client/meals/interact/` | auth, HasDietAccess | `ClientMealInteractionView` |
| `GET` | `/api/diet/v1/client/meals/{meal_id}/` | auth, HasDietAccess | `ClientMealDetailsView` |
| `POST` | `/api/diet/v1/client/meals/{meal_id}/complete/` | auth, HasDietAccess | `MealCompletionView` |
| `GET` | `/api/diet/v1/client/progress/` | auth, HasDietAccess | `ClientProgressView` |
| `GET` | `/api/diet/v1/client/progress/enhanced/` | auth, HasDietAccess | `EnhancedClientProgressView` |
| `GET` | `/api/diet/v1/client/progress/weekly/` | auth, HasDietAccess | `ClientWeeklyProgressView` |
| `GET` | `/api/diet/v1/food/categories/` | auth, HasDietAccess | `FoodCategoryListView` |
| `POST` | `/api/diet/v1/food/import/` | auth | `FoodImportView` |
| `GET` | `/api/diet/v1/food/list/` | auth, HasDietAccess | `FoodListView` |
| `GET` | `/api/diet/v1/food/search/` | auth | `FoodSearchView` |
| `GET` | `/api/diet/v1/meals/{meal_id}/components/` | auth, HasDietAccess | `MealComponentsView` |
| `GET` | `/api/diet/v1/my/diet-plans/` | auth, HasDietAccess | `MyDietPlansView` |
| `GET` | `/api/diet/v1/nutrition/plan/{plan_id}/` | auth, HasDietAccess | `DietPlanNutritionView` |
| `GET` | `/api/diet/v1/plan/{plan_id}/meals-with-ingredients/` | auth, HasDietAccess | `DietPlanMealsWithIngredientsView` |
| `POST` | `/api/diet/v1/plans/generate-rule/` | auth, HasDietAccess, MealUsageLimit | `GenerateDietPlanRuleBasedView` |
| `POST` | `/api/diet/v1/plans/generate-sync/` | auth, HasDietAccess, MealUsageLimit | `GenerateDietPlanSyncView` |
| `POST` | `/api/diet/v1/plans/generate/` | auth, HasDietAccess, MealUsageLimit | `GenerateDietPlanView` |
| `DELETE, GET, POST` | `/api/diet/v1/preferences/` | auth | `UserPreferencesView` |
| `GET, POST` | `/api/diet/v1/preferences/food-category/` | auth, HasDietAccess | `UserFoodCategoryPreferenceView` |
| `DELETE, PUT` | `/api/diet/v1/preferences/food-category/{food_id}/` | auth, HasDietAccess | `UserFoodCategoryPreferenceDetailView` |
| `GET, POST` | `/api/diet/v1/trainer/diet-plans/` | auth, HasDietAccess | `TrainerDietPlanView` |
| `DELETE, POST, PUT` | `/api/diet/v1/trainer/meals/` | auth, HasDietAccess | `TrainerMealView` |
| `DELETE, POST, PUT` | `/api/diet/v1/trainer/meals/{meal_id}/` | auth, HasDietAccess | `TrainerMealView` |
| `GET` | `/api/diet/v1/trainer/templates/` | auth, HasDietAccess | `TrainerTemplatesView` |

### Notifications

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/notifications/` | auth | `APIRootView` |
| `GET, POST` | `/api/notifications/preferences/` | auth | `NotificationPreferenceViewSet` |
| `GET` | `/api/notifications/preferences/event_types/` | auth | `NotificationPreferenceViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/notifications/preferences/{id}/` | auth | `NotificationPreferenceViewSet` |

### Privacy (GDPR)

| Method | Path | Auth | View |
|---|---|---|---|
| `DELETE, GET` | `/api/privacy/erase/` | auth | `PersonalDataEraseView` |
| `GET` | `/api/privacy/export/` | auth | `PersonalDataExportView` |

### Routines & Workouts

| Method | Path | Auth | View |
|---|---|---|---|
| `GET` | `/api/routine/` | auth | `APIRootView` |
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
| `GET, POST` | `/api/routine/exercisesetlogs/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `POST` | `/api/routine/exercisesetlogs/bulk-create/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `GET` | `/api/routine/exercisesetlogs/my-progress/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/exercisesetlogs/{id}/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `GET, POST` | `/api/routine/routine-exercises/` | IsAdminOrOwnerOrReadOnly | `RoutineExerciseViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/routine-exercises/{id}/` | IsAdminOrOwnerOrReadOnly | `RoutineExerciseViewSet` |
| `GET, POST` | `/api/routine/routine-progress/` | (IsTrainerOrAdmin&IsClientOrAssignedTrainer) | `RoutineProgressViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/routine-progress/{id}/` | (IsTrainerOrAdmin&IsClientOrAssignedTrainer) | `RoutineProgressViewSet` |
| `GET, POST` | `/api/routine/routines/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `GET` | `/api/routine/routines/my_clients_progress/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/routines/{id}/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/assign_to_client/` | IsTrainerOrAdminForAssignment | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/unassign_from_client/` | IsTrainerOrAdminForAssignment | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{id}/update_progress/` | auth | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{pk}/assign_to_client/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `POST` | `/api/routine/routines/{pk}/unassign_from_client/` | IsTrainerOrAdmin | `RoutineViewSet` |
| `GET, POST` | `/api/routine/set-logs/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `POST` | `/api/routine/set-logs/bulk-create/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `GET` | `/api/routine/set-logs/my-progress/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/set-logs/{id}/` | IsSetLogCreatorOrTrainerOrAdmin | `ExerciseSetLogViewSet` |
| `GET` | `/api/routine/template-exercises/` | auth | `RoutineTemplateExerciseViewSet` |
| `GET` | `/api/routine/template-exercises/{id}/` | auth | `RoutineTemplateExerciseViewSet` |
| `GET, POST` | `/api/routine/templates/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `GET` | `/api/routine/templates/my_templates/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `GET` | `/api/routine/templates/public_templates/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `DELETE, GET, PATCH, PUT` | `/api/routine/templates/{id}/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `POST` | `/api/routine/templates/{id}/copy/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
| `POST` | `/api/routine/templates/{id}/generate/` | IsTrainerOrReadOnly | `RoutineTemplateViewSet` |
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
| `GET` | `/api/wallet/balance/` | auth | `WalletBalanceView` |
| `POST` | `/api/wallet/client/transfer/` | auth | `ClientTransferToTrainerView` |
| `GET` | `/api/wallet/transactions/` | auth | `WalletTransactionsView` |

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
