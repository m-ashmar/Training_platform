"""Handlers for the verification decisions (`notifications/domain/verification_events.py`).

Context only, never a rendered sentence: the template is resolved at delivery inside the
trainer's language (`notifications/channels/fcm.py`). `data.type` is the `event_type`
verbatim, like every other push, and `related_object_id` is the application, so a tap
opens the verification screen. Deduplicated per emission (`event_id`): a second
rejection months later is a new notice, not a duplicate of the first.

Raised on failure, like the other listeners: a swallowed error would tell the
dispatcher it succeeded and the retry and dead-letter write would be unreachable. A
missing user (an account erased before the worker ran) is the one failure discarded.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.domain.dispatcher import subscribe
from notifications.domain.verification_events import (
    TrainerVerificationApprovedEvent,
    TrainerVerificationRejectedEvent,
)
from notifications.services import NotificationService

logger = logging.getLogger(__name__)
User = get_user_model()


@subscribe(TrainerVerificationApprovedEvent)
def handle_trainer_verification_approved(event: TrainerVerificationApprovedEvent):
    try:
        recipient = User.objects.get(id=event.recipient_id)
        NotificationService.create_and_send(
            recipient=recipient,
            event_type='trainer_verification_approved',
            related_object_id=str(event.application_id),
            metadata={
                'context': {},
                'data': {'type': 'trainer_verification_approved',
                         'application_id': str(event.application_id)},
            },
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning("User not found for TrainerVerificationApprovedEvent: %s",
                       event.recipient_id)
    except Exception as e:
        logger.error("Error handling TrainerVerificationApprovedEvent: %s", e, exc_info=True)
        raise


@subscribe(TrainerVerificationRejectedEvent)
def handle_trainer_verification_rejected(event: TrainerVerificationRejectedEvent):
    try:
        recipient = User.objects.get(id=event.recipient_id)
        NotificationService.create_and_send(
            recipient=recipient,
            event_type='trainer_verification_rejected',
            related_object_id=str(event.application_id),
            metadata={
                'context': {'reason': event.reason},
                'data': {'type': 'trainer_verification_rejected',
                         'application_id': str(event.application_id),
                         'reason': event.reason},
            },
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning("User not found for TrainerVerificationRejectedEvent: %s",
                       event.recipient_id)
    except Exception as e:
        logger.error("Error handling TrainerVerificationRejectedEvent: %s", e, exc_info=True)
        raise
