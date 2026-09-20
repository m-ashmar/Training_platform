"""What the app tells a person about their own progress (the owner, 2026-09-20).

Three notices, all fact-only: a personal best the moment it happens, a weekly recap for
the client, and a weekly digest for the trainer. No amount of money, no link, no call to
buy, and no health claim (no calories, no weight, no muscle) — these reach every build,
and a store build must be able to show any of them.

The words live in the TEMPLATE, so each recipient reads them in their own language at
delivery; only the numbers travel as context.
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class PersonalRecordEvent(BaseDomainEvent):
    """A set that beats everything this person has logged for that exercise."""
    template = NotificationTemplate(
        title=_("New personal best"),
        body=_("%(exercise)s: %(weight)s kg x %(reps)s — your best yet."),
    )
    recipient_id: int = 0
    exercise_id: int = 0
    exercise_name: str = ""
    weight: str = ""
    reps: int = 0
    set_log_id: int = 0


@dataclass
class WeeklyRecapEvent(BaseDomainEvent):
    """The client's own week: what the plan asked for and what was done."""
    template = NotificationTemplate(
        title=_("Your week"),
        body=_("%(logged)s of %(planned)s sets done, %(perfect)s day(s) with nothing "
               "missed."),
    )
    recipient_id: int = 0
    logged: int = 0
    planned: int = 0
    perfect: int = 0
    week: str = ""


@dataclass
class TrainerWeeklyDigestEvent(BaseDomainEvent):
    """The trainer's week: who trained, and who needs a look."""
    template = NotificationTemplate(
        title=_("Your clients this week"),
        body=_("%(trained)s of %(clients)s clients trained. %(attention)s need a look."),
    )
    recipient_id: int = 0
    clients: int = 0
    trained: int = 0
    attention: int = 0
    week: str = ""
