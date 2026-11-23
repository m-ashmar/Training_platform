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
    CoachingEndedEvent,
    CoachingEndingSoonEvent,
    CoachingFinishedEvent,
    CoachingPaymentDueEvent,
    CoachingRenewalSoonEvent,
    CoachingRenewedEvent,
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
            # `request_id` (Road to 10 S3.1): the invitation, what the answer route takes.
            'data': {'type': 'trainer_assignment_request', 'trainer_id': event.actor_id,
                     'request_id': event.request_id},
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
        # RAISED. A listener that logs and returns tells the dispatcher it
        # succeeded, so the retry and the dead-letter write in
        # `process_event_task` were unreachable and a transient database error
        # silently deleted the notification. The `DoesNotExist` branch above
        # stays a swallow: a deleted user is nothing to retry.
        raise


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
        # RAISED. A listener that logs and returns tells the dispatcher it
        # succeeded, so the retry and the dead-letter write in
        # `process_event_task` were unreachable and a transient database error
        # silently deleted the notification. The `DoesNotExist` branch above
        # stays a swallow: a deleted user is nothing to retry.
        raise


@subscribe(ClientRequestReceivedEvent)
def handle_client_request_received(event: ClientRequestReceivedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.client_name},
            # `request_id` (Road to 10 S3.1): the request, what the answer route takes.
            'data': {'type': 'client_request_received', 'client_id': event.actor_id,
                     'request_id': event.request_id},
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
        # RAISED. A listener that logs and returns tells the dispatcher it
        # succeeded, so the retry and the dead-letter write in
        # `process_event_task` were unreachable and a transient database error
        # silently deleted the notification. The `DoesNotExist` branch above
        # stays a swallow: a deleted user is nothing to retry.
        raise


def _answer_ids(event_type, event):
    """The `data` of an answer to a coaching request: the relation's trainer and client,
    whichever of them answered (Road to 10 S3.1, the phase-3 review). An event queued
    before the two fields existed carries its actor as `trainer_id`, as it did then,
    and `client_id` 0."""
    return {'type': event_type,
            'trainer_id': event.trainer_id or event.actor_id,
            'client_id': event.client_id}


@subscribe(ClientRequestApprovedEvent)
def handle_client_request_approved(event: ClientRequestApprovedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.answered_by_name or event.trainer_name},
            'data': _answer_ids('client_request_approved', event),
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
        # RAISED. A listener that logs and returns tells the dispatcher it
        # succeeded, so the retry and the dead-letter write in
        # `process_event_task` were unreachable and a transient database error
        # silently deleted the notification. The `DoesNotExist` branch above
        # stays a swallow: a deleted user is nothing to retry.
        raise


@subscribe(ClientRequestRejectedEvent)
def handle_client_request_rejected(event: ClientRequestRejectedEvent):
    try:
        actor = User.objects.get(id=event.actor_id)
        recipient = User.objects.get(id=event.recipient_id)

        metadata = {
            'context': {'name': event.answered_by_name or event.trainer_name},
            'data': _answer_ids('client_request_rejected', event),
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
        # RAISED. A listener that logs and returns tells the dispatcher it
        # succeeded, so the retry and the dead-letter write in
        # `process_event_task` were unreachable and a transient database error
        # silently deleted the notification. The `DoesNotExist` branch above
        # stays a swallow: a deleted user is nothing to retry.
        raise


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
        # RAISED. A listener that logs and returns tells the dispatcher it
        # succeeded, so the retry and the dead-letter write in
        # `process_event_task` were unreachable and a transient database error
        # silently deleted the notification. The `DoesNotExist` branch above
        # stays a swallow: a deleted user is nothing to retry.
        raise


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
        # RAISED. A listener that logs and returns tells the dispatcher it
        # succeeded, so the retry and the dead-letter write in
        # `process_event_task` were unreachable and a transient database error
        # silently deleted the notification. The `DoesNotExist` branch above
        # stays a swallow: a deleted user is nothing to retry.
        raise


def _send_coaching(event, *, event_type, related_id, context, data):
    """One body for the three coaching notices; raises for a retry like the others."""
    try:
        actor = User.objects.filter(id=event.actor_id).first()
        recipient = User.objects.get(id=event.recipient_id)
        NotificationService.create_and_send(
            recipient=recipient, actor=actor, event_type=event_type,
            related_object_id=str(related_id),
            metadata={'context': context, 'data': data}, event_id=event.event_id)
    except User.DoesNotExist:
        logger.warning("User not found for %s: %s", event_type, event.recipient_id)
    except Exception as e:
        logger.error("Error handling %s: %s", event_type, e, exc_info=True)
        raise


@subscribe(CoachingRenewedEvent)
def handle_coaching_renewed(event: CoachingRenewedEvent):
    _send_coaching(event, event_type='coaching_renewed', related_id=event.period_id,
                   context={'name': event.trainer_name},
                   data={'type': 'coaching_renewed', 'trainer_id': event.actor_id})


@subscribe(CoachingRenewalSoonEvent)
def handle_coaching_renewal_soon(event: CoachingRenewalSoonEvent):
    _send_coaching(event, event_type='coaching_renewal_soon', related_id=event.period_id,
                   context={'name': event.trainer_name, 'days': event.days},
                   data={'type': 'coaching_renewal_soon', 'trainer_id': event.actor_id})


@subscribe(CoachingEndingSoonEvent)
def handle_coaching_ending_soon(event: CoachingEndingSoonEvent):
    _send_coaching(event, event_type='coaching_ending_soon', related_id=event.period_id,
                   context={'name': event.trainer_name, 'days': event.days},
                   data={'type': 'coaching_ending_soon', 'trainer_id': event.actor_id})


@subscribe(CoachingPaymentDueEvent)
def handle_coaching_payment_due(event: CoachingPaymentDueEvent):
    # One notice per step: the store deduplicates on the related id for good, so a
    # reminder keyed on the period alone would be dropped after the first.
    related = event.period_id if not event.step else f"{event.period_id}:{event.step}"
    _send_coaching(event, event_type='coaching_payment_due', related_id=related,
                   context={'name': event.trainer_name, 'days': event.days},
                   data={'type': 'coaching_payment_due', 'trainer_id': event.actor_id,
                         'status': 'frozen'})


@subscribe(CoachingEndedEvent)
def handle_coaching_ended(event: CoachingEndedEvent):
    _send_coaching(event, event_type='coaching_ended', related_id=event.relation_id,
                   context={'name': event.other_name},
                   # `client_id` / `trainer_id` beside `user_id` (Road to 10 S3.1).
                   data={'type': 'coaching_ended', 'user_id': event.actor_id,
                         'client_id': event.client_id, 'trainer_id': event.trainer_id})


@subscribe(CoachingFinishedEvent)
def handle_coaching_finished(event: CoachingFinishedEvent):
    _send_coaching(event, event_type='coaching_finished', related_id=event.relation_id,
                   context={'name': event.other_name},
                   data={'type': 'coaching_finished', 'user_id': event.actor_id,
                         'client_id': event.client_id, 'trainer_id': event.trainer_id})
