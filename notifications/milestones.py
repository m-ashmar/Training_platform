"""
Progress milestones — the `progress_milestone` notification.

The event type and its template were registered from the start but nothing ever
constructed the event, so users crossed every threshold in silence.

Two ladders, both computed from data the platform already records:

  * **Streak** — consecutive days with a workout, the same figure
    `achievements.engine._calculate_workout_streak` uses, so a user is never told
    "7-day streak" by one subsystem while another still says 6.
  * **Volume** — cumulative completed workout sessions.

Idempotency is not handled here. `NotificationService.create_and_send` dedupes on
(recipient, event_type, related_object_id), and each milestone passes a stable id like
`streak-7`, so a threshold notifies exactly once per user for good — no bookkeeping
table, and no risk of a retry re-congratulating someone.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Thresholds are deliberately sparse. A notification for every session is noise, and
# noise is what makes people turn notifications off entirely.
STREAK_MILESTONES = (3, 7, 14, 30, 60, 100, 365)
SESSION_MILESTONES = (1, 10, 25, 50, 100, 250, 500, 1000)


def _streak_variant(days: int) -> tuple:
    """The `progress_milestone` variant for a streak of `days`, and its params."""
    if days >= 365:
        return "streak_year", {"days": days}
    if days >= 100:
        return "streak_100", {"days": days}
    if days >= 30:
        return "streak_30", {"days": days}
    return "streak", {"days": days}


def _session_variant(count: int) -> tuple:
    """The `progress_milestone` variant for `count` workouts, and its params."""
    if count == 1:
        return "sessions_first", {"count": count}
    if count >= 500:
        return "sessions_500", {"count": count}
    if count >= 100:
        return "sessions_100", {"count": count}
    return "sessions", {"count": count}


def _say(variant: str, params: dict) -> str:
    """The variant's sentence in the ACTIVE language. The sentences live on the event's
    template (`ProgressMilestoneEvent.template.variants`), the one place the inbox and a
    push render them from (Road to 10, notification language P1)."""
    from notifications.domain.routine_events import ProgressMilestoneEvent

    return str(ProgressMilestoneEvent.template.variants[variant]) % params


def reached(user) -> list:
    """The milestones this user has reached, as (milestone_id, variant, params).

    Every crossed milestone rather than only the newest: dedup makes resends free, and a
    user whose first sync lands several at once should hear about all of them rather
    than have the platform quietly pick one.
    """
    from routine.models import WorkoutSession

    found = []

    try:
        from achievements.engine import AchievementEngine

        streak = AchievementEngine._calculate_workout_streak(user)
    except Exception:
        # A milestone is a nicety; never let it break the request that completed a
        # workout. Logged rather than swallowed so a broken streak calc is visible.
        logger.warning("streak calculation failed for user %s", user.pk, exc_info=True)
        streak = 0

    for threshold in STREAK_MILESTONES:
        if streak >= threshold:
            found.append((f"streak-{threshold}", *_streak_variant(threshold)))

    sessions = WorkoutSession.objects.filter(user=user, status="completed").count()
    for threshold in SESSION_MILESTONES:
        if sessions >= threshold:
            found.append((f"sessions-{threshold}", *_session_variant(threshold)))

    return found


def evaluate(user) -> list:
    """The milestones this user has reached, as (milestone_id, message), the message in
    the active language."""
    return [(milestone_id, _say(variant, params))
            for milestone_id, variant, params in reached(user)]


def award(user) -> int:
    """Send a `progress_milestone` notification for each newly reached milestone.

    Returns how many were actually delivered — dedup silently drops the ones this user
    has already been told about, which is every one of them after the first run.
    """
    from notifications.services import NotificationService

    from training_platform.i18n import LanguageContext

    sent = 0
    # The row names its variant and params, so the inbox renders it in the READING
    # language and a push in the saved one (Road to 10, notification language P1).
    # `message` is the same sentence in the recipient's saved language, rendered here:
    # what a reader without the variant falls back to (a row read by older code). On
    # the worker without this context it would be LANGUAGE_CODE's.
    milestones_reached = reached(user)
    with LanguageContext.for_user_id(user.pk):
        said = [_say(variant, params) for _id, variant, params in milestones_reached]

    for (milestone_id, variant, params), message in zip(milestones_reached, said):
        result = NotificationService.create_and_send(
            recipient=user,
            event_type="progress_milestone",
            related_object_id=milestone_id,
            metadata={
                "context": {"variant": variant, **params, "message": message},
                "data": {
                    "type": "progress_milestone",
                    "milestone": milestone_id,
                },
            },
        )
        if result is not None:
            sent += 1
    return sent
