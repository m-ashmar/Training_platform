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
