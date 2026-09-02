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
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class RoutineAssignmentEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Routine Assigned"),
        body=_("%(message)s"),
    )
    user_id: int = 0
    routine_id: int = 0


@dataclass
class SessionReminderEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Workout Reminder"),
        body=_("%(message)s"),
    )
    user_id: int = 0
    session_id: int = 0


@dataclass
class SessionCompletedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Workout Completed"),
        body=_("%(message)s"),
    )
    user_id: int = 0
    session_id: int = 0


@dataclass
class ProgressMilestoneEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Milestone Reached 🎯"),
        body=_("%(message)s"),
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
