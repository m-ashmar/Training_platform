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
class CoachingPaymentDueEvent(BaseDomainEvent):
    """The month ended and the wallet could not cover the next one."""
    template = NotificationTemplate(
        title=_("Top up to keep your coaching"),
        body=_("Your wallet could not cover the next month with %(name)s. "
               "Top up within %(days)s days to keep your trainer."),
    )
    actor_id: int = 0          # the trainer
    recipient_id: int = 0      # the client
    trainer_name: str = ""
    days: int = 0
    period_id: int = 0


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
