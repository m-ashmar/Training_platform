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
            "ai_training_consent", "ai_training_consent_set_at"],
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
))
register(PersonalDataSource(
    label="devices", model="users.DeviceToken", user_field="user", on_erase="delete"))
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
    on_erase="delete"))

# --- training -----------------------------------------------------------------
register(PersonalDataSource(label="workout_sessions", model="routine.WorkoutSession", user_field="user"))
register(PersonalDataSource(label="exercise_progress", model="routine.UserExerciseProgress", user_field="user"))
register(PersonalDataSource(label="routine_progress", model="routine.RoutineProgress", user_field="user"))
# What each training day planned against what was done (routine/outcomes.py). Deleted
# with the person, like the progress rows it summarises.
register(PersonalDataSource(label="day_outcomes", model="routine.DayOutcome", user_field="user"))

# --- nutrition ----------------------------------------------------------------
register(PersonalDataSource(label="diet_plans", model="diet.DietPlan", user_field="user"))
register(PersonalDataSource(label="food_preferences", model="diet.UserFoodPreference", user_field="user"))

# --- social -------------------------------------------------------------------
register(PersonalDataSource(label="posts", model="social.Post", user_field="author"))
register(PersonalDataSource(label="comments", model="social.Comment", user_field="author"))

# --- analytics: IP + user agent, so these get a retention window ---------------
register(PersonalDataSource(
    label="activity_log", model="analytics.UserActivity", user_field="user",
    retention_days=180, retention_field="timestamp"))
register(PersonalDataSource(
    # `started_at`, not `session_start`: no such field exists on UserSession, so the
    # purge raised FieldError, purge_expired() logged it and moved on, and these rows —
    # the ones holding IP address and user agent, the reason retention was added at all
    # — were never deleted. validate_sources() below now makes that impossible to ship.
    label="sessions", model="analytics.UserSession", user_field="user",
    retention_days=180, retention_field="started_at"))
register(PersonalDataSource(
    label="performance_metrics", model="analytics.PerformanceMetric", user_field="user"))

# --- assistant ----------------------------------------------------------------
register(PersonalDataSource(label="chat_sessions", model="ai_assistant.ChatSession", user_field="user"))
register(PersonalDataSource(
    label="behaviour_events", model="ai_assistant.UserBehaviorEvent", user_field="user",
    retention_days=365))
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
    retention_is_deadline=True))

# --- chat ---------------------------------------------------------------------
# A private 1:1 thread is JOINT personal data: both people are in every row of it, so
# an erasure takes the thread with the account rather than leaving the other side
# reading half a conversation with a retired stranger. `Conversation` reaches its owner
# by either side, and deleting it cascades to the messages; `Message` is registered
# too so an export can hand someone what they wrote.
register(PersonalDataSource(
    label="conversations", model="chat.Conversation", user_field=("trainer", "client"),
    fields=["id", "created_at", "last_message_at"], on_erase="delete"))
register(PersonalDataSource(
    label="messages_sent", model="chat.Message", user_field="sender",
    # `kind`, `exercise` and `duration_seconds` with the chat's photos, voice notes and
    # videos (B-091). The FILES are deleted with the rows — here, and through the
    # conversation's cascade above (`training_platform/signals.py`, app `chat`); like
    # the media kit's, the export lists them and does not bundle the bytes.
    fields=["id", "conversation", "kind", "body", "exercise", "duration_seconds",
            "created_at", "read_at", "deleted_at"],
    on_erase="delete"))

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
    retention_days=90, erase_also=_chat_notices_sent))

# --- financial: KEPT. Wallet.owner is PROTECT so a deletion cannot erase a balance,
#     and payment history has to survive an erasure request.
register(PersonalDataSource(label="wallet", model="wallet.Wallet", user_field="owner", on_erase="keep"))
register(PersonalDataSource(label="subscriptions", model="subscription.Subscription", user_field="user", on_erase="keep"))

# --- remainder, from audit_coverage() ------------------------------------------
# Everything else holding a user FK. Registered explicitly so the coverage audit is
# empty and a model added later is the only thing that shows up in it.

register(PersonalDataSource(label="achievements_earned", model="achievements.UserAchievement", user_field="user"))
register(PersonalDataSource(label="achievement_progress", model="achievements.AchievementProgress", user_field="user"))
register(PersonalDataSource(label="social_achievements", model="social.UserAchievement", user_field="user"))

register(PersonalDataSource(label="goals", model="analytics.UserGoal", user_field="user"))
register(PersonalDataSource(label="dashboard", model="analytics.AnalyticsDashboard", user_field="user",
                            retention_days=180, retention_field="computed_at"))

register(PersonalDataSource(label="daily_advice", model="diet.DailyAdvice", user_field="user",
                            retention_days=365, retention_field="generated_at"))
register(PersonalDataSource(label="daily_progress", model="diet.DailyProgress", user_field="user"))
register(PersonalDataSource(label="food_category_preferences", model="diet.UserFoodCategoryPreference", user_field="user"))
register(PersonalDataSource(label="learned_food_weights", model="diet.UserFoodWeight", user_field="user"))
register(PersonalDataSource(label="meal_swaps", model="diet.MealSwap", user_field="user"))
register(PersonalDataSource(label="meal_events", model="diet.MealEvent", user_field="user"))
# The meals themselves, and what was on them. These reach the client through
# `DietPlan.user`, two hops from CustomUser, which is why the coverage audit could not
# see them and an Art. 15 export left out everything the client had actually eaten —
# names, foods, grams, what they finished and what they rated.
register(PersonalDataSource(label="meals", model="diet.Meal",
                            user_field="diet_plan__user"))
register(PersonalDataSource(label="meal_components", model="diet.MealComponent",
                            user_field="meal__diet_plan__user"))

register(PersonalDataSource(label="ai_usage_cost", model="ai_assistant.UsageCost", user_field="user", on_erase="keep"))
register(PersonalDataSource(label="ai_insights", model="ai_assistant.UserInsight", user_field="user"))

register(PersonalDataSource(label="notification_preferences", model="notifications.UserNotificationPreference", user_field="user"))

# Content the user authored. Erasure detaches rather than deletes where the content is
# part of someone else's experience — but here every one is theirs alone.
register(PersonalDataSource(label="exercises_created", model="routine.Exercise", user_field="created_by", on_erase="keep"))
register(PersonalDataSource(label="routines_created", model="routine.Routine", user_field="created_by", on_erase="keep"))
register(PersonalDataSource(label="templates_created", model="routine.RoutineTemplate", user_field="created_by", on_erase="keep"))

register(PersonalDataSource(label="challenges_created", model="social.Challenge", user_field="creator", on_erase="keep"))
register(PersonalDataSource(label="challenge_participation", model="social.ChallengeParticipation", user_field="user"))
register(PersonalDataSource(label="post_likes", model="social.PostLike", user_field="user"))
register(PersonalDataSource(label="comment_likes", model="social.CommentLike", user_field="user"))
# BOTH directions. Registered on "follower" alone, an erasure dropped who you follow and
# left every row saying who follows you — the erased account stayed in other people's
# follower lists and counts.
register(PersonalDataSource(label="follows", model="social.UserFollow",
                            user_field=("follower", "following")))

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
                            before_erase=_return_held_money))
# Answered requests, kept for the trainer's response-time statistic. Deleted with either
# person: the statistic loses a sample, and nothing else depends on the row. The
# statistic reads the last 90 days (users/trainer_proof.py); twice that is kept, then the
# record of who asked whom goes.
register(PersonalDataSource(label="trainer_request_outcomes", model="users.TrainerRequestOutcome",
                            user_field=("trainer", "client"),
                            retention_days=180, retention_field="responded_at"))
# Short-lived credentials: deleted on erasure, and expired rows purged on a schedule.
register(PersonalDataSource(label="otp_verifications", model="users.OTPVerification", user_field="user",
                            retention_days=1))
register(PersonalDataSource(label="password_reset_tokens", model="users.PasswordResetToken", user_field="user",
                            retention_days=1))

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
                            anonymise={"note": ""}))
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
                            on_erase="delete", erase_also=_blocks_against))
# Consent dies with the account: there is nothing left to process, and a consent row
# for a retired account is a permission nobody can exercise.
register(PersonalDataSource(label="ai_consent", model="moderation.AIConsent",
                            user_field="user", on_erase="delete"))

# Financial and audit: KEPT under erasure. Deleting these would destroy the ledger.
# `fields=None`, so the PUBLIC LISTING an agent agreed to — display_name, city, area,
# opening_hours and public_phone (the shop line, never the number they registered
# with) — travels with their own export and is kept under erasure like the rest of the
# financial record. A new field on this model needs no change here; a new model does.
register(PersonalDataSource(label="agent_profile", model="wallet.AgentProfile", user_field="user", on_erase="keep"))
# Each paid month of coaching: what was charged, the commission and the trainer's share.
# Kept like the ledger rows it names.
register(PersonalDataSource(label="coaching_periods", model="users.CoachingPeriod",
                            user_field=("trainer", "client"), on_erase="keep"))
register(PersonalDataSource(label="transactions", model="wallet.Transaction", user_field="actor", on_erase="keep"))
# Which operator set which USD/SYP rate, and when. Names an operator, never a customer,
# and it is the evidence that every converted purchase was struck at a rate a named
# person chose. Kept — the row is append-only and past money depends on it.
register(PersonalDataSource(label="exchange_rates", model="wallet.ExchangeRate",
                            user_field="set_by", on_erase="keep"))
register(PersonalDataSource(label="wallet_audit_log", model="wallet.WalletAuditLog", user_field="actor", on_erase="keep"))
register(PersonalDataSource(label="idempotency_keys", model="wallet.IdempotencyKey", user_field="created_by",
                            on_erase="keep", retention_days=30))
# Which operator changed what a build may sell. Kept: it is the platform's evidence to a
# store that a policy change was made by a named person, and it holds no customer data.
register(PersonalDataSource(label="capability_changes", model="policy.CapabilityChangeLog",
                            user_field="changed_by", on_erase="keep"))
# Which operator published which skin. Same reasoning as the line above: it names an
# operator, never a customer, and it is the evidence that a look the app shipped was
# signed off by a person. Kept.
register(PersonalDataSource(label="theme_changes", model="policy.ThemeChangeLog",
                            user_field="changed_by", on_erase="keep"))

# --- reachable only through another row ---------------------------------------
# Everything below holds data about a person but does not point at them directly, so
# the coverage audit's old "does this model have a ForeignKey to CustomUser" test could
# not see any of it. `_reaches_a_user` follows the chain now, and these are what it
# found.
register(PersonalDataSource(label="assistant_messages", model="ai_assistant.ChatMessage",
                            user_field="session__user"))
# What the client actually lifted, set by set. The most personal thing the training
# side records, and it was in no export.
register(PersonalDataSource(label="exercise_set_logs", model="routine.ExerciseSetLog",
                            user_field=("user_exercise_progress__user",
                                        "workout_session__user")))
# Authored content: kept on erasure, exactly like the Exercise/Routine/Template rows
# they hang off, so another user's routine does not lose its exercises.
register(PersonalDataSource(label="exercise_media", model="routine.ExerciseMedia",
                            user_field="exercise__created_by", on_erase="keep"))
register(PersonalDataSource(label="routine_exercises", model="routine.RoutineExercise",
                            user_field="routine__created_by", on_erase="keep"))
register(PersonalDataSource(label="template_exercises", model="routine.RoutineTemplateExercise",
                            user_field="template__created_by", on_erase="keep"))
# Financial, and therefore kept: a payment is part of the ledger the wallet sources
# already protect.
register(PersonalDataSource(label="payments", model="subscription.Payment",
                            user_field="subscription__user", on_erase="keep"))
register(PersonalDataSource(label="subscription_usage", model="subscription.SubscriptionUsage",
                            user_field="subscription__user", on_erase="keep"))
# A transfer held for review against a customer's checkout (B-051): the sender's note
# and account as the gateway reported them. A financial record like the payment it
# belongs to — kept on erasure, exported to the customer it names. A transfer queued
# with no checkout named belongs to nobody yet and is not reachable from a person.
# Only what is the customer's: the transfer's sender, note and gateway record are the
# SENDER's — a transfer with no reference is attributed to a checkout by its amount, so
# the sender may be someone else — and the candidates are other customers' payments.
register(PersonalDataSource(label="transfer_reviews", model="subscription.TransferReview",
                            user_field="payment__subscription__user",
                            fields=["id", "reason", "status", "amount", "currency",
                                    "occurred_at", "created_at", "resolved_at"],
                            on_erase="keep"))
# A live credential, not a record. An erased account must not keep one.
register(PersonalDataSource(label="agent_api_keys", model="wallet.AgentAPIKey",
                            user_field="agent__user"))
