"""
notifications/domain/routine_events.py — routine/training domain events.

routine.services.send_notification() → routine.tasks.send_async_notification →
NotificationService.create_and_send(event_type=<one of these>).

These event classes exist primarily to carry a NotificationTemplate so the FCM
delivery boundary can render a meaningful, translated title/body. Without a
template registered for the event type, NotificationTemplateResolver falls back to
a generic "You have a new notification." and the routine message is lost.

The caller supplies the human-readable text as metadata['context']['message'],
which these templates interpolate via %(message)s.

**Variants** (Road to 10, notification language P1, 2026-11-24). Four of them also
name the sentence they say, and its params, in the context — `{"variant": key, <params>,
"message": <the sentence in the recipient's saved language, as before>}` — and the body
is rendered from `template.variants[key]` in the ACTIVE language: the request's in the
inbox (`Accept-Language`), the saved one in a push. So an inbox read in Arabic reads
Arabic whatever language the row was written in. A row with no variant (written before,
or `custom`: the trainer's own words) reads its stored `message`. The variant bodies are
the msgids the writers already used, so the catalogue's translations serve both.
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class RoutineAssignmentEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Routine Assigned"),
        body=_("%(message)s"),
        # `routine`: the routine's name (`routine/services.py notify_assignment`).
        variants={
            "assigned": _("A new routine, %(routine)s, has been assigned to you."),
        },
    )
    user_id: int = 0
    routine_id: int = 0


@dataclass
class SessionReminderEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Workout Reminder"),
        body=_("%(message)s"),
        # `notifications/tasks.py send_workout_reminders`: `day` and `routine` for a
        # scheduled day, `days` since the last workout for a nudge.
        variants={
            "scheduled": _("Day %(day)d of %(routine)s is waiting for you."),
            "yesterday": _("You trained yesterday. Keep the streak alive."),
            "days": _("It has been %(days)d days since your last workout."),
            "days_long": _(
                "It has been %(days)d days. Even a short session gets you moving again."),
        },
    )
    user_id: int = 0
    session_id: int = 0


@dataclass
class SessionCompletedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Workout Completed"),
        body=_("%(message)s"),
        # `routine/views.py _completed`: `routine` for the client, and `name` (the
        # client's) with it for their trainer.
        variants={
            "client": _("Well done! You finished a workout on %(routine)s."),
            "trainer": _("%(name)s finished a workout on %(routine)s."),
        },
    )
    user_id: int = 0
    session_id: int = 0


@dataclass
class ProgressMilestoneEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Milestone Reached 🎯"),
        body=_("%(message)s"),
        # `notifications/milestones.py`: `days` in a row, or `count` workouts.
        variants={
            "streak_year": _("A full year of training. %(days)d days in a row."),
            "streak_100": _("%(days)d days without missing one. Remarkable."),
            "streak_30": _("%(days)d-day streak — this is a habit now."),
            "streak": _("%(days)d days in a row. Keep it going."),
            "sessions_first": _("First workout complete. The hardest one is behind you."),
            "sessions_500": _("%(count)d workouts completed. That is a training career."),
            "sessions_100": _("%(count)d workouts completed."),
            "sessions": _("%(count)d workouts done. Momentum is building."),
        },
    )
    user_id: int = 0
    milestone: str = ""


@dataclass
class CustomRoutineNotificationEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Notification"),
        body=_("%(message)s"),
    )
    user_id: int = 0
