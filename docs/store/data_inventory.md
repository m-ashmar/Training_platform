# Data inventory

> **Generated** by `python manage.py data_inventory` from the privacy registry (`training_platform/privacy/sources.py`; the words in `training_platform/privacy/disclosure.py`). Do not edit it by hand: change the registry, run the command, commit both. `tests/test_store_docs.py` fails while this file and the registry disagree.

What the platform's server holds about a person, why, who else receives it, what the person can download and what happens when they delete their account (spec §9 B3). `privacy_labels.md` (Apple's App Privacy answers and Google's Data safety answers) and the privacy-policy drafts in `docs/legal/` are written from it, and must list the same data.

90 sources, in the order they are registered.

## What every source has in common

- **Download.** Every source is a section of the person's export, `GET /api/privacy/export/` (a JSON file), with the fields listed as exported below.
- **Deletion.** In the app: `DELETE /api/privacy/erase/?confirm=ERASE` (`GET` previews it). Without the app: a request on the web page `/legal/delete-account/`, which an operator confirms with the owner of the address and carries out through the same erasure. Each source says what erasure does to it: **deleted**; **anonymised** (the fields named are cleared or replaced, the others stay on the row, which is listed field by field); or **kept** (a record of money, or content other people's programs are built on). The account row itself is anonymised and deactivated.
- **Retention.** A source with a window is purged every night (`training_platform.privacy.purge_expired_personal_data`); the others are kept while the account exists.
- **Operators.** The platform's own operators see records in the console as their work needs. They are the platform, not a recipient.
- **In transit.** Encrypted: production redirects HTTP to HTTPS and sends HSTS (`settings_production.py`, `fly.toml force_https`).

## Service providers that hold every source

| Provider | What it does |
|---|---|
| Fly.io | Runs the servers (`fly.toml`, region Amsterdam). Every request and every stored row passes through it. |
| The PostgreSQL and Redis providers | Hold the database and the caches. Chosen by the owner at deployment. |
| Sentry (optional) | Server error reports, only when `SENTRY_DSN` is set, and with `send_default_pii=False` (no email, no request body, no IP). |
| AWS Secrets Manager (optional) | Holds the server's own secrets when `AWS_SECRET_NAME` is set. No personal data. |

## Recipients

Who, besides those providers and the platform's operators, receives data, and which sources reach them.

| Key | Who | Kind | What they receive | Sources |
|---|---|---|---|---|
| `coach` | The client's trainer | person | While a coaching is approved: the profile a coach works from (name, email, photo, age, gender, height, weight, goals, activity level, training answers, health notes and injury areas), the client's training log, diet plan, nutrition record and food preferences, and their chat. A trainer with only a pending request sees a short row and never health notes (`users/trainer_requests.py`). A client's training and nutrition are the trainer's to read only through the approved relation (`CustomUser.can_access_user_data`). | `profile`, `workout_sessions`, `exercise_progress`, `routine_progress`, `day_outcomes`, `diet_plans`, `food_preferences`, `conversations`, `messages_sent`, `daily_progress`, `meals`, `meal_components`, `trainer_relations`, `coaching_periods`, `exercise_set_logs` |
| `clients` | The trainer's clients | person | What a trainer writes for the clients they coach: routines, diet plans, chat messages, challenges they set, and the trainer's profile and prices on a build that may show them. | `profile`, `media_kit`, `conversations`, `messages_sent`, `exercises_created`, `routines_created`, `trainer_relations`, `coaching_periods`, `exercise_media`, `routine_exercises` |
| `community` | Other signed-in users | person | What a person publishes: username, name and photo, posts, comments, likes, follows, challenges, leaderboard places and badges. Hidden from anyone they blocked or who blocked them (`moderation.services.blocked_ids`); a suspended account leaves every public surface. | `profile`, `media_kit`, `posts`, `comments`, `achievements_earned`, `social_achievements`, `challenges_created`, `challenge_participation`, `post_likes`, `comment_likes`, `follows`, `agent_profile`, `routine_products` |
| `public` | Anyone, without signing in | person | The public trainer directory (`GET /api/v1/trainers/`, `trainers/public/`): a trainer's card — name, photo, bio, specialisations, certifications, experience and the proof numbers, the price only on a build that may show it. | `profile`, `media_kit` |
| `agent` | The agent who tops a wallet up | person | The client's name and account number and the amount, on the top-up and its receipt (`wallet/agents.py person`). Agents are accounts the platform creates, never self-registered. | `transactions` |
| `ai_provider` | The AI service provider | service provider | Only after the person grants AI consent (`moderation.AIConsent`; every AI route refuses without it): the messages of the conversation, and what the coach reads to answer — the profile (name, age, gender, height, weight, BMI, goals, injury note, activity level, the trainer's name), workouts and set logs, the routine, the diet plan and meals, and the coach's own notes about the person (`ai_assistant/services/context_compiler.py`, `ai_assistant/tools/`). The app names no provider (the owner's decision, 2026-09-24); the one configured is OpenAI (`OPENAI_API_KEY`, `OPENAI_MODEL`). Nothing else is sent to an AI: diet plans are built by the platform's own rules. | `profile`, `workout_sessions`, `exercise_progress`, `routine_progress`, `day_outcomes`, `diet_plans`, `daily_progress`, `meals`, `meal_components`, `ai_insights`, `routines_created`, `assistant_messages`, `exercise_set_logs`, `routine_exercises` |
| `push_provider` | Google Firebase Cloud Messaging | service provider | The device's push token and the title and text of each push (`notifications/channels/fcm.py`), which can carry a person's name and the first words of a chat message. | `devices`, `messages_sent`, `notifications` |
| `email_provider` | The email delivery provider | service provider | The email address and the sign-up or password-reset code — the only emails the server sends (`users/utils.py`). The provider is the owner's choice (`EMAIL_HOST`). | `profile`, `notification_emails`, `otp_verifications`, `password_reset_tokens` |
| `file_storage` | Google Cloud Storage | service provider | The files people upload: profile photos, trainers' media, chat photos, voice notes and videos, post and challenge images, and trainers' verification documents (a separate private bucket). Production refuses to start without it (`MEDIA_BACKEND=gcs`, `settings_production.py`). | `profile`, `media_kit`, `verification_documents`, `posts`, `messages_sent`, `exercises_created`, `challenges_created`, `routine_products` |
| `payment_gateway` | ShamCash | service provider | For a payment: its reference, amount and currency (`subscription/gateways/shamcash.py`); the platform reads the transfers its own merchant account received. For a trainer's withdrawal: the trainer's ShamCash address and the amount, which an operator sends through ShamCash. | `payout_methods`, `withdrawals`, `payments` |

## Data types, as the stores name them

| Key | In plain words | Apple App Privacy | Google Play Data safety | Sources |
|---|---|---|---|---|
| `name` | Name | Contact Info > Name | Personal info > Name | `profile`, `payout_methods`, `agent_profile` |
| `email` | Email address | Contact Info > Email Address | Personal info > Email address | `profile`, `account_deletion_requests` |
| `phone` | Phone number | Contact Info > Phone Number | Personal info > Phone number | `profile`, `agent_profile` |
| `address` | Shop location of an agent | Contact Info > Physical Address | Personal info > Address | `agent_profile` |
| `user_ids` | Account identifiers | Identifiers > User ID | Personal info > User IDs | `profile` |
| `device_ids` | Device push token | Identifiers > Device ID | Device or other IDs > Device or other IDs | `devices` |
| `other_personal` | Date of birth, age and gender | Other Data > Other Data Types | Personal info > Other info | `profile` |
| `health` | Health information | Health & Fitness > Health | Health and fitness > Health info | `profile`, `diet_plans`, `food_preferences`, `performance_metrics`, `ai_training_data`, `goals`, `daily_advice` |
| `fitness` | Fitness and training activity | Health & Fitness > Fitness | Health and fitness > Fitness info | `profile`, `workout_sessions`, `exercise_progress`, `routine_progress`, `day_outcomes`, `diet_plans`, `food_preferences`, `performance_metrics`, `behaviour_events`, `goals`, `dashboard`, `daily_advice`, `daily_progress`, `food_category_preferences`, `learned_food_weights`, `meal_swaps`, `meal_events`, `meals`, `meal_components`, `ai_insights`, `routines_created`, `challenge_participation`, `exercise_set_logs`, `routine_exercises` |
| `purchases` | Purchase and payment history | Purchases > Purchase History | Financial info > Purchase history | `subscriptions`, `trainer_relations`, `coaching_periods`, `routine_purchases`, `transactions`, `idempotency_keys`, `payments`, `transfer_reviews`, `payment_events` |
| `financial` | Wallet and payout details | Financial Info > Other Financial Info | Financial info > Other financial info | `payout_methods`, `withdrawals`, `withdrawal_events`, `wallet`, `agent_profile`, `agent_receivable`, `agent_settlements`, `transactions`, `wallet_audit_log`, `transfer_reviews`, `payer_address` |
| `messages` | Chat messages | User Content > Emails or Text Messages | Messages > Other in-app messages | `conversations`, `messages_sent` |
| `ai_conversations` | Conversations with the AI coach | User Content > Other User Content | Messages > Other in-app messages | `chat_sessions`, `ai_training_data`, `ai_insights`, `assistant_messages` |
| `photos_videos` | Photos and videos | User Content > Photos or Videos | Photos and videos > Photos, Videos | `profile`, `media_kit`, `posts`, `messages_sent`, `exercises_created`, `challenges_created`, `routine_products` |
| `audio` | Voice notes | User Content > Audio Data | Audio > Voice or sound recordings | `messages_sent` |
| `user_content` | Posts, comments and other content you create | User Content > Other User Content | App activity > Other user-generated content | `profile`, `posts`, `comments`, `trainer_saved_meals`, `trainer_saved_meal_items`, `exercises_created`, `routines_created`, `templates_created`, `challenges_created`, `routine_products`, `exercise_media`, `routine_exercises`, `template_exercises` |
| `support` | Reports and requests you send us | User Content > Customer Support | App activity > Other user-generated content | `reports_made`, `account_deletion_requests` |
| `documents` | Identity and qualification documents | Other Data > Other Data Types | Files and docs > Files and docs | `verification_documents`, `verification_decisions`, `verification_application` |
| `app_activity` | App activity | Usage Data > Product Interaction | App activity > App interactions | `activity_log`, `sessions`, `behaviour_events`, `notifications`, `notification_emails`, `achievements_earned`, `achievement_progress`, `social_achievements`, `ai_usage_cost`, `notification_preferences`, `challenge_participation`, `post_likes`, `comment_likes`, `follows`, `trainer_relations`, `trainer_request_outcomes`, `coaching_endings`, `blocks`, `ai_consent`, `subscription_usage` |
| `connection` | IP address and device details | Diagnostics > Other Diagnostic Data | Device or other IDs > Device or other IDs | `activity_log`, `sessions`, `agent_profile`, `wallet_audit_log` |
| `credentials` | Sign-in codes | not declared: Security secrets (one-time codes, reset tokens, API keys), stored hashed or encrypted and used only to sign in; neither form has a category for them. | not declared | `otp_verifications`, `password_reset_tokens`, `agent_api_keys` |
| `staff_records` | Operator records | not declared: About the platform's own operators, who work in the web console, not the app. No app user gives this data. | not declared | `console_logins`, `exchange_rates`, `capability_changes`, `theme_changes`, `app_releases_published`, `legal_documents_published` |

## Summary

| Source | Model | Data types | Recipients | Erasure | Retention |
|---|---|---|---|---|---|
| `profile` | `users.CustomUser` | `name`, `email`, `phone`, `user_ids`, `other_personal`, `health`, `fitness`, `photos_videos`, `user_content` | `coach`, `clients`, `community`, `public`, `ai_provider`, `email_provider`, `file_storage` | anonymised | - |
| `devices` | `users.DeviceToken` | `device_ids` | `push_provider` | deleted | - |
| `media_kit` | `mediakit.MediaAsset` | `photos_videos` | `public`, `community`, `clients`, `file_storage` | deleted | - |
| `verification_documents` | `trainer_verification.VerificationDocument` | `documents` | `file_storage` | deleted | - |
| `verification_decisions` | `trainer_verification.VerificationDecision` | `documents` | nobody | deleted | - |
| `verification_application` | `trainer_verification.VerificationApplication` | `documents` | nobody | deleted | - |
| `payout_methods` | `payouts.PayoutMethod` | `financial`, `name` | `payment_gateway` | deleted | - |
| `withdrawals` | `payouts.Withdrawal` | `financial` | `payment_gateway` | kept | - |
| `withdrawal_events` | `payouts.WithdrawalEvent` | `financial` | nobody | kept | - |
| `console_logins` | `admin_dashboard.ConsoleLogin` | `staff_records` | nobody | deleted | 90 days |
| `workout_sessions` | `routine.WorkoutSession` | `fitness` | `coach`, `ai_provider` | deleted | - |
| `exercise_progress` | `routine.UserExerciseProgress` | `fitness` | `coach`, `ai_provider` | deleted | - |
| `routine_progress` | `routine.RoutineProgress` | `fitness` | `coach`, `ai_provider` | deleted | - |
| `day_outcomes` | `routine.DayOutcome` | `fitness` | `coach`, `ai_provider` | deleted | - |
| `diet_plans` | `diet.DietPlan` | `fitness`, `health` | `coach`, `ai_provider` | deleted | - |
| `food_preferences` | `diet.UserFoodPreference` | `health`, `fitness` | `coach` | deleted | - |
| `posts` | `social.Post` | `user_content`, `photos_videos` | `community`, `file_storage` | deleted | - |
| `comments` | `social.Comment` | `user_content` | `community` | deleted | - |
| `activity_log` | `analytics.UserActivity` | `app_activity`, `connection` | nobody | deleted | 180 days |
| `sessions` | `analytics.UserSession` | `app_activity`, `connection` | nobody | deleted | 180 days |
| `performance_metrics` | `analytics.PerformanceMetric` | `health`, `fitness` | nobody | deleted | - |
| `chat_sessions` | `ai_assistant.ChatSession` | `ai_conversations` | nobody | deleted | - |
| `behaviour_events` | `ai_assistant.UserBehaviorEvent` | `app_activity`, `fitness` | nobody | deleted | 365 days |
| `ai_training_data` | `ai_assistant.AITrainingData` | `ai_conversations`, `health` | nobody | deleted | 365 days |
| `conversations` | `chat.Conversation` | `messages` | `coach`, `clients` | deleted | - |
| `messages_sent` | `chat.Message` | `messages`, `photos_videos`, `audio` | `coach`, `clients`, `push_provider`, `file_storage` | deleted | - |
| `notifications` | `notifications.Notification` | `app_activity` | `push_provider` | deleted | 90 days |
| `notification_emails` | `notifications.EmailDelivery` | `app_activity` | `email_provider` | deleted | 90 days |
| `wallet` | `wallet.Wallet` | `financial` | nobody | kept | - |
| `subscriptions` | `subscription.Subscription` | `purchases` | nobody | kept | - |
| `achievements_earned` | `achievements.UserAchievement` | `app_activity` | `community` | deleted | - |
| `achievement_progress` | `achievements.AchievementProgress` | `app_activity` | nobody | deleted | - |
| `social_achievements` | `social.UserAchievement` | `app_activity` | `community` | deleted | - |
| `goals` | `analytics.UserGoal` | `fitness`, `health` | nobody | deleted | - |
| `dashboard` | `analytics.AnalyticsDashboard` | `fitness` | nobody | deleted | 180 days |
| `daily_advice` | `diet.DailyAdvice` | `fitness`, `health` | nobody | deleted | 365 days |
| `daily_progress` | `diet.DailyProgress` | `fitness` | `coach`, `ai_provider` | deleted | - |
| `food_category_preferences` | `diet.UserFoodCategoryPreference` | `fitness` | nobody | deleted | - |
| `learned_food_weights` | `diet.UserFoodWeight` | `fitness` | nobody | deleted | - |
| `meal_swaps` | `diet.MealSwap` | `fitness` | nobody | deleted | - |
| `meal_events` | `diet.MealEvent` | `fitness` | nobody | deleted | - |
| `meals` | `diet.Meal` | `fitness` | `coach`, `ai_provider` | deleted | - |
| `meal_components` | `diet.MealComponent` | `fitness` | `coach`, `ai_provider` | deleted | - |
| `trainer_saved_meals` | `diet.TrainerSavedMeal` | `user_content` | nobody | deleted | - |
| `trainer_saved_meal_items` | `diet.TrainerSavedMealItem` | `user_content` | nobody | deleted | - |
| `ai_usage_cost` | `ai_assistant.UsageCost` | `app_activity` | nobody | kept | - |
| `ai_insights` | `ai_assistant.UserInsight` | `fitness`, `ai_conversations` | `ai_provider` | deleted | - |
| `notification_preferences` | `notifications.UserNotificationPreference` | `app_activity` | nobody | deleted | - |
| `exercises_created` | `routine.Exercise` | `user_content`, `photos_videos` | `clients`, `file_storage` | kept | - |
| `routines_created` | `routine.Routine` | `user_content`, `fitness` | `clients`, `ai_provider` | kept | - |
| `templates_created` | `routine.RoutineTemplate` | `user_content` | nobody | kept | - |
| `challenges_created` | `social.Challenge` | `user_content`, `photos_videos` | `community`, `file_storage` | kept | - |
| `challenge_participation` | `social.ChallengeParticipation` | `fitness`, `app_activity` | `community` | deleted | - |
| `post_likes` | `social.PostLike` | `app_activity` | `community` | deleted | - |
| `comment_likes` | `social.CommentLike` | `app_activity` | `community` | deleted | - |
| `follows` | `social.UserFollow` | `app_activity` | `community` | deleted | - |
| `trainer_relations` | `users.TrainerClientRelation` | `app_activity`, `purchases` | `coach`, `clients` | deleted | - |
| `trainer_request_outcomes` | `users.TrainerRequestOutcome` | `app_activity` | nobody | deleted | 180 days |
| `coaching_endings` | `users.CoachingEnding` | `app_activity` | nobody | anonymised | - |
| `otp_verifications` | `users.OTPVerification` | `credentials` | `email_provider` | deleted | 1 days |
| `password_reset_tokens` | `users.PasswordResetToken` | `credentials` | `email_provider` | deleted | 1 days |
| `reports_made` | `moderation.Report` | `support` | nobody | anonymised | - |
| `blocks` | `moderation.Block` | `app_activity` | nobody | deleted | - |
| `ai_consent` | `moderation.AIConsent` | `app_activity` | nobody | deleted | - |
| `agent_profile` | `wallet.AgentProfile` | `name`, `address`, `phone`, `financial`, `connection` | `community` | kept | - |
| `agent_receivable` | `wallet.AgentReceivableEntry` | `financial` | nobody | kept | - |
| `agent_settlements` | `wallet.AgentSettlement` | `financial` | nobody | kept | - |
| `coaching_periods` | `users.CoachingPeriod` | `purchases` | `coach`, `clients` | kept | - |
| `routine_products` | `routine.RoutineProduct` | `user_content`, `photos_videos` | `community`, `file_storage` | kept | - |
| `routine_purchases` | `routine.RoutinePurchase` | `purchases` | nobody | kept | - |
| `transactions` | `wallet.Transaction` | `purchases`, `financial` | `agent` | kept | - |
| `exchange_rates` | `wallet.ExchangeRate` | `staff_records` | nobody | kept | - |
| `wallet_audit_log` | `wallet.WalletAuditLog` | `financial`, `connection` | nobody | kept | - |
| `idempotency_keys` | `wallet.IdempotencyKey` | `purchases` | nobody | kept | 30 days |
| `capability_changes` | `policy.CapabilityChangeLog` | `staff_records` | nobody | kept | - |
| `theme_changes` | `policy.ThemeChangeLog` | `staff_records` | nobody | kept | - |
| `app_releases_published` | `releases.AppRelease` | `staff_records` | nobody | kept | - |
| `legal_documents_published` | `legal.LegalDocument` | `staff_records` | nobody | kept | - |
| `account_deletion_requests` | `legal.AccountDeletionRequest` | `email`, `support` | nobody | anonymised | 180 days |
| `assistant_messages` | `ai_assistant.ChatMessage` | `ai_conversations` | `ai_provider` | deleted | - |
| `exercise_set_logs` | `routine.ExerciseSetLog` | `fitness` | `coach`, `ai_provider` | deleted | - |
| `exercise_media` | `routine.ExerciseMedia` | `user_content` | `clients` | kept | - |
| `routine_exercises` | `routine.RoutineExercise` | `user_content`, `fitness` | `clients`, `ai_provider` | kept | - |
| `template_exercises` | `routine.RoutineTemplateExercise` | `user_content` | nobody | kept | - |
| `payments` | `subscription.Payment` | `purchases` | `payment_gateway` | kept | - |
| `subscription_usage` | `subscription.SubscriptionUsage` | `app_activity` | nobody | kept | - |
| `transfer_reviews` | `subscription.TransferReview` | `purchases`, `financial` | nobody | kept | - |
| `payer_address` | `subscription.PayerAddress` | `financial` | nobody | deleted | - |
| `payment_events` | `subscription.PaymentEvent` | `purchases` | nobody | kept | - |
| `agent_api_keys` | `wallet.AgentAPIKey` | `credentials` | nobody | deleted | - |

## Sources

### `profile` — `users.CustomUser`

**Why.** The account: signing in, reaching the person (codes by email), and what the app plans training and nutrition from (age, body measurements, goals, injuries). A trainer's professional profile and price are what clients choose a trainer by.

**Whose.** The account's own row.

**Who else receives it.** The client's trainer (`coach`); The trainer's clients (`clients`); Other signed-in users (`community`); Anyone, without signing in (`public`); The AI service provider (`ai_provider`); The email delivery provider (`email_provider`); Google Cloud Storage (`file_storage`).

**Data types.** Name (`name`); Email address (`email`); Phone number (`phone`); Account identifiers (`user_ids`); Date of birth, age and gender (`other_personal`); Health information (`health`); Fitness and training activity (`fitness`); Photos and videos (`photos_videos`); Posts, comments and other content you create (`user_content`).

**Fields stored.** `id`, `password`, `is_superuser`, `username`, `email`, `date_joined`, `phone_number`, `first_name`, `last_name`, `ai_training_consent`, `ai_training_consent_set_at`, `profile_picture`, `user_type`, `is_active`, `email_verified`, `is_staff`, `preferred_language`, `preferred_timezone`, `workout_reminder_hour`, `height`, `weight`, `age`, `gender`, `specific_injury`, `activity_level`, `activity_level_answered_at`, `date_of_birth`, `training_goal`, `training_place`, `training_days_per_week`, `injury_areas`, `trainer_bio`, `trainer_specializations`, `trainer_certifications`, `trainer_experience_years`, `trainer_hourly_rate`, `trainer_is_verified`, `trainer_is_available`, `trainer_goal_focus`, `trainer_injury_focus`, `trainer_training_places`, `assigned_trainer`, `client_goals`, `client_preferences`, `created_at`, `updated_at`, `last_login`, `groups`, `user_permissions`.

**In the export.** `id`, `username`, `email`, `phone_number`, `first_name`, `last_name`, `age`, `date_of_birth`, `gender`, `height`, `weight`, `activity_level`, `specific_injury`, `injury_areas`, `client_goals`, `training_goal`, `training_place`, `training_days_per_week`, `date_joined`, `ai_training_consent`, `ai_training_consent_set_at`, `groups`, `user_permissions`. Not exported: `password`, `is_superuser`, `profile_picture`, `user_type`, `is_active`, `email_verified`, `is_staff`, `preferred_language`, `preferred_timezone`, `workout_reminder_hour`, `activity_level_answered_at`, `trainer_bio`, `trainer_specializations`, `trainer_certifications`, `trainer_experience_years`, `trainer_hourly_rate`, `trainer_is_verified`, `trainer_is_available`, `trainer_goal_focus`, `trainer_injury_focus`, `trainer_training_places`, `assigned_trainer`, `client_preferences`, `created_at`, `updated_at`, `last_login`.

**On erasure.** Anonymised: `is_active`, `email`, `username`, `phone_number`, `first_name`, `last_name`, `specific_injury`, `client_goals`, `date_of_birth`, `injury_areas`, `training_goal`, `training_place`, `training_days_per_week`, `assigned_trainer`, `profile_picture`, `ai_training_consent`, `ai_training_consent_set_at` are cleared or replaced; `password`, `is_superuser`, `date_joined`, `user_type`, `email_verified`, `is_staff`, `preferred_language`, `preferred_timezone`, `workout_reminder_hour`, `height`, `weight`, `age`, `gender`, `activity_level`, `activity_level_answered_at`, `trainer_bio`, `trainer_specializations`, `trainer_certifications`, `trainer_experience_years`, `trainer_hourly_rate`, `trainer_is_verified`, `trainer_is_available`, `trainer_goal_focus`, `trainer_injury_focus`, `trainer_training_places`, `client_preferences`, `created_at`, `updated_at`, `last_login`, `groups`, `user_permissions` stay on the row.

**Retention.** While the account exists.

### `devices` — `users.DeviceToken`

**Why.** Delivering push notifications to the person's phones.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Google Firebase Cloud Messaging (`push_provider`).

**Data types.** Device push token (`device_ids`).

**Fields stored.** `id`, `user`, `token`, `platform`, `app_version`, `device_id`, `is_active`, `last_used_at`, `created_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `media_kit` — `mediakit.MediaAsset`

**Why.** A trainer's photos and videos on their profile and card, and the exercise demonstrations they film.

**Whose.** Rows whose `person` is the account.

**Who else receives it.** Anyone, without signing in (`public`); Other signed-in users (`community`); The trainer's clients (`clients`); Google Cloud Storage (`file_storage`).

**Data types.** Photos and videos (`photos_videos`).

**Fields stored.** `id`, `owner_type`, `owner_id`, `slot`, `person`, `original`, `file_1x`, `file_2x`, `blur`, `focal_x`, `focal_y`, `brightness`, `width`, `height`, `alt_ar`, `alt_en`, `poster`, `hls_url`, `duration_seconds`, `processing_state`, `uploaded_by`, `created_at`, `updated_at`.

**In the export.** `owner_type`, `owner_id`, `slot`, `alt_ar`, `alt_en`, `created_at`. Not exported: `id`, `person`, `original`, `file_1x`, `file_2x`, `blur`, `focal_x`, `focal_y`, `brightness`, `width`, `height`, `poster`, `hls_url`, `duration_seconds`, `processing_state`, `uploaded_by`, `updated_at`.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `verification_documents` — `trainer_verification.VerificationDocument`

**Why.** Proving a trainer's identity and qualifications before an operator marks them verified, which withdrawals require.

**Whose.** Rows whose `application__trainer` is the account.

**Who else receives it.** Google Cloud Storage (`file_storage`).

**Data types.** Identity and qualification documents (`documents`).

**Fields stored.** `id`, `application`, `slot`, `file`, `thumbnail`, `kind`, `content_type`, `size`, `sha256`, `width`, `height`, `state`, `rejection_reason`, `reviewed_in_round`, `uploaded_by`, `uploaded_at`, `replaced_at`, `purge_after`.

**In the export.** `id`, `slot`, `kind`, `content_type`, `size`, `state`, `rejection_reason`, `uploaded_at`, `replaced_at`, `purge_after`. Not exported: `application`, `file`, `thumbnail`, `sha256`, `width`, `height`, `reviewed_in_round`, `uploaded_by`.

**On erasure.** Deleted.

**Also on erasure.** the rows are deleted by `_delete_verification_documents`, files first.

**Retention.** While the account exists.

### `verification_decisions` — `trainer_verification.VerificationDecision`

**Why.** What operators decided about a trainer's documents, and why: the record the verified badge rests on.

**Whose.** Rows whose `application__trainer` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Identity and qualification documents (`documents`).

**Fields stored.** `id`, `application`, `round`, `outcome`, `reason`, `documents`, `decided_by`, `decided_at`.

**In the export.** `id`, `round`, `outcome`, `reason`, `documents`, `decided_at`. Not exported: `application`, `decided_by`.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `verification_application` — `trainer_verification.VerificationApplication`

**Why.** A trainer's request to be verified, and its state.

**Whose.** Rows whose `trainer` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Identity and qualification documents (`documents`).

**Fields stored.** `id`, `trainer`, `state`, `round`, `submitted_at`, `decided_at`, `decided_by`, `rejection_reason`, `created_at`, `updated_at`.

**In the export.** `id`, `state`, `round`, `submitted_at`, `decided_at`, `rejection_reason`, `created_at`. Not exported: `trainer`, `decided_by`, `updated_at`.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `payout_methods` — `payouts.PayoutMethod`

**Why.** Where a trainer's withdrawals are paid: a ShamCash address and the holder's name (encrypted), or cash at a named agent.

**Whose.** Rows whose `trainer` is the account.

**Who else receives it.** ShamCash (`payment_gateway`).

**Data types.** Wallet and payout details (`financial`); Name (`name`).

**Fields stored.** `id`, `trainer`, `kind`, `shamcash_address`, `address_digest`, `holder_name`, `agent`, `is_default`, `verified_at`, `created_at`, `updated_at`.

**In the export.** `id`, `kind`, `shamcash_address`, `holder_name`, `agent`, `is_default`, `verified_at`, `created_at`. Not exported: `trainer`, `address_digest`, `updated_at`.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `withdrawals` — `payouts.Withdrawal`

**Why.** Each withdrawal a trainer asked for and how it was paid. A record of money.

**Whose.** Rows whose `trainer` is the account.

**Who else receives it.** ShamCash (`payment_gateway`).

**Data types.** Wallet and payout details (`financial`).

**Fields stored.** `id`, `number`, `trainer`, `amount`, `currency`, `state`, `method`, `destination_kind`, `destination_address`, `destination_holder`, `destination_agent`, `shamcash_transaction_id`, `hold_reference`, `return_reference`, `payout_reference`, `failure_reason`, `requested_at`, `approved_at`, `sent_at`, `confirmed_at`, `failed_at`, `cancelled_at`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `withdrawal_events` — `payouts.WithdrawalEvent`

**Why.** Every step of a withdrawal (requested, approved, sent, confirmed, failed), unchangeable once written.

**Whose.** Rows whose `withdrawal__trainer` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Wallet and payout details (`financial`).

**Fields stored.** `id`, `withdrawal`, `from_state`, `to_state`, `actor`, `actor_role`, `evidence`, `created_at`.

**In the export.** `id`, `withdrawal`, `from_state`, `to_state`, `actor_role`, `created_at`. Not exported: `actor`, `evidence`.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `console_logins` — `admin_dashboard.ConsoleLogin`

**Why.** An operator's sign-ins to the console from their phone: when, and the address and browser of the computer. The operator's own audit trail.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Operator records (`staff_records`).

**Fields stored.** `id`, `user`, `token_digest`, `state`, `expires_at`, `browser_digest`, `match_code`, `wrong_codes`, `claimed_ip`, `claimed_user_agent`, `cancel_reason`, `created_at`, `claimed_at`, `approved_at`, `used_at`, `cancelled_at`.

**In the export.** `id`, `state`, `created_at`, `expires_at`, `claimed_at`, `claimed_ip`, `claimed_user_agent`, `approved_at`, `used_at`, `cancelled_at`, `cancel_reason`. Not exported: `user`, `token_digest`, `browser_digest`, `match_code`, `wrong_codes`.

**On erasure.** Deleted.

**Retention.** Purged nightly: 90 days after `created_at`.

### `workout_sessions` — `routine.WorkoutSession`

**Why.** The workouts the person does: when, which routine day, how it felt.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The client's trainer (`coach`); The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `routine`, `day`, `start_time`, `end_time`, `status`, `client_uuid`, `effort_rating`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `exercise_progress` — `routine.UserExerciseProgress`

**Why.** Progress on each exercise per day: sets, reps, weight lifted, notes.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The client's trainer (`coach`); The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `exercise`, `date`, `completed_sets`, `target_sets`, `skipped`, `total_weight`, `total_repetitions`, `duration`, `notes`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `routine_progress` — `routine.RoutineProgress`

**Why.** Progress through each day of the person's routine.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The client's trainer (`coach`); The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `routine`, `day`, `status`, `completion_time`, `exercises_completed`, `total_exercises`, `notes`, `date`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `day_outcomes` — `routine.DayOutcome`

**Why.** What each training day planned against what was done: adherence and streaks.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The client's trainer (`coach`); The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `routine`, `day`, `date`, `via`, `confirmed_unlogged`, `quick_log`, `complete`, `planned_sets`, `logged_sets`, `missed_sets`, `planned_reps`, `logged_reps`, `reps_known`, `planned_volume_kg`, `logged_volume_kg`, `exercises_planned`, `exercises_full`, `exercises_partial`, `exercises_untouched`, `exercises_skipped`, `prescription_exact`, `workout_session`, `detail`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `diet_plans` — `diet.DietPlan`

**Why.** The person's diet plans: calorie and macro targets, dates, the planned meals and the allergen check of each plan.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The client's trainer (`coach`); The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`); Health information (`health`).

**Fields stored.** `id`, `user`, `goal`, `daily_calories`, `target_protein`, `target_carbs`, `target_fat`, `start_date`, `end_date`, `duration_weeks`, `generated_plan`, `allergen_report`, `generation_strategy`, `template`, `created_by`, `is_active`, `is_draft`, `superseded_on`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `food_preferences` — `diet.UserFoodPreference`

**Why.** The foods the person likes, dislikes and chooses, and the allergies they declared, so plans leave out what they must not eat.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The client's trainer (`coach`).

**Data types.** Health information (`health`); Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `allergies`, `local_ratio`, `liked_foods`, `disliked_foods`, `protein_choices`, `carb_choices`, `fat_choices`, `vegetable_choices`, `fruit_choices`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `posts` — `social.Post`

**Why.** Posts the person publishes to the community feed.

**Whose.** Rows whose `author` is the account.

**Who else receives it.** Other signed-in users (`community`); Google Cloud Storage (`file_storage`).

**Data types.** Posts, comments and other content you create (`user_content`); Photos and videos (`photos_videos`).

**Fields stored.** `id`, `author`, `post_type`, `title`, `content`, `visibility`, `image`, `content_type`, `object_id`, `likes_count`, `comments_count`, `shares_count`, `views_count`, `created_at`, `updated_at`, `is_flagged`, `is_hidden`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `comments` — `social.Comment`

**Why.** Comments the person writes on posts.

**Whose.** Rows whose `author` is the account.

**Who else receives it.** Other signed-in users (`community`).

**Data types.** Posts, comments and other content you create (`user_content`).

**Fields stored.** `id`, `author`, `post`, `content`, `parent_comment`, `likes_count`, `created_at`, `updated_at`, `is_flagged`, `is_hidden`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `activity_log` — `analytics.UserActivity`

**Why.** A log of what the person did in the app (a routine completed, a meal ticked, a plan generated), which achievements count. A row the app posts carries its IP address and device string.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`); IP address and device details (`connection`).

**Fields stored.** `id`, `user`, `activity_type`, `timestamp`, `content_type`, `object_id`, `metadata`, `session_id`, `ip_address`, `user_agent`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** Purged nightly: 180 days after `timestamp`.

### `sessions` — `analytics.UserSession`

**Why.** App sessions the app reports (start, end, counts), with the IP address and device string.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`); IP address and device details (`connection`).

**Fields stored.** `id`, `user`, `session_id`, `started_at`, `ended_at`, `ip_address`, `user_agent`, `device_type`, `browser`, `os`, `page_views`, `api_calls`, `duration_seconds`, `country`, `region`, `city`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** Purged nightly: 180 days after `started_at`.

### `performance_metrics` — `analytics.PerformanceMetric`

**Why.** Measurements over time (body weight, workout duration) for progress charts and badges.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Health information (`health`); Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `metric_type`, `value`, `unit`, `recorded_at`, `related_activity`, `notes`, `metadata`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `chat_sessions` — `ai_assistant.ChatSession`

**Why.** The person's conversations with the AI coach: titles, message counts and cost.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Conversations with the AI coach (`ai_conversations`).

**Fields stored.** `id`, `user`, `session_id`, `title`, `total_tokens_used`, `total_messages`, `estimated_cost_usd`, `is_active`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `behaviour_events` — `ai_assistant.UserBehaviorEvent`

**Why.** What the person did (workouts completed or abandoned, sets, meals, plans, AI chats), from which the AI coach's notes about their habits are computed.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`); Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `event_type`, `event_data`, `created_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** Purged nightly: 365 days after `created_at`.

### `ai_training_data` — `ai_assistant.AITrainingData`

**Why.** Anonymous copies of AI coach conversations, kept only while the person has "help us improve" on, to improve the coach. No name or account, bands instead of measurements; deleted when the setting goes off, with the account, or when the retention ends.

**Whose.** Rows whose `subject_key` is `_training_subject_key(account)`, derived on the server.

**Who else receives it.** Nobody outside the platform.

**Data types.** Conversations with the AI coach (`ai_conversations`); Health information (`health`).

**Fields stored.** `id`, `subject_key`, `reply_key`, `consented`, `retain_until`, `user_context_snapshot`, `user_message`, `tools_called`, `tool_results`, `ai_response`, `response_tokens`, `response_latency_ms`, `user_feedback`, `created_at`.

**In the export.** `created_at`, `retain_until`, `user_context_snapshot`, `user_message`, `tools_called`, `tool_results`, `ai_response`, `user_feedback`. Not exported: `id`, `subject_key`, `reply_key`, `consented`, `response_tokens`, `response_latency_ms`.

**On erasure.** Deleted.

**Retention.** Purged nightly: deleted when `retain_until` passes (set at most 365 days after the row is written).

### `conversations` — `chat.Conversation`

**Why.** The one chat thread between a trainer and each client they coach.

**Whose.** Rows whose `trainer` or `client` is the account.

**Who else receives it.** The client's trainer (`coach`); The trainer's clients (`clients`).

**Data types.** Chat messages (`messages`).

**Fields stored.** `id`, `trainer`, `client`, `created_at`, `last_message_at`, `trainer_last_read_at`, `client_last_read_at`, `trainer_notified_at`, `client_notified_at`.

**In the export.** `id`, `created_at`, `last_message_at`. Not exported: `trainer`, `client`, `trainer_last_read_at`, `client_last_read_at`, `trainer_notified_at`, `client_notified_at`.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `messages_sent` — `chat.Message`

**Why.** What the person sent in the trainer-client chat: text, photos, voice notes and videos, a form-check video naming an exercise, a coach's card pointing at the client's program.

**Whose.** Rows whose `sender` is the account.

**Who else receives it.** The client's trainer (`coach`); The trainer's clients (`clients`); Google Firebase Cloud Messaging (`push_provider`); Google Cloud Storage (`file_storage`).

**Data types.** Chat messages (`messages`); Photos and videos (`photos_videos`); Voice notes (`audio`).

**Fields stored.** `id`, `conversation`, `sender`, `body`, `kind`, `attachment`, `duration_seconds`, `width`, `height`, `size_bytes`, `exercise`, `poster`, `routine`, `program_day`, `program_changes`, `created_at`, `client_uuid`, `read_at`, `deleted_at`.

**In the export.** `id`, `conversation`, `kind`, `body`, `exercise`, `duration_seconds`, `routine`, `program_day`, `created_at`, `read_at`, `deleted_at`. Not exported: `sender`, `attachment`, `width`, `height`, `size_bytes`, `poster`, `program_changes`, `client_uuid`.

**On erasure.** Deleted.

**Also on erasure.** before any row goes, `_chat_files_sent` runs (`sources.py`).

**Retention.** While the account exists.

### `notifications` — `notifications.Notification`

**Why.** The person's in-app inbox of notices (payments, coaching, chat, reminders); each push goes out through Firebase.

**Whose.** Rows whose `recipient` is the account.

**Who else receives it.** Google Firebase Cloud Messaging (`push_provider`).

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `recipient`, `actor`, `event_type`, `event_id`, `related_object_id`, `metadata`, `is_read`, `status`, `deduplication_key`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Also on erasure.** `_chat_notices_sent` also deletes rows about the person that other people hold, which are not exported.

**Retention.** Purged nightly: 90 days after `created_at`.

### `notification_emails` — `notifications.EmailDelivery`

**Why.** Which notification emails (payment receipt, plan reminders, withdrawals) the platform owed the person and whether each was sent; no address or text is stored here.

**Whose.** Rows whose `recipient` is the account.

**Who else receives it.** The email delivery provider (`email_provider`).

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `recipient`, `notification`, `kind`, `dedup_key`, `context`, `status`, `attempts`, `language`, `error`, `created_at`, `last_attempt_at`, `sent_at`.

**In the export.** `id`, `kind`, `context`, `status`, `language`, `created_at`, `sent_at`. Not exported: `recipient`, `notification`, `dedup_key`, `attempts`, `error`, `last_attempt_at`.

**On erasure.** Deleted.

**Retention.** Purged nightly: 90 days after `created_at`.

### `wallet` — `wallet.Wallet`

**Why.** The person's wallet: balance and currency. A record of money.

**Whose.** Rows whose `owner` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Wallet and payout details (`financial`).

**Fields stored.** `id`, `owner`, `owner_type`, `balance`, `currency`, `currency_chosen_at`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `subscriptions` — `subscription.Subscription`

**Why.** The person's plan: which one, from when to when, and where it came from (paid, granted by an operator, trial). A record of money.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Purchase and payment history (`purchases`).

**Fields stored.** `id`, `user`, `plan`, `status`, `start_date`, `end_date`, `trial_end_date`, `auto_renew`, `cancelled_at`, `source`, `has_diet_access`, `has_routine_access`, `has_challenges_access`, `has_ai_advice`, `has_priority_support`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `achievements_earned` — `achievements.UserAchievement`

**Why.** Badges the person earned.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Other signed-in users (`community`).

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `user`, `achievement`, `earned_at`, `progress_data`, `is_featured`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `achievement_progress` — `achievements.AchievementProgress`

**Why.** Progress toward each badge.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `user`, `achievement`, `current_value`, `target_value`, `progress_percentage`, `last_updated`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `social_achievements` — `social.UserAchievement`

**Why.** Community badges the person earned.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Other signed-in users (`community`).

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `user`, `achievement`, `earned_at`, `progress_data`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `goals` — `analytics.UserGoal`

**Why.** The person's goals (workouts a week, a target weight) and progress toward them.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Fitness and training activity (`fitness`); Health information (`health`).

**Fields stored.** `id`, `user`, `goal_type`, `title`, `description`, `target_value`, `current_value`, `start_value`, `unit`, `created_at`, `target_date`, `completed_at`, `status`, `progress_percentage`, `metadata`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `dashboard` — `analytics.AnalyticsDashboard`

**Why.** Summaries of the person's progress computed for the dashboard screens.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `dashboard_type`, `user`, `data`, `computed_at`, `period_start`, `period_end`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** Purged nightly: 180 days after `computed_at`.

### `daily_advice` — `diet.DailyAdvice`

**Why.** The day's nutrition tip, written by the platform's own rules from the person's goal, calorie target and BMI (not by an AI).

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Fitness and training activity (`fitness`); Health information (`health`).

**Fields stored.** `id`, `user`, `text`, `generated_at`, `for_date`, `context_data`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** Purged nightly: 365 days after `generated_at`.

### `daily_progress` — `diet.DailyProgress`

**Why.** Each day's nutrition against the plan's targets: meals ticked, calories and macros.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The client's trainer (`coach`); The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `diet_plan`, `date`, `meals_completed`, `total_meals`, `calories_consumed`, `protein_consumed`, `carbs_consumed`, `fat_consumed`, `target_calories`, `target_protein`, `target_carbs`, `target_fat`, `is_day_completed`, `completed_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `food_category_preferences` — `diet.UserFoodCategoryPreference`

**Why.** Foods the person picked for each meal or macro.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `food`, `meal`, `macro`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `learned_food_weights` — `diet.UserFoodWeight`

**Why.** What the planner learned the person prefers, from their swaps and ticks, so plans fit them better.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `food`, `weight`, `observations`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `meal_swaps` — `diet.MealSwap`

**Why.** Foods the person swapped out of a meal, and what they chose instead.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `meal`, `rejected_food`, `chosen_food`, `at`, `similarity`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `meal_events` — `diet.MealEvent`

**Why.** When the person ticked, skipped or rated a meal.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user`, `meal`, `kind`, `at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `meals` — `diet.Meal`

**Why.** The meals of each diet plan: names, times, what was eaten, likes and notes.

**Whose.** Rows whose `diet_plan__user` is the account.

**Who else receives it.** The client's trainer (`coach`); The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `template`, `date`, `diet_plan`, `image_url`, `description`, `translations`, `is_ai_generated`, `meal_type`, `name`, `recipe`, `shape`, `reason`, `name_ar`, `reason_ar`, `target`, `scheduled_time`, `estimated_duration`, `is_liked`, `notes`, `is_completed`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `meal_components` — `diet.MealComponent`

**Why.** The foods and grams of each meal, and what was actually eaten.

**Whose.** Rows whose `meal__diet_plan__user` is the account.

**Who else receives it.** The client's trainer (`coach`); The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `meal`, `food`, `quantity`, `meal_time`, `is_completed`, `completed_at`, `actual_quantity_consumed`, `is_liked`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `trainer_saved_meals` — `diet.TrainerSavedMeal`

**Why.** A trainer's own library of saved meals.

**Whose.** Rows whose `trainer` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Posts, comments and other content you create (`user_content`).

**Fields stored.** `id`, `trainer`, `name`, `meal_type`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `trainer_saved_meal_items` — `diet.TrainerSavedMealItem`

**Why.** The foods and grams in a trainer's saved meals.

**Whose.** Rows whose `saved_meal__trainer` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Posts, comments and other content you create (`user_content`).

**Fields stored.** `id`, `saved_meal`, `food`, `grams`, `position`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `ai_usage_cost` — `ai_assistant.UsageCost`

**Why.** How much the person used the AI coach each day (messages, tokens, cost), for the daily allowance. An accounting record.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `user`, `date`, `total_tokens`, `total_messages`, `estimated_cost_usd`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `ai_insights` — `ai_assistant.UserInsight`

**Why.** Notes the platform computes about the person's training, diet and past chats, which the AI coach reads as its memory.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`); Conversations with the AI coach (`ai_conversations`).

**Fields stored.** `id`, `user`, `insight_type`, `content`, `confidence`, `expires_at`, `created_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `notification_preferences` — `notifications.UserNotificationPreference`

**Why.** Which notices the person wants, and on which channel.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `user`, `event_type`, `is_enabled`, `channels`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `exercises_created` — `routine.Exercise`

**Why.** Exercises a trainer created for their programs (name, description, image). Kept, because clients' routines use them.

**Whose.** Rows whose `created_by` is the account.

**Who else receives it.** The trainer's clients (`clients`); Google Cloud Storage (`file_storage`).

**Data types.** Posts, comments and other content you create (`user_content`); Photos and videos (`photos_videos`).

**Fields stored.** `id`, `name`, `name_en`, `name_ar`, `description`, `description_en`, `description_ar`, `image`, `target_muscle`, `difficulty_level`, `equipment`, `created_by`, `is_global`, `created_at`, `updated_at`, `is_active`, `catalogue_key`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `routines_created` — `routine.Routine`

**Why.** Routines a trainer wrote, each one a client's own copy. Kept, because the client's program is built on it.

**Whose.** Rows whose `created_by` is the account.

**Who else receives it.** The trainer's clients (`clients`); The AI service provider (`ai_provider`).

**Data types.** Posts, comments and other content you create (`user_content`); Fitness and training activity (`fitness`).

**Fields stored.** `id`, `name`, `description`, `translations`, `created_by`, `is_active`, `days`, `created_at`, `updated_at`, `source_template`, `purchase`, `start_date`, `end_date`, `day_names`, `difficulty_level`, `estimated_duration`, `assigned_to`, `exercises`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `templates_created` — `routine.RoutineTemplate`

**Why.** A trainer's reusable program templates. Kept.

**Whose.** Rows whose `created_by` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Posts, comments and other content you create (`user_content`).

**Fields stored.** `id`, `name`, `name_en`, `name_ar`, `description`, `description_en`, `description_ar`, `goal`, `days`, `weeks`, `created_by`, `is_public`, `created_at`, `updated_at`, `exercises`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `challenges_created` — `social.Challenge`

**Why.** Community challenges the person created (title, rules, image). Kept, because other people joined them.

**Whose.** Rows whose `creator` is the account.

**Who else receives it.** Other signed-in users (`community`); Google Cloud Storage (`file_storage`).

**Data types.** Posts, comments and other content you create (`user_content`); Photos and videos (`photos_videos`).

**Fields stored.** `id`, `title`, `title_en`, `title_ar`, `description`, `description_en`, `description_ar`, `challenge_type`, `creator`, `start_date`, `end_date`, `status`, `target_value`, `unit`, `rules`, `metric`, `max_participants`, `participants_count`, `reward_description`, `image`, `is_hidden`, `winner_checked_at`, `settled_active_participants`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `challenge_participation` — `social.ChallengeParticipation`

**Why.** Challenges the person joined, their progress and their place.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Other signed-in users (`community`).

**Data types.** Fitness and training activity (`fitness`); App activity (`app_activity`).

**Fields stored.** `id`, `user`, `challenge`, `status`, `current_value`, `progress_percentage`, `joined_at`, `completed_at`, `rank`, `review_state`, `review_reason`, `review_cleared_through`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `post_likes` — `social.PostLike`

**Why.** Posts the person liked.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Other signed-in users (`community`).

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `user`, `post`, `created_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `comment_likes` — `social.CommentLike`

**Why.** Comments the person liked.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Other signed-in users (`community`).

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `user`, `comment`, `created_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `follows` — `social.UserFollow`

**Why.** Who the person follows, and who follows them.

**Whose.** Rows whose `follower` or `following` is the account.

**Who else receives it.** Other signed-in users (`community`).

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `follower`, `following`, `created_at`, `notify_activity`, `show_in_feed`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `trainer_relations` — `users.TrainerClientRelation`

**Why.** The coaching link between a trainer and a client: the request and its note, the agreed monthly price, the state.

**Whose.** Rows whose `trainer` or `client` is the account.

**Who else receives it.** The client's trainer (`coach`); The trainer's clients (`clients`).

**Data types.** App activity (`app_activity`); Purchase and payment history (`purchases`).

**Fields stored.** `id`, `trainer`, `client`, `status`, `initiated_by`, `agreed_rate`, `agreed_currency`, `hold_reference`, `requested_at`, `ends_at_period_end`, `client_note`, `responded_at`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Also on erasure.** before any row goes, `_return_held_money` runs (`sources.py`).

**Retention.** While the account exists.

### `trainer_request_outcomes` — `users.TrainerRequestOutcome`

**Why.** Answered coaching requests (who asked whom, the answer, how fast), for a trainer's response-time figure.

**Whose.** Rows whose `trainer` or `client` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `trainer`, `client`, `initiated_by`, `outcome`, `requested_at`, `responded_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** Purged nightly: 180 days after `responded_at`.

### `coaching_endings` — `users.CoachingEnding`

**Why.** Why a coaching ended, as the client or the trainer chose it on leaving, for the platform's operators.

**Whose.** Rows whose `trainer` or `client` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `trainer`, `client`, `ended_by`, `reason`, `scheduled`, `created_at`.

**In the export.** `id`, `trainer`, `client`, `ended_by`, `scheduled`, `created_at`. Not exported: `reason`.

**On erasure.** Anonymised: `trainer`, `client` are cleared or replaced; `ended_by`, `reason`, `scheduled`, `created_at` stay on the row.

**Retention.** While the account exists.

### `otp_verifications` — `users.OTPVerification`

**Why.** One-time sign-up codes, stored hashed.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The email delivery provider (`email_provider`).

**Data types.** Sign-in codes (`credentials`).

**Fields stored.** `id`, `user`, `otp_code`, `email`, `purpose`, `created_at`, `expires_at`, `is_verified`, `verified_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** Purged nightly: 1 days after `created_at`.

### `password_reset_tokens` — `users.PasswordResetToken`

**Why.** Password-reset codes, stored hashed.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** The email delivery provider (`email_provider`).

**Data types.** Sign-in codes (`credentials`).

**Fields stored.** `id`, `user`, `token_hash`, `created_at`, `expires_at`, `is_used`, `used_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** Purged nightly: 1 days after `created_at`.

### `reports_made` — `moderation.Report`

**Why.** Reports the person made about content or people, and what came of them. On erasure the reporter's words go and the record of the complaint stays.

**Whose.** Rows whose `reporter` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Reports and requests you send us (`support`).

**Fields stored.** `id`, `reporter`, `target_type`, `target_id`, `reason`, `note`, `status`, `handled_by`, `handled_at`, `resolution_note`, `created_at`.

**In the export.** `id`, `target_type`, `target_id`, `reason`, `note`, `status`, `resolution_note`, `handled_at`, `created_at`. Not exported: `reporter`, `handled_by`.

**On erasure.** Anonymised: `note` are cleared or replaced; `reporter`, `target_type`, `target_id`, `reason`, `status`, `handled_by`, `handled_at`, `resolution_note`, `created_at` stay on the row.

**Retention.** While the account exists.

### `blocks` — `moderation.Block`

**Why.** The people this person blocked.

**Whose.** Rows whose `blocker` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `blocker`, `blocked`, `created_at`.

**In the export.** `id`, `blocked`, `created_at`. Not exported: `blocker`.

**On erasure.** Deleted.

**Also on erasure.** `_blocks_against` also deletes rows about the person that other people hold, which are not exported.

**Retention.** While the account exists.

### `ai_consent` — `moderation.AIConsent`

**Why.** Whether and when the person agreed that the AI coach may read their data, and to which policy version.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `user`, `granted_at`, `revoked_at`, `policy_version`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `agent_profile` — `wallet.AgentProfile`

**Why.** An agent's account: limits, status, allowed addresses, and the listing they agreed to show signed-in users (shop name, city, area, hours, shop phone). Part of the financial record.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Other signed-in users (`community`).

**Data types.** Name (`name`); Shop location of an agent (`address`); Phone number (`phone`); Wallet and payout details (`financial`); IP address and device details (`connection`).

**Fields stored.** `id`, `user`, `wallet_type`, `daily_limit`, `monthly_limit`, `daily_limit_usd`, `monthly_limit_usd`, `status`, `ip_allowlist`, `display_name`, `city`, `area`, `opening_hours`, `public_phone`, `is_listed`, `per_transaction_limit`, `per_transaction_limit_usd`, `receivable_ceiling`, `receivable_ceiling_usd`, `next_settlement_on`, `key_revoked_at`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `agent_receivable` — `wallet.AgentReceivableEntry`

**Why.** The cash an agent holds for the platform, movement by movement. A record of money.

**Whose.** Rows whose `agent__user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Wallet and payout details (`financial`).

**Fields stored.** `id`, `agent`, `kind`, `currency`, `amount`, `balance_after`, `money_type`, `transaction`, `settlement`, `created_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `agent_settlements` — `wallet.AgentSettlement`

**Why.** Settlements of that cash, recorded by an operator. A record of money.

**Whose.** Rows whose `agent__user` or `recorded_by` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Wallet and payout details (`financial`).

**Fields stored.** `id`, `agent`, `currency`, `amount`, `receivable_before`, `receivable_after`, `note`, `recorded_by`, `idempotency_key`, `created_at`.

**In the export.** `id`, `agent`, `currency`, `amount`, `receivable_before`, `receivable_after`, `created_at`. Not exported: `note`, `recorded_by`, `idempotency_key`.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `coaching_periods` — `users.CoachingPeriod`

**Why.** Each paid month of coaching: price, the platform's commission, the trainer's share, dates. A record of money.

**Whose.** Rows whose `trainer` or `client` is the account.

**Who else receives it.** The client's trainer (`coach`); The trainer's clients (`clients`).

**Data types.** Purchase and payment history (`purchases`).

**Fields stored.** `id`, `trainer`, `client`, `relation`, `number`, `price`, `currency`, `commission_rate`, `commission`, `trainer_share`, `price_amount`, `price_currency`, `exchange_rate`, `exchange_rate_value`, `payout_owed_since`, `status`, `hold_reference`, `payout_reference`, `commission_reference`, `starts_at`, `ends_at`, `renewal_reminder_sent_at`, `frozen_at`, `frozen_notices`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `routine_products` — `routine.RoutineProduct`

**Why.** Routines a trainer lists for sale: title, description, price, cover, review state. Kept, because buyers own copies.

**Whose.** Rows whose `trainer` is the account.

**Who else receives it.** Other signed-in users (`community`); Google Cloud Storage (`file_storage`).

**Data types.** Posts, comments and other content you create (`user_content`); Photos and videos (`photos_videos`).

**Fields stored.** `id`, `trainer`, `source_template`, `title_ar`, `title_en`, `description_ar`, `description_en`, `price`, `currency`, `preview_days`, `place`, `cover`, `status`, `submitted_at`, `template_version`, `review_decision`, `review_reason`, `reviewed_at`, `reviewed_by`, `published_at`, `retired_at`, `created_at`, `updated_at`.

**In the export.** `id`, `title_ar`, `title_en`, `description_ar`, `description_en`, `price`, `currency`, `preview_days`, `place`, `status`, `review_decision`, `review_reason`, `submitted_at`, `published_at`, `retired_at`, `created_at`. Not exported: `trainer`, `source_template`, `cover`, `template_version`, `reviewed_at`, `reviewed_by`, `updated_at`.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `routine_purchases` — `routine.RoutinePurchase`

**Why.** Routine products the person bought: the price, how it was paid, refunds. The trainer is never told who bought. A record of money.

**Whose.** Rows whose `buyer` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Purchase and payment history (`purchases`).

**Fields stored.** `id`, `buyer`, `product`, `status`, `money_type`, `price_amount`, `price_currency`, `amount`, `currency`, `exchange_rate`, `exchange_rate_value`, `commission_rate`, `commission`, `trainer_share`, `trainer_amount`, `trainer_currency`, `charge_reference`, `payout_reference`, `commission_reference`, `idempotency_key`, `paid_with`, `checkout_reference`, `refunded_at`, `refunded_by`, `refund_reason`, `created_at`, `updated_at`.

**In the export.** `id`, `product`, `status`, `money_type`, `price_amount`, `price_currency`, `amount`, `currency`, `exchange_rate_value`, `charge_reference`, `paid_with`, `refunded_at`, `refund_reason`, `created_at`. Not exported: `buyer`, `exchange_rate`, `commission_rate`, `commission`, `trainer_share`, `trainer_amount`, `trainer_currency`, `payout_reference`, `commission_reference`, `idempotency_key`, `checkout_reference`, `refunded_by`, `updated_at`.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `transactions` — `wallet.Transaction`

**Why.** Every movement of money the person made (top-ups, payments, transfers, payouts): the append-only ledger.

**Whose.** Rows whose `actor` is the account.

**Who else receives it.** The agent who tops a wallet up (`agent`).

**Data types.** Purchase and payment history (`purchases`); Wallet and payout details (`financial`).

**Fields stored.** `id`, `reference_id`, `tx_type`, `actor`, `source_wallet`, `destination_wallet`, `amount`, `currency`, `money_type`, `exchange_rate`, `exchange_rate_value`, `exchange_reference`, `metadata`, `reverses`, `created_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `exchange_rates` — `wallet.ExchangeRate`

**Why.** Which operator set which USD/SYP rate, and when.

**Whose.** Rows whose `set_by` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Operator records (`staff_records`).

**Fields stored.** `id`, `base`, `quote`, `rate`, `effective_from`, `set_by`, `note`, `created_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `wallet_audit_log` — `wallet.WalletAuditLog`

**Why.** An append-only trail of wallet actions: who, on which route, from which IP address and device.

**Whose.** Rows whose `actor` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Wallet and payout details (`financial`); IP address and device details (`connection`).

**Fields stored.** `id`, `event_type`, `actor`, `request_id`, `ip_address`, `user_agent`, `path`, `payload`, `created_at`, `prev_hash`, `entry_hash`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `idempotency_keys` — `wallet.IdempotencyKey`

**Why.** Keys that make a retried money request safe: the request's hash and the answer given.

**Whose.** Rows whose `created_by` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Purchase and payment history (`purchases`).

**Fields stored.** `id`, `key`, `request_hash`, `created_by`, `created_at`, `processed`, `response_snapshot`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Purged nightly: 30 days after `created_at`.

### `capability_changes` — `policy.CapabilityChangeLog`

**Why.** Which operator changed what a build may show and sell, and when.

**Whose.** Rows whose `changed_by` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Operator records (`staff_records`).

**Fields stored.** `id`, `channel`, `min_version`, `action`, `changed_by`, `old_values`, `new_values`, `created_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `theme_changes` — `policy.ThemeChangeLog`

**Why.** Which operator published which app theme, and when.

**Whose.** Rows whose `changed_by` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Operator records (`staff_records`).

**Fields stored.** `id`, `theme_id_at_the_time`, `name`, `action`, `changed_by`, `old_values`, `new_values`, `created_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `app_releases_published` — `releases.AppRelease`

**Why.** Which operator published which app build, and when.

**Whose.** Rows whose `published_by` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Operator records (`staff_records`).

**Fields stored.** `id`, `channel`, `version_name`, `build_number`, `min_supported_build`, `release_notes_ar`, `release_notes_en`, `apk_file`, `download_url`, `sha256`, `size_bytes`, `store_url`, `is_published`, `published_at`, `published_by`, `created_at`, `updated_at`.

**In the export.** `id`, `channel`, `version_name`, `build_number`, `published_at`. Not exported: `min_supported_build`, `release_notes_ar`, `release_notes_en`, `apk_file`, `download_url`, `sha256`, `size_bytes`, `store_url`, `is_published`, `published_by`, `created_at`, `updated_at`.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `legal_documents_published` — `legal.LegalDocument`

**Why.** Which operator published which version of the privacy policy and terms.

**Whose.** Rows whose `published_by` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Operator records (`staff_records`).

**Fields stored.** `id`, `kind`, `language`, `version`, `title`, `body`, `is_published`, `published_at`, `published_by`, `created_at`, `updated_at`.

**In the export.** `id`, `kind`, `language`, `version`, `published_at`. Not exported: `title`, `body`, `is_published`, `published_by`, `created_at`, `updated_at`.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `account_deletion_requests` — `legal.AccountDeletionRequest`

**Why.** A request to delete an account made on the public web page: the address and an optional reason, for an operator to confirm with the owner of the address and act on.

**Whose.** Rows whose `email__iexact` is `_deletion_request_address(account)`, derived on the server.

**Who else receives it.** Nobody outside the platform.

**Data types.** Email address (`email`); Reports and requests you send us (`support`).

**Fields stored.** `id`, `email`, `reason`, `status`, `operator_note`, `handled_at`, `handled_by`, `created_at`.

**In the export.** `id`, `email`, `reason`, `status`, `created_at`, `handled_at`. Not exported: `operator_note`, `handled_by`.

**On erasure.** Anonymised: `email`, `reason` are cleared or replaced; `status`, `operator_note`, `handled_at`, `handled_by`, `created_at` stay on the row.

**Retention.** Purged nightly: 180 days after `handled_at`.

### `assistant_messages` — `ai_assistant.ChatMessage`

**Why.** The person's messages to the AI coach and the coach's replies.

**Whose.** Rows whose `session__user` is the account.

**Who else receives it.** The AI service provider (`ai_provider`).

**Data types.** Conversations with the AI coach (`ai_conversations`).

**Fields stored.** `id`, `session`, `role`, `content`, `tool_calls`, `tool_results`, `tokens_used`, `feedback`, `created_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `exercise_set_logs` — `routine.ExerciseSetLog`

**Why.** Every set the person logged: weight, reps, rest, effort, personal records.

**Whose.** Rows whose `user_exercise_progress__user` or `workout_session__user` is the account.

**Who else receives it.** The client's trainer (`coach`); The AI service provider (`ai_provider`).

**Data types.** Fitness and training activity (`fitness`).

**Fields stored.** `id`, `user_exercise_progress`, `workout_session`, `set_number`, `weight`, `reps`, `rest_time`, `duration`, `rpe`, `notes`, `date`, `client_uuid`, `created_at`, `values_changed_at`, `personal_record`, `previous_best`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `exercise_media` — `routine.ExerciseMedia`

**Why.** Media links and text a trainer attached to their exercises. Kept with the exercise.

**Whose.** Rows whose `exercise__created_by` is the account.

**Who else receives it.** The trainer's clients (`clients`).

**Data types.** Posts, comments and other content you create (`user_content`).

**Fields stored.** `id`, `exercise`, `media_type`, `content`, `title`, `description`, `order`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `routine_exercises` — `routine.RoutineExercise`

**Why.** The exercises, sets and reps of each routine a trainer wrote. Kept with the routine.

**Whose.** Rows whose `routine__created_by` is the account.

**Who else receives it.** The trainer's clients (`clients`); The AI service provider (`ai_provider`).

**Data types.** Posts, comments and other content you create (`user_content`); Fitness and training activity (`fitness`).

**Fields stored.** `id`, `routine`, `exercise`, `sets`, `reps`, `rest_time`, `day`, `order`, `weight`, `duration`, `distance`, `notes`, `superset_group`, `load_mode`, `load_value`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `template_exercises` — `routine.RoutineTemplateExercise`

**Why.** The exercises of a trainer's templates. Kept with the template.

**Whose.** Rows whose `template__created_by` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Posts, comments and other content you create (`user_content`).

**Fields stored.** `id`, `template`, `exercise`, `sets`, `reps`, `rest_time`, `day`, `order`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `payments` — `subscription.Payment`

**Why.** Each payment for a plan, or for a routine product bought through a checkout: amount, currency, state, the gateway's reference, invoice number and what it pays for. A record of money.

**Whose.** Rows whose `subscription__user` or `buyer` is the account.

**Who else receives it.** ShamCash (`payment_gateway`).

**Data types.** Purchase and payment history (`purchases`).

**Fields stored.** `id`, `subscription`, `buyer`, `routine_product`, `amount`, `currency`, `price_amount`, `price_currency`, `exchange_rate`, `exchange_rate_value`, `status`, `payment_method`, `money_type`, `transaction_id`, `payment_intent_id`, `gateway_response`, `gateway_error`, `gateway_transaction_reference`, `gateway_event_id`, `invoice_number`, `completed_at`, `covers_from`, `covers_until`, `payout_settled_at`, `description`, `metadata`, `created_at`, `updated_at`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `subscription_usage` — `subscription.SubscriptionUsage`

**Why.** How much of a plan's allowances the person used in each period.

**Whose.** Rows whose `subscription__user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** App activity (`app_activity`).

**Fields stored.** `id`, `subscription`, `feature`, `usage_count`, `limit`, `period_start`, `period_end`.

**In the export.** Every field.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `transfer_reviews` — `subscription.TransferReview`

**Why.** A transfer held for an operator's review against the person's checkout, with the sender and note as ShamCash reported them. A record of money.

**Whose.** Rows whose `payment__subscription__user` or `payment__buyer` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Purchase and payment history (`purchases`); Wallet and payout details (`financial`).

**Fields stored.** `id`, `gateway`, `transaction_id`, `amount`, `currency`, `note`, `sender`, `occurred_at`, `raw`, `reason`, `payment`, `candidate_payment_ids`, `sender_match_payment_ids`, `status`, `resolved_by`, `resolved_at`, `resolution_note`, `created_at`.

**In the export.** `id`, `reason`, `status`, `amount`, `currency`, `occurred_at`, `created_at`, `resolved_at`. Not exported: `gateway`, `transaction_id`, `note`, `sender`, `raw`, `payment`, `candidate_payment_ids`, `sender_match_payment_ids`, `resolved_by`, `resolution_note`.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `payer_address` — `subscription.PayerAddress`

**Why.** The ShamCash account address the person pays from (encrypted), so an operator can match a transfer with a missing reference to their checkout.

**Whose.** Rows whose `user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Wallet and payout details (`financial`).

**Fields stored.** `id`, `user`, `address`, `address_digest`, `verified_at`, `created_at`, `updated_at`.

**In the export.** `id`, `address`, `verified_at`, `created_at`, `updated_at`. Not exported: `user`, `address_digest`.

**On erasure.** Deleted.

**Retention.** While the account exists.

### `payment_events` — `subscription.PaymentEvent`

**Why.** Every state the person's payment went through and who or what moved it. A record of money.

**Whose.** Rows whose `payment__subscription__user` or `payment__buyer` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Purchase and payment history (`purchases`).

**Fields stored.** `id`, `payment`, `from_status`, `to_status`, `actor`, `actor_role`, `evidence`, `backfill`, `created_at`.

**In the export.** `id`, `payment`, `from_status`, `to_status`, `actor_role`, `backfill`, `created_at`. Not exported: `actor`, `evidence`.

**On erasure.** Kept.

**Retention.** Kept for good, and after erasure.

### `agent_api_keys` — `wallet.AgentAPIKey`

**Why.** An agent's API keys (hashed or encrypted) and the name of the device they were paired with.

**Whose.** Rows whose `agent__user` is the account.

**Who else receives it.** Nobody outside the platform.

**Data types.** Sign-in codes (`credentials`).

**Fields stored.** `id`, `agent`, `key_id`, `hashed_key`, `secret_ciphertext`, `is_active`, `created_at`, `last_rotated_at`, `device_name`, `last_signature_at`.

**In the export.** Every field.

**On erasure.** Deleted.

**Retention.** While the account exists.
