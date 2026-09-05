"""What each app holds about a user.

Registered once, here, so export / erasure / retention stay in step. Retention windows
answer P12-03: `UserActivity` and `UserSession` carry IP address and user agent and were
previously kept forever.
"""
from django.utils.crypto import get_random_string

from .registry import PersonalDataSource, register

# --- identity -----------------------------------------------------------------
register(PersonalDataSource(
    label="profile", model="users.CustomUser", user_field="pk",
    fields=["id", "username", "email", "phone_number", "first_name", "last_name",
            "age", "gender", "height", "weight", "activity_level", "specific_injury",
            "client_goals", "date_joined"],
    on_erase="anonymise",
    anonymise={
        "is_active": False,
        "email": lambda u: f"retired+{u.pk}-{get_random_string(12)}@invalid.local",
        "username": lambda u: f"retired_{u.pk}_{get_random_string(12)}",
        "phone_number": "0000000000",
        "first_name": "", "last_name": "",
        "specific_injury": "", "client_goals": [],
        # Clearing the field triggers the post-commit receiver that removes
        # the stored file; leaving it would orphan the image.
        "profile_picture": None,
    },
))
register(PersonalDataSource(
    label="devices", model="users.DeviceToken", user_field="user", on_erase="delete"))

# --- training -----------------------------------------------------------------
register(PersonalDataSource(label="workout_sessions", model="routine.WorkoutSession", user_field="user"))
register(PersonalDataSource(label="exercise_progress", model="routine.UserExerciseProgress", user_field="user"))
register(PersonalDataSource(label="routine_progress", model="routine.RoutineProgress", user_field="user"))

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
register(PersonalDataSource(
    label="ai_training_data", model="ai_assistant.AITrainingData", user_field="user",
    retention_days=365, retention_field="retain_until"))

# --- notifications ------------------------------------------------------------
register(PersonalDataSource(
    label="notifications", model="notifications.Notification", user_field="recipient",
    retention_days=90))

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
register(PersonalDataSource(label="follows", model="social.UserFollow", user_field="follower"))

register(PersonalDataSource(label="trainer_relations", model="users.TrainerClientRelation", user_field="trainer"))
# Short-lived credentials: deleted on erasure, and expired rows purged on a schedule.
register(PersonalDataSource(label="otp_verifications", model="users.OTPVerification", user_field="user",
                            retention_days=1))
register(PersonalDataSource(label="password_reset_tokens", model="users.PasswordResetToken", user_field="user",
                            retention_days=1))

# Financial and audit: KEPT under erasure. Deleting these would destroy the ledger.
register(PersonalDataSource(label="agent_profile", model="wallet.AgentProfile", user_field="user", on_erase="keep"))
register(PersonalDataSource(label="transactions", model="wallet.Transaction", user_field="actor", on_erase="keep"))
register(PersonalDataSource(label="wallet_audit_log", model="wallet.WalletAuditLog", user_field="actor", on_erase="keep"))
register(PersonalDataSource(label="idempotency_keys", model="wallet.IdempotencyKey", user_field="created_by",
                            on_erase="keep", retention_days=30))
