"""
notifications/domain/trainer_client_events.py — Domain events for trainer-client lifecycle.

Each event carries:
  - A typed NotificationTemplate (lazy-translated title/body)
  - Primitive context fields for serialization safety
  - No pre-evaluated English strings

Templates are evaluated at the FCM delivery boundary inside
LanguageContext.for_user_id(recipient_id).
"""

from dataclasses import dataclass, field
from django.utils.translation import gettext_lazy as _

from notifications.domain.events import BaseDomainEvent, NotificationTemplate


@dataclass
class TrainerAssignmentRequestEvent(BaseDomainEvent):
    """Trainer requests to assign a client."""
    template = NotificationTemplate(
        title=_("Trainer Assignment Request"),
        body=_("Trainer %(name)s has requested to assign you as a client."),
    )
    actor_id: int = 0
    recipient_id: int = 0
    trainer_name: str = ""
    #: The invitation's id (the relation), what `client/respond-to-invite/` takes as
    #: `request_id` (Road to 10 S3.1). 0 on an event queued before the field existed.
    request_id: int = 0


@dataclass
class TrainerUnassignmentEvent(BaseDomainEvent):
    """Trainer unassigns a client."""
    template = NotificationTemplate(
        title=_("Trainer Unassignment"),
        body=_("Trainer %(name)s has unassigned you."),
    )
    actor_id: int = 0
    recipient_id: int = 0
    trainer_name: str = ""


@dataclass
class ClientRequestReceivedEvent(BaseDomainEvent):
    """Client sends a request to join a trainer."""
    template = NotificationTemplate(
        title=_("New Client Request"),
        body=_("%(name)s has requested to join your training."),
    )
    actor_id: int = 0
    recipient_id: int = 0
    client_name: str = ""
    #: The request's id (the relation), what `trainer/respond-to-request/` takes as
    #: `request_id` (Road to 10 S3.1). 0 on an event queued before the field existed.
    request_id: int = 0


@dataclass
class ClientRequestApprovedEvent(BaseDomainEvent):
    """Trainer approves a client's request."""
    template = NotificationTemplate(
        title=_("Request Approved"),
        body=_("Your request to trainer %(name)s has been approved."),
    )
    actor_id: int = 0
    recipient_id: int = 0
    trainer_name: str = ""


@dataclass
class ClientRequestRejectedEvent(BaseDomainEvent):
    """Trainer rejects a client's request."""
    template = NotificationTemplate(
        title=_("Request Rejected"),
        body=_("Your request to trainer %(name)s has been rejected."),
    )
    actor_id: int = 0
    recipient_id: int = 0
    trainer_name: str = ""


@dataclass
class ClientRequestCancelledEvent(BaseDomainEvent):
    """Client cancels their pending request to a trainer."""
    template = NotificationTemplate(
        title=_("Request Cancelled"),
        body=_("%(name)s has cancelled their trainer request."),
    )
    actor_id: int = 0
    recipient_id: int = 0
    client_name: str = ""


@dataclass
class ClientUnassignedTrainerEvent(BaseDomainEvent):
    """Client unassigns themselves from a trainer."""
    template = NotificationTemplate(
        title=_("Client Unassigned"),
        body=_("%(name)s has unassigned from your training."),
    )
    actor_id: int = 0
    recipient_id: int = 0
    client_name: str = ""


# --- monthly coaching (users/coaching.py). No amounts in the text: a push reaches every
# build, and a store build may not show a coaching price.
@dataclass
class CoachingRenewedEvent(BaseDomainEvent):
    """A new month of coaching was charged from the client's wallet."""
    template = NotificationTemplate(
        title=_("Coaching renewed"),
        body=_("Your coaching with %(name)s continues for another month."),
    )
    actor_id: int = 0          # the trainer
    recipient_id: int = 0      # the client
    trainer_name: str = ""
    period_id: int = 0


@dataclass
class CoachingRenewalSoonEvent(BaseDomainEvent):
    """The paid month ends in a few days (COACHING_RENEWAL_REMINDER_DAYS). Sent to every
    client, whether or not the wallet covers the next month (the owner, 2026-09-19)."""
    template = NotificationTemplate(
        title=_("Your coaching renews soon"),
        body=_("Your month of coaching with %(name)s ends in %(days)s days."),
    )
    actor_id: int = 0          # the trainer
    recipient_id: int = 0      # the client
    trainer_name: str = ""
    days: int = 0
    period_id: int = 0


@dataclass
class CoachingEndingSoonEvent(BaseDomainEvent):
    """The same reminder for a client who asked not to renew after this month (Road to
    10 S2.7, the owner's D12): the paid month ends in a few days and the coaching ends
    with it; nothing is charged. Sent once per month, instead of
    `CoachingRenewalSoonEvent`, never beside it."""
    template = NotificationTemplate(
        title=_("Your coaching ends soon"),
        body=_("Your month of coaching with %(name)s ends in %(days)s days, and it will "
               "not renew. Nothing more will be charged."),
    )
    actor_id: int = 0          # the trainer
    recipient_id: int = 0      # the client
    trainer_name: str = ""
    days: int = 0
    period_id: int = 0


@dataclass
class CoachingPaymentDueEvent(BaseDomainEvent):
    """The month ended and the wallet could not cover the next one: the coaching is
    paused (frozen). Sent when it freezes (`step` 0) and again on each step of
    COACHING_FROZEN_REMINDER_DAYS while it stays paused. States the fact only: no amount,
    no link, the same on every build."""
    template = NotificationTemplate(
        title=_("Your coaching is paused"),
        body=_("Your coaching with %(name)s is paused until the next month is paid. "
               "You can still see your routines."),
    )
    actor_id: int = 0          # the trainer
    recipient_id: int = 0      # the client
    trainer_name: str = ""
    days: int = 0              # unused since the freeze; kept for queued events
    period_id: int = 0
    step: int = 0


@dataclass
class CoachingEndedEvent(BaseDomainEvent):
    """The grace period ran out unpaid, so the coaching ended. Sent to both people."""
    template = NotificationTemplate(
        title=_("Coaching ended"),
        body=_("Coaching between you and %(name)s has ended: the monthly fee was not paid."),
    )
    actor_id: int = 0          # the other person
    recipient_id: int = 0
    other_name: str = ""
    relation_id: int = 0
    #: Both sides by role (Road to 10 S3.1), so each page opens without guessing which
    #: of the two the recipient is. 0 on an event queued before the fields existed.
    client_id: int = 0
    trainer_id: int = 0


@dataclass
class CoachingFinishedEvent(BaseDomainEvent):
    """The coaching ended for a reason other than money: the trainer stopped at the end
    of the month, or an account was suspended. Sent to both people."""
    template = NotificationTemplate(
        title=_("Coaching ended"),
        body=_("Coaching between you and %(name)s has ended."),
    )
    actor_id: int = 0          # the other person
    recipient_id: int = 0
    other_name: str = ""
    relation_id: int = 0
    #: As `CoachingEndedEvent` (Road to 10 S3.1).
    client_id: int = 0
    trainer_id: int = 0
