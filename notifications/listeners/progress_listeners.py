"""Handlers for the progress events (routine/strength.py, routine/tasks.py).

Context only, never a rendered sentence: the template is resolved at delivery inside the
recipient's language (notifications/channels/fcm.py). The `type` in the FCM `data`
payload is the `event_type` verbatim, as every other listener here does.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.domain.dispatcher import subscribe
from notifications.domain.progress_events import PersonalRecordEvent
from notifications.services import NotificationService

logger = logging.getLogger(__name__)
User = get_user_model()


@subscribe(PersonalRecordEvent)
def handle_personal_record(event: PersonalRecordEvent):
    """"You beat your best" — keyed on the SET, so a retry of the same set never
    celebrates twice, and two different records on one day are two pieces of news.

    Raised on failure like the other listeners: a swallowed error would tell the
    dispatcher it succeeded and the retry and dead-letter write would be unreachable.
    A missing user is the one permanent failure worth discarding.
    """
    try:
        recipient = User.objects.get(id=event.recipient_id)
        NotificationService.create_and_send(
            recipient=recipient,
            event_type='personal_record',
            related_object_id=str(event.set_log_id),
            metadata={
                'context': {'exercise': event.exercise_name, 'weight': event.weight,
                            'reps': event.reps},
                'data': {'type': 'personal_record', 'exercise_id': str(event.exercise_id),
                         'set_log_id': str(event.set_log_id)},
            },
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning("User not found for PersonalRecordEvent: %s", event.recipient_id)
    except Exception as e:
        logger.error("Error handling PersonalRecordEvent: %s", e, exc_info=True)
        raise
