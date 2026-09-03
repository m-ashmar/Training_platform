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

from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

# Thresholds are deliberately sparse. A notification for every session is noise, and
# noise is what makes people turn notifications off entirely.
STREAK_MILESTONES = (3, 7, 14, 30, 60, 100, 365)
SESSION_MILESTONES = (1, 10, 25, 50, 100, 250, 500, 1000)


def _streak_message(days: int) -> str:
    if days >= 365:
        return _("A full year of training. %(days)d days in a row.") % {"days": days}
    if days >= 100:
        return _("%(days)d days without missing one. Remarkable.") % {"days": days}
    if days >= 30:
        return _("%(days)d-day streak — this is a habit now.") % {"days": days}
    return _("%(days)d days in a row. Keep it going.") % {"days": days}


def _session_message(count: int) -> str:
    if count == 1:
        return _("First workout complete. The hardest one is behind you.")
    if count >= 500:
        return _("%(count)d workouts completed. That is a training career.") % {"count": count}
    if count >= 100:
        return _("%(count)d workouts completed.") % {"count": count}
    return _("%(count)d workouts done. Momentum is building.") % {"count": count}


def evaluate(user) -> list:
    """Return the milestones this user has just reached, as (milestone_id, message).

    Returns every crossed milestone rather than only the newest: dedup makes resends
    free, and a user whose first sync lands several at once should hear about all of
    them rather than have the platform quietly pick one.
    """
    from routine.models import WorkoutSession

    reached = []

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
            reached.append((f"streak-{threshold}", _streak_message(threshold)))

    sessions = WorkoutSession.objects.filter(user=user, status="completed").count()
    for threshold in SESSION_MILESTONES:
        if sessions >= threshold:
            reached.append((f"sessions-{threshold}", _session_message(threshold)))

    return reached


def award(user) -> int:
    """Send a `progress_milestone` notification for each newly reached milestone.

    Returns how many were actually delivered — dedup silently drops the ones this user
    has already been told about, which is every one of them after the first run.
    """
    from notifications.services import NotificationService

    from training_platform.i18n import LanguageContext

    sent = 0
    # Rendered here, inside the recipient's language. `evaluate` builds lazy strings;
    # resolving them on the worker without this context would use LANGUAGE_CODE, and
    # the sentence would arrive in English inside an Arabic notification because the
    # FCM boundary translates the template around it, not the text handed to it.
    with LanguageContext.for_user_id(user.pk):
        milestones_reached = [(mid, str(msg)) for mid, msg in evaluate(user)]

    for milestone_id, message in milestones_reached:
        result = NotificationService.create_and_send(
            recipient=user,
            event_type="progress_milestone",
            related_object_id=milestone_id,
            metadata={
                "context": {"message": message},
                "data": {
                    "type": "progress_milestone",
                    "milestone": milestone_id,
                },
            },
        )
        if result is not None:
            sent += 1
    return sent
