"""What each app holds about a user.

Registered once, here, so export / erasure / retention stay in step. Retention windows
answer P12-03: `UserActivity` and `UserSession` carry IP address and user agent and were
previously kept forever.
"""
from django.conf import settings
from django.utils.crypto import get_random_string

from .registry import PersonalDataSource, register

# --- identity -----------------------------------------------------------------
register(PersonalDataSource(
    label="profile", model="users.CustomUser", user_field="pk",
    fields=["id", "username", "email", "phone_number", "first_name", "last_name",
            "age", "date_of_birth", "gender", "height", "weight", "activity_level",
            "specific_injury", "injury_areas", "client_goals", "training_goal",
            "training_place", "training_days_per_week", "date_joined",
            # The "help us improve" answer and when it was given (BACKEND_BUGS B-084).
            "ai_training_consent", "ai_training_consent_set_at",
            # What the person chose or wrote that the list above left out (SIDE_BUGS
            # S-214): their photo, language and reminder settings, their preferences,
            # a trainer's whole professional card, and when they last signed in. The
            # console's `groups` and `user_permissions` stay out: a declared list is
            # what the export holds (`registry._export_links`).
            "user_type", "profile_picture", "preferred_language", "preferred_timezone",
            "workout_reminder_hour", "activity_level_answered_at", "client_preferences",
            "trainer_bio", "trainer_specializations", "trainer_certifications",
            "trainer_experience_years", "trainer_hourly_rate", "trainer_is_verified",
            "trainer_is_available", "trainer_goal_focus", "trainer_injury_focus",
            "trainer_training_places", "last_login"],
    on_erase="anonymise",
    anonymise={
        "is_active": False,
        "email": lambda u: f"retired+{u.pk}-{get_random_string(12)}@invalid.local",
        "username": lambda u: f"retired_{u.pk}_{get_random_string(12)}",
        "phone_number": "0000000000",
        "first_name": "", "last_name": "",
        "specific_injury": "", "client_goals": [],
        # Health data and the date that identifies a person; the onboarding answers
        # beside them say nothing once these are gone, but go too.
        "date_of_birth": None, "injury_areas": [],
        "training_goal": "", "training_place": "", "training_days_per_week": None,
        # The body measurements and the rest of what the app plans from (SIDE_BUGS
        # S-213). They stayed on the deactivated row while the deletion page promised
        # "your measurements" are deleted. `activity_level` has a CHECK that admits only
        # a declared level, so it goes back to its default with the stamp cleared, which
        # is how the column says "never asked".
        "height": None, "weight": None, "age": None, "gender": None,
        "activity_level": "Sedentary", "activity_level_answered_at": None,
        "client_preferences": {},
        # A trainer's professional profile, written by them and shown on the public
        # directory: an erased trainer is not a card anyone should be able to rebuild.
        "trainer_bio": None, "trainer_specializations": [], "trainer_certifications": [],
        "trainer_experience_years": None, "trainer_hourly_rate": None,
        "trainer_goal_focus": [], "trainer_injury_focus": [], "trainer_training_places": [],
        # The denormalised pointer to the trainer. The relation rows are deleted by the
        # trainer_relations source below; leaving this set would keep the display name
        # of a trainer on an erased account, and leave the two sources of truth
        # disagreeing in the one direction that grants access.
        "assigned_trainer": None,
        # Clearing the field triggers the post-commit receiver that removes
        # the stored file; leaving it would orphan the image.
        "profile_picture": None,
        # A consent dies with the account, as `ai_consent` below does.
        "ai_training_consent": False, "ai_training_consent_set_at": None,
    },
    purpose=("The account: signing in, reaching the person (codes by email), and what the "
             "app plans training and nutrition from (age, body measurements, goals, "
             "injuries). A trainer's professional profile and price are what clients choose "
             "a trainer by."),
    recipients=("coach", "clients", "community", "public", "ai_provider", "email_provider",
                "file_storage"),
    data_types=("name", "email", "phone", "user_ids", "other_personal", "health",
                "fitness", "photos_videos", "user_content"),
))
register(PersonalDataSource(
    label="devices", model="users.DeviceToken", user_field="user", on_erase="delete",
    purpose="Delivering push notifications to the person's phones.",
    recipients=("push_provider",),
    data_types=("device_ids",)))
# A trainer's photographs (the media kit). Deleted with the account; the files go with
# the rows (training_platform/signals.py). `uploaded_by` names the operator who
# uploaded a picture and is not what makes it personal data.
register(PersonalDataSource(
    label="media_kit", model="mediakit.MediaAsset", user_field="person",
    # `owner_id` joined the export with the per-trainer exercise library: on a
    # `trainer_exercise` row it is the EXERCISE the trainer filmed, which is a fact
    # about them — which moves this coach demonstrates — and an export that hid it
    # would list a row the subject could not identify. `person` still selects exactly
    # the rows that belong to someone, so erasure needed no change: a trainer's
    # demonstrations go with their account, files and all.
    fields=["owner_type", "owner_id", "slot", "alt_ar", "alt_en", "created_at"],
    on_erase="delete",
    purpose=("A trainer's photos and videos on their profile and card, and the exercise "
             "demonstrations they film."),
    recipients=("public", "community", "clients", "file_storage"),
    data_types=("photos_videos",)))

# A trainer's verification (trainer_verification/): identity cards, certificates, a
# photo, and what operators decided about them. Deleted with the account, files included
# — and the FILES FIRST: each document's file and thumbnail are deleted from storage
# before its row (`trainer_verification.services.delete_documents`), and when storage
# refuses, the whole erasure is refused (`erasure_refused`, retry later) rather than
# committed with an ID card left in the bucket and no row pointing at it. Documents
# first, so each source's count is its own.
# While the account lives: current documents and decisions are kept (they are the
# evidence the verification rests on); a REPLACED document is deleted 90 days after it
# was replaced (`trainer_verification.purge_replaced_documents`, daily), and what was
# decided about it survives in the decision's snapshot. The export lists the rows, not
# the files and not the operator who decided.
def _delete_verification_documents(user, queryset):
    from trainer_verification.services import DocumentFilesNotDeleted, delete_documents

    from .registry import ErasureRefused

    try:
        delete_documents(queryset.order_by("id"))
    except DocumentFilesNotDeleted as exc:
        # A file deleted before the one that failed stays deleted: storage cannot roll
        # back. Its row comes back with the rollback, and the retry finds it gone, which
        # counts as deleted.
        raise ErasureRefused(
            "A verification document could not be deleted from storage, so nothing was "
            "erased.", cause="storage") from exc


register(PersonalDataSource(
    label="verification_documents", model="trainer_verification.VerificationDocument",
    user_field="application__trainer",
    fields=["id", "slot", "kind", "content_type", "size", "state", "rejection_reason",
            "uploaded_at", "replaced_at", "purge_after"],
    on_erase="delete", delete_rows=_delete_verification_documents,
    purpose=("Proving a trainer's identity and qualifications before an operator marks them "
             "verified, which withdrawals require."),
    recipients=("file_storage",),
    data_types=("documents",)))
register(PersonalDataSource(
    label="verification_decisions", model="trainer_verification.VerificationDecision",
    user_field="application__trainer",
    fields=["id", "round", "outcome", "reason", "documents", "decided_at"],
    on_erase="delete",
    purpose=("What operators decided about a trainer's documents, and why: the record the "
             "verified badge rests on."),
    recipients=(),
    data_types=("documents",)))
register(PersonalDataSource(
    label="verification_application", model="trainer_verification.VerificationApplication",
    user_field="trainer",
    fields=["id", "state", "round", "submitted_at", "decided_at", "rejection_reason",
            "created_at"],
    on_erase="delete",
    purpose="A trainer's request to be verified, and its state.",
    recipients=(),
    data_types=("documents",)))

# A trainer's payouts (payouts/). A payout METHOD is personal data and nothing else — a
# ShamCash address and the account holder's name, both encrypted at rest — so it goes
# with the account. A WITHDRAWAL is a record of money, kept like the ledger rows it
# names; it carries its own copy of the destination (encrypted), which is what the
# money was actually sent to. Its events are kept with it; the export lists what
# happened and when, not the operator who did it or their notes.
register(PersonalDataSource(
    label="payout_methods", model="payouts.PayoutMethod", user_field="trainer",
    fields=["id", "kind", "shamcash_address", "holder_name", "agent", "is_default",
            "verified_at", "created_at"],
    on_erase="delete",
    purpose=("Where a trainer's withdrawals are paid: a ShamCash address and the holder's "
             "name (encrypted), or cash at a named agent."),
    recipients=("payment_gateway",),
    data_types=("financial", "name")))
register(PersonalDataSource(
    label="withdrawals", model="payouts.Withdrawal", user_field="trainer",
    on_erase="keep",
    purpose="Each withdrawal a trainer asked for and how it was paid. A record of money.",
    recipients=("payment_gateway",),
    data_types=("financial",)))
register(PersonalDataSource(
    label="withdrawal_events", model="payouts.WithdrawalEvent",
    user_field="withdrawal__trainer",
    fields=["id", "withdrawal", "from_state", "to_state", "actor_role", "created_at"],
    on_erase="keep",
    purpose=("Every step of a withdrawal (requested, approved, sent, confirmed, failed), "
             "unchangeable once written."),
    recipients=(),
    data_types=("financial",)))

# An operator's console sign-ins from the phone (admin_dashboard/console_login.py,
# B-120): when each link was made, opened, approved and used, and the address and
# browser of the computer that opened it. The row is the operator's own audit trail;
# it goes with the account and is kept 90 days. The secrets are hashes and the number
# the computer showed means nothing once the link is closed, so none is exported.
register(PersonalDataSource(
    label="console_logins", model="admin_dashboard.ConsoleLogin", user_field="user",
    fields=["id", "state", "created_at", "expires_at", "claimed_at", "claimed_ip",
            "claimed_user_agent", "approved_at", "used_at", "cancelled_at",
            "cancel_reason"],
    on_erase="delete", retention_days=90,
    purpose=("An operator's sign-ins to the console from their phone: when, and the address "
             "and browser of the computer. The operator's own audit trail."),
    recipients=(),
    data_types=("staff_records",)))

# --- training -----------------------------------------------------------------
register(PersonalDataSource(label="workout_sessions", model="routine.WorkoutSession", user_field="user",
                            purpose=("The workouts the person does: when, which routine "
                                     "day, how it felt."),
                            recipients=("coach", "ai_provider"),
                            data_types=("fitness",)))
register(PersonalDataSource(label="exercise_progress", model="routine.UserExerciseProgress", user_field="user",
                            purpose=("Progress on each exercise per day: sets, reps, weight "
                                     "lifted, notes."),
                            recipients=("coach", "ai_provider"),
                            data_types=("fitness",)))
register(PersonalDataSource(label="routine_progress", model="routine.RoutineProgress", user_field="user",
                            purpose="Progress through each day of the person's routine.",
                            recipients=("coach", "ai_provider"),
                            data_types=("fitness",)))
# What each training day planned against what was done (routine/outcomes.py). Deleted
# with the person, like the progress rows it summarises.
register(PersonalDataSource(label="day_outcomes", model="routine.DayOutcome", user_field="user",
                            purpose=("What each training day planned against what was done: "
                                     "adherence and streaks."),
                            recipients=("coach", "ai_provider"),
                            data_types=("fitness",)))

# --- nutrition ----------------------------------------------------------------
register(PersonalDataSource(label="diet_plans", model="diet.DietPlan", user_field="user",
                            purpose=("The person's diet plans: calorie and macro targets, "
                                     "dates, the planned meals and the allergen check of "
                                     "each plan."),
                            recipients=("coach", "ai_provider"),
                            data_types=("fitness", "health")))
register(PersonalDataSource(label="food_preferences", model="diet.UserFoodPreference", user_field="user",
                            purpose=("The foods the person likes, dislikes and chooses, and "
                                     "the allergies they declared, so plans leave out what "
                                     "they must not eat."),
                            recipients=("coach",),
                            data_types=("health", "fitness")))

# --- social -------------------------------------------------------------------
register(PersonalDataSource(label="posts", model="social.Post", user_field="author",
                            purpose="Posts the person publishes to the community feed.",
                            recipients=("community", "file_storage"),
                            data_types=("user_content", "photos_videos")))
register(PersonalDataSource(label="comments", model="social.Comment", user_field="author",
                            purpose="Comments the person writes on posts.",
                            recipients=("community",),
                            data_types=("user_content",)))

# --- analytics: IP + user agent, so these get a retention window ---------------
register(PersonalDataSource(
    label="activity_log", model="analytics.UserActivity", user_field="user",
    retention_days=180, retention_field="timestamp",
    purpose=("A log of what the person did in the app (a routine completed, a meal ticked, "
             "a plan generated), which achievements count. A row the app posts carries its "
             "IP address and device string."),
    recipients=(),
    data_types=("app_activity", "connection")))
register(PersonalDataSource(
    # `started_at`, not `session_start`: no such field exists on UserSession, so the
    # purge raised FieldError, purge_expired() logged it and moved on, and these rows —
    # the ones holding IP address and user agent, the reason retention was added at all
    # — were never deleted. validate_sources() below now makes that impossible to ship.
    label="sessions", model="analytics.UserSession", user_field="user",
    retention_days=180, retention_field="started_at",
    purpose=("App sessions the app reports (start, end, counts), with the IP address and "
             "device string."),
    recipients=(),
    data_types=("app_activity", "connection")))
register(PersonalDataSource(
    label="performance_metrics", model="analytics.PerformanceMetric", user_field="user",
    purpose=("Measurements over time (body weight, the chest, arm, waist and thigh "
             "circumferences the person logs, workout duration) for progress charts and "
             "badges."),
    recipients=(),
    data_types=("health", "fitness")))

# --- assistant ----------------------------------------------------------------
register(PersonalDataSource(label="chat_sessions", model="ai_assistant.ChatSession", user_field="user",
                            purpose=("The person's conversations with the AI coach: titles, "
                                     "message counts and cost."),
                            recipients=(),
                            data_types=("ai_conversations",)))
register(PersonalDataSource(
    label="behaviour_events", model="ai_assistant.UserBehaviorEvent", user_field="user",
    retention_days=365,
    purpose=("What the person did (workouts completed or abandoned, sets, meals, plans, AI "
             "chats), from which the AI coach's notes about their habits are computed."),
    recipients=(),
    data_types=("app_activity", "fitness")))
def _training_subject_key(user):
    from ai_assistant.anonymise import subject_key_for

    return subject_key_for(user)


# The anonymous copies kept for improving the coach while "help us improve" is on (the
# owner, 2026-09-24). No FK reaches the person: a copy is found by `subject_key`, the
# keyed pseudonym of the user id, which is how erasure deletes them. The export hands
# the person every copy as it is stored, so they can see that it holds no name, account
# or measurement, but without the two pseudonyms, which mean nothing to anyone
# without the server secret.
register(PersonalDataSource(
    label="ai_training_data", model="ai_assistant.AITrainingData",
    user_field="subject_key", user_value=_training_subject_key,
    fields=["created_at", "retain_until", "user_context_snapshot", "user_message",
            "tools_called", "tool_results", "ai_response", "user_feedback"],
    retention_days=settings.AI_TRAINING_RETENTION_DAYS, retention_field="retain_until",
    retention_is_deadline=True,
    purpose=("Anonymous copies of AI coach conversations, kept only while the person has "
             "\"help us improve\" on, to improve the coach. No name or account, bands instead "
             "of measurements; deleted when the setting goes off, with the account, or "
             "when the retention ends."),
    recipients=(),
    data_types=("ai_conversations", "health")))

# --- chat ---------------------------------------------------------------------
# A private 1:1 thread is JOINT personal data: both people are in every row of it, so
# an erasure takes the thread with the account rather than leaving the other side
# reading half a conversation with a retired stranger. `Conversation` reaches its owner
# by either side, and deleting it cascades to the messages; `Message` is registered
# too so an export can hand someone what they wrote.
#
# The FILES of the chat's photos, voice notes and videos (B-091) go with the rows, and
# not through a per-row delete receiver: `chat.Message` keeps Django's fast delete, so a
# coach's erasure is a DELETE, not every message loaded into memory (review 2026-09-24,
# `training_platform/signals.py FILES_DELETED_EXPLICITLY`). Two steps sweep them, both
# on commit, a rolled-back erasure keeping every file:
#   * deleting a conversation reads its messages' files first
#     (`chat.media.delete_conversation_files`, a `pre_delete` on Conversation);
#   * `messages_sent` deletes the person's messages by SENDER as a plain DELETE, so its
#     own `before_erase` reads their files first — whether or not the thread goes too.
# A file both steps reach is deleted once (`chat.media.delete_files_on_commit`). The
# preview reports `files`: how many stored files the person sent.
def _chat_files_sent(user, queryset, *, dry_run):
    # `stored_names`: the attachment AND the poster frame a worker cut from a video
    # (2026-09-27). `files` still counts what the person SENT; a poster is derived from
    # one of those and goes with it.
    from chat.media import CHAT_FILES, delete_stored_on_commit, stored_names

    names = stored_names(queryset)
    if not dry_run:
        delete_stored_on_commit(names)
    return {"files": len(names[CHAT_FILES])}


register(PersonalDataSource(
    label="conversations", model="chat.Conversation", user_field=("trainer", "client"),
    fields=["id", "created_at", "last_message_at"], on_erase="delete",
    purpose="The one chat thread between a trainer and each client they coach.",
    recipients=("coach", "clients"),
    data_types=("messages",)))
register(PersonalDataSource(
    label="messages_sent", model="chat.Message", user_field="sender",
    # `kind`, `exercise` and `duration_seconds` with the chat's photos, voice notes and
    # videos (B-091); like the media kit's, the export lists them and does not bundle
    # the bytes (nor a video's poster frame, which is cut from it). A trainer's program
    # card (2026-09-27) names its `routine` and `program_day`.
    fields=["id", "conversation", "kind", "body", "exercise", "duration_seconds",
            "routine", "program_day", "created_at", "read_at", "deleted_at"],
    on_erase="delete", before_erase=_chat_files_sent,
    purpose=("What the person sent in the trainer-client chat: text, photos, voice notes "
             "and videos, a form-check video naming an exercise, a coach's card "
             "pointing at the client's program."),
    recipients=("coach", "clients", "push_provider", "file_storage"),
    data_types=("messages", "photos_videos", "audio")))

# --- notifications ------------------------------------------------------------
# The chat notices this person SENT go with them too. Each one sits in the other side's
# inbox holding the sender's name and the first 120 characters of what they wrote
# (`chat/events.py`), and the conversation it points at is deleted above — so without
# this the thread vanished and the other side's inbox kept rendering the erased
# person's name and words for the 90 days of retention. Not exported: they are the
# recipient's inbox, not the sender's data to download.
def _chat_notices_sent(user):
    from notifications.models import Notification

    return Notification.objects.filter(actor=user, event_type="chat_message")


register(PersonalDataSource(
    label="notifications", model="notifications.Notification", user_field="recipient",
    retention_days=90, erase_also=_chat_notices_sent,
    purpose=("The person's in-app inbox of notices (payments, coaching, chat, reminders); "
             "each push goes out through Firebase."),
    recipients=("push_provider",),
    data_types=("app_activity",)))
# The emails the notification system owed this person, and whether each left
# (notifications/channels/email.py). No address and no text is stored — the kind, the
# ids it was written from, the outcome — and the same window as the inbox.
register(PersonalDataSource(
    label="notification_emails", model="notifications.EmailDelivery", user_field="recipient",
    fields=["id", "kind", "context", "status", "language", "created_at", "sent_at"],
    retention_days=90,
    purpose=("Which notification emails (payment receipt, plan reminders, withdrawals) the "
             "platform owed the person and whether each was sent; no address or text is "
             "stored here."),
    recipients=("email_provider",),
    data_types=("app_activity",)))

# --- financial: KEPT. Wallet.owner is PROTECT so a deletion cannot erase a balance,
#     and payment history has to survive an erasure request.
register(PersonalDataSource(label="wallet", model="wallet.Wallet", user_field="owner", on_erase="keep",
                            purpose=("The person's wallet: balance and currency. A record "
                                     "of money."),
                            recipients=(),
                            data_types=("financial",)))
register(PersonalDataSource(label="subscriptions", model="subscription.Subscription", user_field="user", on_erase="keep",
                            purpose=("The person's plan: which one, from when to when, and "
                                     "where it came from (paid, granted by an operator, "
                                     "trial). A record of money."),
                            recipients=(),
                            data_types=("purchases",)))

# --- remainder, from audit_coverage() ------------------------------------------
# Everything else holding a user FK. Registered explicitly so the coverage audit is
# empty and a model added later is the only thing that shows up in it.

register(PersonalDataSource(label="achievements_earned", model="achievements.UserAchievement", user_field="user",
                            purpose="Badges the person earned.",
                            recipients=("community",),
                            data_types=("app_activity",)))
register(PersonalDataSource(label="achievement_progress", model="achievements.AchievementProgress", user_field="user",
                            purpose="Progress toward each badge.",
                            recipients=(),
                            data_types=("app_activity",)))
register(PersonalDataSource(label="social_achievements", model="social.UserAchievement", user_field="user",
                            purpose="Community badges the person earned.",
                            recipients=("community",),
                            data_types=("app_activity",)))

register(PersonalDataSource(label="goals", model="analytics.UserGoal", user_field="user",
                            purpose=("The person's goals (workouts a week, a target weight) "
                                     "and progress toward them."),
                            recipients=(),
                            data_types=("fitness", "health")))
register(PersonalDataSource(label="dashboard", model="analytics.AnalyticsDashboard", user_field="user",
                            retention_days=180, retention_field="computed_at",
                            purpose=("Summaries of the person's progress computed for the "
                                     "dashboard screens."),
                            recipients=(),
                            data_types=("fitness",)))

register(PersonalDataSource(label="daily_advice", model="diet.DailyAdvice", user_field="user",
                            retention_days=365, retention_field="generated_at",
                            purpose=("The day's nutrition tip, written by the platform's "
                                     "own rules from the person's goal, calorie target and "
                                     "BMI (not by an AI)."),
                            recipients=(),
                            data_types=("fitness", "health")))
register(PersonalDataSource(label="daily_progress", model="diet.DailyProgress", user_field="user",
                            purpose=("Each day's nutrition against the plan's targets: "
                                     "meals ticked, calories and macros."),
                            recipients=("coach", "ai_provider"),
                            data_types=("fitness",)))
register(PersonalDataSource(label="food_category_preferences", model="diet.UserFoodCategoryPreference", user_field="user",
                            purpose="Foods the person picked for each meal or macro.",
                            recipients=(),
                            data_types=("fitness",)))
register(PersonalDataSource(label="learned_food_weights", model="diet.UserFoodWeight", user_field="user",
                            purpose=("What the planner learned the person prefers, from "
                                     "their swaps and ticks, so plans fit them better."),
                            recipients=(),
                            data_types=("fitness",)))
register(PersonalDataSource(label="meal_swaps", model="diet.MealSwap", user_field="user",
                            purpose=("Foods the person swapped out of a meal, and what they "
                                     "chose instead."),
                            recipients=(),
                            data_types=("fitness",)))
register(PersonalDataSource(label="meal_events", model="diet.MealEvent", user_field="user",
                            purpose="When the person ticked, skipped or rated a meal.",
                            recipients=(),
                            data_types=("fitness",)))
# The meals themselves, and what was on them. These reach the client through
# `DietPlan.user`, two hops from CustomUser, which is why the coverage audit could not
# see them and an Art. 15 export left out everything the client had actually eaten —
# names, foods, grams, what they finished and what they rated.
register(PersonalDataSource(label="meals", model="diet.Meal",
                            user_field="diet_plan__user",
                            purpose=("The meals of each diet plan: names, times, what was "
                                     "eaten, likes and notes."),
                            recipients=("coach", "ai_provider"),
                            data_types=("fitness",)))
register(PersonalDataSource(label="meal_components", model="diet.MealComponent",
                            user_field="meal__diet_plan__user",
                            purpose=("The foods and grams of each meal, and what was "
                                     "actually eaten."),
                            recipients=("coach", "ai_provider"),
                            data_types=("fitness",)))
# A trainer's saved meals (BACKEND_BUGS B-130): the trainer's own library, holding no
# client's data. Exported to that trainer and deleted with their account.
register(PersonalDataSource(label="trainer_saved_meals", model="diet.TrainerSavedMeal",
                            user_field="trainer",
                            purpose="A trainer's own library of saved meals.",
                            recipients=(),
                            data_types=("user_content",)))
register(PersonalDataSource(label="trainer_saved_meal_items", model="diet.TrainerSavedMealItem",
                            user_field="saved_meal__trainer",
                            purpose="The foods and grams in a trainer's saved meals.",
                            recipients=(),
                            data_types=("user_content",)))

register(PersonalDataSource(label="ai_usage_cost", model="ai_assistant.UsageCost", user_field="user", on_erase="keep",
                            purpose=("How much the person used the AI coach each day "
                                     "(messages, tokens, cost), for the daily allowance. An "
                                     "accounting record."),
                            recipients=(),
                            data_types=("app_activity",)))
register(PersonalDataSource(label="ai_insights", model="ai_assistant.UserInsight", user_field="user",
                            purpose=("Notes the platform computes about the person's "
                                     "training, diet and past chats, which the AI coach "
                                     "reads as its memory."),
                            recipients=("ai_provider",),
                            data_types=("fitness", "ai_conversations")))

register(PersonalDataSource(label="notification_preferences", model="notifications.UserNotificationPreference", user_field="user",
                            purpose="Which notices the person wants, and on which channel.",
                            recipients=(),
                            data_types=("app_activity",)))

# Content the user authored. Erasure detaches rather than deletes where the content is
# part of someone else's experience — but here every one is theirs alone.
register(PersonalDataSource(label="exercises_created", model="routine.Exercise", user_field="created_by", on_erase="keep",
                            purpose=("Exercises a trainer created for their programs (name, "
                                     "description, image). Kept, because clients' routines "
                                     "use them."),
                            recipients=("clients", "file_storage"),
                            data_types=("user_content", "photos_videos")))
# Both sides of a routine (SIDE_BUGS S-214): the trainer who wrote it and the client it
# was written for. On `created_by` alone a client's own program — the routine their
# coach wrote them and its exercises below — was in the trainer's export and missing
# from theirs. Kept on erasure either way: nothing changes for the rows.
register(PersonalDataSource(label="routines_created", model="routine.Routine",
                            user_field=("created_by", "assigned_to"), on_erase="keep",
                            purpose=("Routines a trainer wrote, each one a client's own "
                                     "copy, and the routines written for the person. Kept, "
                                     "because the client's program is built on it."),
                            recipients=("clients", "ai_provider"),
                            data_types=("user_content", "fitness")))
register(PersonalDataSource(label="templates_created", model="routine.RoutineTemplate", user_field="created_by", on_erase="keep",
                            purpose="A trainer's reusable program templates. Kept.",
                            recipients=(),
                            data_types=("user_content",)))

register(PersonalDataSource(label="challenges_created", model="social.Challenge", user_field="creator", on_erase="keep",
                            purpose=("Community challenges the person created (title, "
                                     "rules, image). Kept, because other people joined "
                                     "them."),
                            recipients=("community", "file_storage"),
                            data_types=("user_content", "photos_videos")))
register(PersonalDataSource(label="challenge_participation", model="social.ChallengeParticipation", user_field="user",
                            purpose=("Challenges the person joined, their progress and "
                                     "their place."),
                            recipients=("community",),
                            data_types=("fitness", "app_activity")))
register(PersonalDataSource(label="post_likes", model="social.PostLike", user_field="user",
                            purpose="Posts the person liked.",
                            recipients=("community",),
                            data_types=("app_activity",)))
register(PersonalDataSource(label="comment_likes", model="social.CommentLike", user_field="user",
                            purpose="Comments the person liked.",
                            recipients=("community",),
                            data_types=("app_activity",)))
# BOTH directions. Registered on "follower" alone, an erasure dropped who you follow and
# left every row saying who follows you — the erased account stayed in other people's
# follower lists and counts.
register(PersonalDataSource(label="follows", model="social.UserFollow",
                            user_field=("follower", "following"),
                            purpose="Who the person follows, and who follows them.",
                            recipients=("community",),
                            data_types=("app_activity",)))

# BOTH sides of the bond, for the same reason and with a sharper consequence. Registered
# on "trainer" alone, a CLIENT's erasure left the relation row standing: the trainer kept
# them on the client list, kept reading their profile endpoint, and kept creating routines
# attached to them — writing NEW personal data about someone who had asked to be
# forgotten — and the erased account could no longer call unassign-trainer to stop it,
# because erasure had deactivated it.
#
# And the row is also a record of money: `hold_reference` names the client's charge
# sitting in escrow until a routine is assigned. It is returned before the row goes, and
# if it cannot be returned nothing is erased. See `release_holds_before_erasure`.
#
# A trainer who still coaches someone cannot be erased at all (the owner, 2026-09-19):
# the client paid for a month of coaching that an erased account cannot deliver.
def _return_held_money(user, queryset, *, dry_run):
    from users.trainer_relations import (
        HoldFailed,
        TrainerHasActiveClients,
        release_holds_before_erasure,
    )

    from .registry import ErasureRefused

    try:
        return release_holds_before_erasure(user, queryset, dry_run=dry_run)
    except TrainerHasActiveClients as exc:
        raise ErasureRefused(
            "The trainer still coaches active clients, so nothing was erased.",
            code="trainer_has_active_clients", active_clients=exc.count) from exc
    except HoldFailed as exc:
        raise ErasureRefused(
            "Money held for a trainer relation could not be returned, so nothing was "
            "erased.") from exc


register(PersonalDataSource(label="trainer_relations", model="users.TrainerClientRelation",
                            user_field=("trainer", "client"),
                            before_erase=_return_held_money,
                            purpose=("The coaching link between a trainer and a client: the "
                                     "request and its note, the agreed monthly price, the "
                                     "state."),
                            recipients=("coach", "clients"),
                            data_types=("app_activity", "purchases")))
# Answered requests, kept for the trainer's response-time statistic. Deleted with either
# person: the statistic loses a sample, and nothing else depends on the row. The
# statistic reads the last 90 days (users/trainer_proof.py); twice that is kept, then the
# record of who asked whom goes.
register(PersonalDataSource(label="trainer_request_outcomes", model="users.TrainerRequestOutcome",
                            user_field=("trainer", "client"),
                            retention_days=180, retention_field="responded_at",
                            purpose=("Answered coaching requests (who asked whom, the "
                                     "answer, how fast), for a trainer's response-time "
                                     "figure."),
                            recipients=(),
                            data_types=("app_activity",)))
# Why each coaching ended, from the leave and remove sheets (BACKEND_BUGS B-159, B-160).
# The owner (2026-09-28): seen by the platform's operators only, never by the other
# person — so nobody outside receives it — and on erasure the row is KEPT, anonymised:
# the links to both people go, who ended it, the reason and the date stay. The export
# leaves the reason out: a trainer's reason is about the client and is never shown to
# the client, and one field list serves both people's exports.
register(PersonalDataSource(label="coaching_endings", model="users.CoachingEnding",
                            user_field=("trainer", "client"),
                            fields=["id", "trainer", "client", "ended_by", "scheduled",
                                    "created_at"],
                            on_erase="anonymise",
                            anonymise={"trainer": None, "client": None},
                            purpose=("Why a coaching ended, as the client or the trainer "
                                     "chose it on leaving, for the platform's operators."),
                            recipients=(),
                            data_types=("app_activity",)))
# Short-lived credentials: deleted on erasure, and expired rows purged on a schedule.
register(PersonalDataSource(label="otp_verifications", model="users.OTPVerification", user_field="user",
                            retention_days=1,
                            purpose="One-time sign-up codes, stored hashed.",
                            recipients=("email_provider",),
                            data_types=("credentials",)))
register(PersonalDataSource(label="password_reset_tokens", model="users.PasswordResetToken", user_field="user",
                            retention_days=1,
                            purpose="Password-reset codes, stored hashed.",
                            recipients=("email_provider",),
                            data_types=("credentials",)))

# --- safety: reports, blocks, AI consent (moderation/) -------------------------
# A report is the platform's evidence that a complaint was made and answered — the
# thing a store asks to see — so it is ANONYMISED rather than deleted when the
# reporter erases their account: the row, its reason, its outcome and the operator's
# note survive, and the reporter's own words go with them. `reporter` is CASCADE and
# the account itself is only anonymised, so the row stays attached to an account that
# no longer identifies anybody. Reports made AGAINST the person are untouched by
# design: `target_id` is not a foreign key, so erasing an account does not erase the
# complaints about it.
#
# The export carries what `GET reports/mine/` carries (`moderation.serializers
# .ReportSerializer`) and nothing more. With no `fields` it exported every column,
# `handled_by` among them: the operator who decided each complaint, which the API and the
# contract both promise the reporter is never told.
register(PersonalDataSource(label="reports_made", model="moderation.Report",
                            user_field="reporter",
                            fields=["id", "target_type", "target_id", "reason", "note",
                                    "status", "resolution_note", "handled_at",
                                    "created_at"],
                            on_erase="anonymise",
                            anonymise={"note": ""},
                            purpose=("Reports the person made about content or people, and "
                                     "what came of them. On erasure the reporter's words go "
                                     "and the record of the complaint stays."),
                            recipients=(),
                            data_types=("support",)))
# Blocks are one person's private list and mean nothing once the account is gone.
# The list is the blocks this person MADE — that is all the export shows, as `GET
# blocks/` does. A block made AGAINST them is never disclosed, so those rows are not in
# `user_field` (which the export follows); they are deleted with the account through
# `erase_also`, which neither exports nor counts them. Registered on both sides, the
# export told a blocked person exactly who had blocked them.
def _blocks_against(user):
    from moderation.models import Block

    return Block.objects.filter(blocked=user)


register(PersonalDataSource(label="blocks", model="moderation.Block",
                            user_field="blocker", fields=["id", "blocked", "created_at"],
                            on_erase="delete", erase_also=_blocks_against,
                            purpose="The people this person blocked.",
                            recipients=(),
                            data_types=("app_activity",)))
# Consent dies with the account: there is nothing left to process, and a consent row
# for a retired account is a permission nobody can exercise.
register(PersonalDataSource(label="ai_consent", model="moderation.AIConsent",
                            user_field="user", on_erase="delete",
                            purpose=("Whether and when the person agreed that the AI coach "
                                     "may read their data, and to which policy version."),
                            recipients=(),
                            data_types=("app_activity",)))

# Financial and audit: KEPT under erasure. Deleting these would destroy the ledger.
# `fields=None`, so the PUBLIC LISTING an agent agreed to — display_name, city, area,
# opening_hours and public_phone (the shop line, never the number they registered
# with) — travels with their own export and is kept under erasure like the rest of the
# financial record. A new field on this model needs no change here; a new model does.
register(PersonalDataSource(label="agent_profile", model="wallet.AgentProfile", user_field="user", on_erase="keep",
                            purpose=("An agent's account: limits, status, allowed "
                                     "addresses, and the listing they agreed to show "
                                     "signed-in users (shop name, city, area, hours, shop "
                                     "phone). Part of the financial record."),
                            recipients=("community",),
                            data_types=("name", "address", "phone", "financial",
                                        "connection")))
# The cash an agent held for the platform, movement by movement, and the settlements an
# operator recorded against it (plan step 9). Records of money, kept like the ledger rows
# they name. A settlement also names the operator who recorded it, so it is in that
# operator's export too; the export lists what was handed over and when, not the
# operator's note (as withdrawal events above).
register(PersonalDataSource(label="agent_receivable", model="wallet.AgentReceivableEntry",
                            user_field="agent__user", on_erase="keep",
                            purpose=("The cash an agent holds for the platform, movement by "
                                     "movement. A record of money."),
                            recipients=(),
                            data_types=("financial",)))
register(PersonalDataSource(label="agent_settlements", model="wallet.AgentSettlement",
                            user_field=("agent__user", "recorded_by"),
                            fields=["id", "agent", "currency", "amount",
                                    "receivable_before", "receivable_after", "created_at"],
                            on_erase="keep",
                            purpose=("Settlements of that cash, recorded by an operator. A "
                                     "record of money."),
                            recipients=(),
                            data_types=("financial",)))
# Each paid month of coaching: what was charged, the commission and the trainer's share.
# Kept like the ledger rows it names.
register(PersonalDataSource(label="coaching_periods", model="users.CoachingPeriod",
                            user_field=("trainer", "client"), on_erase="keep",
                            purpose=("Each paid month of coaching: price, the platform's "
                                     "commission, the trainer's share, dates. A record of "
                                     "money."),
                            recipients=("coach", "clients"),
                            data_types=("purchases",)))
# Routine products (plan step 10). A trainer's listing is authored content that buyers'
# purchases point at, and a purchase is a record of money like the ledger rows it names:
# both kept on erasure. A purchase is exported to its BUYER only — the trainer's own
# export carries their listings and their sales totals are in their ledger, never who
# bought (a purchase is not a coaching). The operator who reviewed or refunded is named
# on the row and in no one's export.
register(PersonalDataSource(
    label="routine_products", model="routine.RoutineProduct", user_field="trainer",
    fields=["id", "title_ar", "title_en", "description_ar", "description_en", "price",
            "currency", "preview_days", "place", "status", "review_decision",
            "review_reason", "submitted_at", "published_at", "retired_at", "created_at"],
    on_erase="keep",
    purpose=("Routines a trainer lists for sale: title, description, price, cover, review "
             "state. Kept, because buyers own copies."),
    recipients=("community", "file_storage"),
    data_types=("user_content", "photos_videos")))
register(PersonalDataSource(
    label="routine_purchases", model="routine.RoutinePurchase", user_field="buyer",
    fields=["id", "product", "status", "money_type", "price_amount", "price_currency", "amount",
            "currency", "exchange_rate_value", "charge_reference", "paid_with",
            "refunded_at", "refund_reason", "created_at"],
    on_erase="keep",
    purpose=("Routine products the person bought: the price, how it was paid, refunds. The "
             "trainer is never told who bought. A record of money."),
    recipients=(),
    data_types=("purchases",)))
register(PersonalDataSource(label="transactions", model="wallet.Transaction", user_field="actor", on_erase="keep",
                            purpose=("Every movement of money the person made (top-ups, "
                                     "payments, transfers, payouts): the append-only "
                                     "ledger."),
                            recipients=("agent",),
                            data_types=("purchases", "financial")))
# A client's direct payment to a trainer (BACKEND_BUGS B-163, the owner 2026-09-28): the
# split of a record of money, which is kept, and the client's optional line to the
# trainer, which is user-written text and goes with EITHER person's account. The note
# is why this is not the ledger: the ledger is append-only and kept through an erasure.
register(PersonalDataSource(
    label="coaching_transfers", model="wallet.CoachingTransfer",
    user_field=("client", "trainer"),
    fields=["id", "client", "trainer", "amount", "currency", "commission_rate",
            "commission", "trainer_share", "charge_reference", "note", "created_at"],
    on_erase="anonymise", anonymise={"note": ""},
    purpose=("A client's payment to a trainer from the wallet: the amount, the platform's "
             "commission, the trainer's share, and the note the client wrote to the "
             "trainer, which both of them see. The note is erased with either account; "
             "the rest is a record of money."),
    recipients=("coach",),
    data_types=("purchases", "messages")))
# Which operator set which USD/SYP rate, and when. Names an operator, never a customer,
# and it is the evidence that every converted purchase was struck at a rate a named
# person chose. Kept — the row is append-only and past money depends on it.
register(PersonalDataSource(label="exchange_rates", model="wallet.ExchangeRate",
                            user_field="set_by", on_erase="keep",
                            purpose="Which operator set which USD/SYP rate, and when.",
                            recipients=(),
                            data_types=("staff_records",)))
register(PersonalDataSource(label="wallet_audit_log", model="wallet.WalletAuditLog", user_field="actor", on_erase="keep",
                            purpose=("An append-only trail of wallet actions: who, on which "
                                     "route, from which IP address and device."),
                            recipients=(),
                            data_types=("financial", "connection")))
register(PersonalDataSource(label="idempotency_keys", model="wallet.IdempotencyKey", user_field="created_by",
                            on_erase="keep", retention_days=30,
                            purpose=("Keys that make a retried money request safe: the "
                                     "request's hash and the answer given."),
                            recipients=(),
                            data_types=("purchases",)))
# Which operator changed what a build may sell. Kept: it is the platform's evidence to a
# store that a policy change was made by a named person, and it holds no customer data.
register(PersonalDataSource(label="capability_changes", model="policy.CapabilityChangeLog",
                            user_field="changed_by", on_erase="keep",
                            purpose=("Which operator changed what a build may show and "
                                     "sell, and when."),
                            recipients=(),
                            data_types=("staff_records",)))
# Which operator published which skin. Same reasoning as the line above: it names an
# operator, never a customer, and it is the evidence that a look the app shipped was
# signed off by a person. Kept.
register(PersonalDataSource(label="theme_changes", model="policy.ThemeChangeLog",
                            user_field="changed_by", on_erase="keep",
                            purpose="Which operator published which app theme, and when.",
                            recipients=(),
                            data_types=("staff_records",)))
# Which operator published which app build (releases/). Names an operator, never a
# customer, and it is the record of who told every phone to install what. Kept.
register(PersonalDataSource(label="app_releases_published", model="releases.AppRelease",
                            user_field="published_by",
                            fields=["id", "channel", "version_name", "build_number",
                                    "published_at"],
                            on_erase="keep",
                            purpose="Which operator published which app build, and when.",
                            recipients=(),
                            data_types=("staff_records",)))
# Which operator published which wording of the privacy policy and terms (legal/). The
# published text is what people were shown and agreed to: kept.
register(PersonalDataSource(label="legal_documents_published", model="legal.LegalDocument",
                            user_field="published_by",
                            fields=["id", "kind", "language", "version", "published_at"],
                            on_erase="keep",
                            purpose=("Which operator published which version of the privacy "
                                     "policy and terms."),
                            recipients=(),
                            data_types=("staff_records",)))
# A request to delete an account, made on the public web page by someone who could not
# open the app (legal/, spec B6). Keyed by the ADDRESS the requester typed, never by an
# account — the page must not look one up — so it is matched to a person by their email.
# Exported to them; on their erasure the record that a request was made and handled
# stays and the address and the words go (anonymised). Retention: 180 days after an
# operator handled it; an open request waits for its operator. The operator named in
# `handled_by` is the platform's record of who answered, in no one's export.
def _deletion_request_address(user):
    """An account without an address matches no request — not every anonymised one,
    whose address is "". A request is never stored with a space in its address."""
    return user.email or " "


register(PersonalDataSource(label="account_deletion_requests",
                            model="legal.AccountDeletionRequest",
                            user_field="email__iexact",
                            user_value=_deletion_request_address,
                            fields=["id", "email", "reason", "status", "created_at",
                                    "handled_at"],
                            on_erase="anonymise",
                            anonymise={"email": "", "reason": ""},
                            retention_days=180, retention_field="handled_at",
                            purpose=("A request to delete an account made on the public web "
                                     "page: the address and an optional reason, for an "
                                     "operator to confirm with the owner of the address and "
                                     "act on."),
                            recipients=(),
                            data_types=("email", "support")))

# --- reachable only through another row ---------------------------------------
# Everything below holds data about a person but does not point at them directly, so
# the coverage audit's old "does this model have a ForeignKey to CustomUser" test could
# not see any of it. `_reaches_a_user` follows the chain now, and these are what it
# found.
register(PersonalDataSource(label="assistant_messages", model="ai_assistant.ChatMessage",
                            user_field="session__user",
                            purpose=("The person's messages to the AI coach and the coach's "
                                     "replies."),
                            recipients=("ai_provider",),
                            data_types=("ai_conversations",)))
# What the client actually lifted, set by set. The most personal thing the training
# side records, and it was in no export.
register(PersonalDataSource(label="exercise_set_logs", model="routine.ExerciseSetLog",
                            user_field=("user_exercise_progress__user",
                                        "workout_session__user"),
                            purpose=("Every set the person logged: weight, reps, rest, "
                                     "effort, personal records."),
                            recipients=("coach", "ai_provider"),
                            data_types=("fitness",)))
# Authored content: kept on erasure, exactly like the Exercise/Routine/Template rows
# they hang off, so another user's routine does not lose its exercises.
register(PersonalDataSource(label="exercise_media", model="routine.ExerciseMedia",
                            user_field="exercise__created_by", on_erase="keep",
                            purpose=("Media links and text a trainer attached to their "
                                     "exercises. Kept with the exercise."),
                            recipients=("clients",),
                            data_types=("user_content",)))
register(PersonalDataSource(label="routine_exercises", model="routine.RoutineExercise",
                            user_field=("routine__created_by", "routine__assigned_to"),
                            on_erase="keep",
                            purpose=("The exercises, sets and reps of each routine a "
                                     "trainer wrote, and of each routine written for the "
                                     "person. Kept with the routine."),
                            recipients=("clients", "ai_provider"),
                            data_types=("user_content", "fitness")))
register(PersonalDataSource(label="template_exercises", model="routine.RoutineTemplateExercise",
                            user_field="template__created_by", on_erase="keep",
                            purpose=("The exercises of a trainer's templates. Kept with the "
                                     "template."),
                            recipients=(),
                            data_types=("user_content",)))
# Financial, and therefore kept: a payment is part of the ledger the wallet sources
# already protect.
# A routine product bought through a checkout (the web hub) is a payment too, with its
# buyer and no subscription: both routes reach the person.
register(PersonalDataSource(label="payments", model="subscription.Payment",
                            user_field=("subscription__user", "buyer"), on_erase="keep",
                            purpose=("Each payment for a plan, or for a routine product "
                                     "bought through a checkout: amount, currency, state, "
                                     "the gateway's reference, invoice number and what it "
                                     "pays for. A record of money."),
                            recipients=("payment_gateway",),
                            data_types=("purchases",)))
register(PersonalDataSource(label="subscription_usage", model="subscription.SubscriptionUsage",
                            user_field="subscription__user", on_erase="keep",
                            purpose=("How much of a plan's allowances the person used in "
                                     "each period."),
                            recipients=(),
                            data_types=("app_activity",)))
# A transfer held for review against a customer's checkout (B-051): the sender's note
# and account as the gateway reported them. A financial record like the payment it
# belongs to — kept on erasure, exported to the customer it names. A transfer queued
# with no checkout named belongs to nobody yet and is not reachable from a person.
# Only what is the customer's: the transfer's sender, note and gateway record are the
# SENDER's — a transfer with no reference is attributed to a checkout by its amount, so
# the sender may be someone else — and the candidates are other customers' payments.
register(PersonalDataSource(label="transfer_reviews", model="subscription.TransferReview",
                            user_field=("payment__subscription__user", "payment__buyer"),
                            fields=["id", "reason", "status", "amount", "currency",
                                    "occurred_at", "created_at", "resolved_at"],
                            on_erase="keep",
                            purpose=("A transfer held for an operator's review against the "
                                     "person's checkout, with the sender and note as "
                                     "ShamCash reported them. A record of money."),
                            recipients=(),
                            data_types=("purchases", "financial")))
# The public ShamCash address a customer pays from, saved on their profile (the spec,
# section 6: "Save the address, not the account"). Personal data and nothing else —
# encrypted at rest, like a trainer's payout address — so it goes with the account. Used
# only as evidence that a transfer came from this customer; the digest is a keyed hash
# and is not exported. `verified_at` (when a transfer from it first confirmed one of the
# person's own checkouts, SIDE_BUGS S-241) is theirs and is exported.
register(PersonalDataSource(label="payer_address", model="subscription.PayerAddress",
                            user_field="user",
                            fields=["id", "address", "verified_at", "created_at",
                                    "updated_at"],
                            on_erase="delete",
                            purpose=("The ShamCash account address the person pays from "
                                     "(encrypted), so an operator can match a transfer "
                                     "with a missing reference to their checkout."),
                            recipients=(),
                            data_types=("financial",)))
# Every state a customer's payment went through (plan step 5; immutable at the database,
# `subscription/migrations/0016`). A record of money like the payment it belongs to:
# kept on erasure, exported to the customer. The export lists the steps and who or what
# took them, not the evidence (a transfer's details are its sender's, a note is the
# operator's) and not which operator.
register(PersonalDataSource(label="payment_events", model="subscription.PaymentEvent",
                            user_field=("payment__subscription__user", "payment__buyer"),
                            fields=["id", "payment", "from_status", "to_status",
                                    "actor_role", "backfill", "created_at"],
                            on_erase="keep",
                            purpose=("Every state the person's payment went through and "
                                     "who or what moved it. A record of money."),
                            recipients=(),
                            data_types=("purchases",)))
# A live credential, not a record. An erased account must not keep one.
register(PersonalDataSource(label="agent_api_keys", model="wallet.AgentAPIKey",
                            user_field="agent__user",
                            purpose=("An agent's API keys (hashed or encrypted) and the "
                                     "name of the device they were paired with."),
                            recipients=(),
                            data_types=("credentials",)))
