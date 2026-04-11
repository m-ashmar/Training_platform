"""
notifications/listeners/trainer_client_listeners.py — Handlers for trainer-client domain events.

Each listener:
  1. Resolves actor/recipient from event IDs
  2. Builds context-only metadata (no title/body strings)
  3. Creates Notification via NotificationService

Templates are resolved at delivery-time by FCMChannel inside
LanguageContext.for_user_id(recipient_id).
"""

import logging

from django.contrib.auth import get_user_model

from notifications.domain.dispatcher import subscribe
from notifications.domain.trainer_client_events import (
    TrainerAssignmentRequestEvent,
    TrainerUnassignmentEvent,
    ClientRequestReceivedEvent,
    ClientRequestApprovedEvent,
    ClientRequestRejectedEvent,
    ClientRequestCancelledEvent,
    ClientUnassignedTrainerEvent,
)
from notifications.services import NotificationService

logger = logging.getLogger(__name__)
User = get_user_model()


@subscribe(TrainerAssignmentRequestEvent)
def handle_trainer_assignment_request(event: TrainerAssignmentRequestEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.trainer_name},
            'data': {'type': 'trainer_assignment_request', 'trainer_id': event.actor_id},
        }

        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='trainer_assignment_request',
            related_object_id=str(event.actor_id),
            metadata=metadata,
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning(f"User not found for TrainerAssignmentRequestEvent: actor={event.actor_id}, recipient={event.recipient_id}")
    except Exception as e:
        logger.error(f"Error handling TrainerAssignmentRequestEvent: {e}", exc_info=True)


@subscribe(TrainerUnassignmentEvent)
def handle_trainer_unassignment(event: TrainerUnassignmentEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.trainer_name},
            'data': {'type': 'trainer_unassignment', 'trainer_id': event.actor_id},
        }

        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='trainer_unassignment',
            related_object_id=str(event.actor_id),
            metadata=metadata,
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning(f"User not found for TrainerUnassignmentEvent: {event.actor_id}")
    except Exception as e:
        logger.error(f"Error handling TrainerUnassignmentEvent: {e}", exc_info=True)


@subscribe(ClientRequestReceivedEvent)
def handle_client_request_received(event: ClientRequestReceivedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.client_name},
            'data': {'type': 'client_request_received', 'client_id': event.actor_id},
        }

        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='client_request_received',
            related_object_id=str(event.actor_id),
            metadata=metadata,
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning(f"User not found for ClientRequestReceivedEvent: {event.actor_id}")
    except Exception as e:
        logger.error(f"Error handling ClientRequestReceivedEvent: {e}", exc_info=True)


@subscribe(ClientRequestApprovedEvent)
def handle_client_request_approved(event: ClientRequestApprovedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.trainer_name},
            'data': {'type': 'client_request_approved', 'trainer_id': event.actor_id},
        }

        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='client_request_approved',
            related_object_id=str(event.actor_id),
            metadata=metadata,
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning(f"User not found for ClientRequestApprovedEvent: {event.actor_id}")
    except Exception as e:
        logger.error(f"Error handling ClientRequestApprovedEvent: {e}", exc_info=True)


@subscribe(ClientRequestRejectedEvent)
def handle_client_request_rejected(event: ClientRequestRejectedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.trainer_name},
            'data': {'type': 'client_request_rejected', 'trainer_id': event.actor_id},
        }

        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='client_request_rejected',
            related_object_id=str(event.actor_id),
            metadata=metadata,
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning(f"User not found for ClientRequestRejectedEvent: {event.actor_id}")
    except Exception as e:
        logger.error(f"Error handling ClientRequestRejectedEvent: {e}", exc_info=True)


@subscribe(ClientRequestCancelledEvent)
def handle_client_request_cancelled(event: ClientRequestCancelledEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.client_name},
            'data': {'type': 'client_request_cancelled', 'client_id': event.actor_id},
        }

        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='client_request_cancelled',
            related_object_id=str(event.actor_id),
            metadata=metadata,
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning(f"User not found for ClientRequestCancelledEvent: {event.actor_id}")
    except Exception as e:
        logger.error(f"Error handling ClientRequestCancelledEvent: {e}", exc_info=True)


@subscribe(ClientUnassignedTrainerEvent)
def handle_client_unassigned_trainer(event: ClientUnassignedTrainerEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.client_name},
            'data': {'type': 'client_unassigned_trainer', 'client_id': event.actor_id},
        }

        NotificationService.create_and_send(
            recipient=recipient,
            actor=actor,
            event_type='client_unassigned_trainer',
            related_object_id=str(event.actor_id),
            metadata=metadata,
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning(f"User not found for ClientUnassignedTrainerEvent: {event.actor_id}")
    except Exception as e:
        logger.error(f"Error handling ClientUnassignedTrainerEvent: {e}", exc_info=True)
