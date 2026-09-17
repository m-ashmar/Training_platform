from celery import shared_task
from django.contrib.auth import get_user_model
import logging
from django.db import OperationalError, InterfaceError

# Transient failures worth retrying. These tasks previously used a bare @shared_task:
# no bind, no autoretry, no self.retry() — so ANY exception (a DB blip, an FCM 503, a
# broker hiccup) lost the job permanently and silently. `autoretry_for` gives them a
# retry policy without changing any signature; permanent errors still fail fast.
TRANSIENT_ERRORS = (
    OperationalError,
    InterfaceError,
    ConnectionError,
    TimeoutError,
)


logger = logging.getLogger(__name__)


@shared_task(
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def send_async_notification(user_id, notif_type, message, related_object_id=None,
                            related_object_type=None, event_id=None):
    """
    Asynchronously create a routine notification.

    Writes to the CANONICAL notifications.Notification store via NotificationService
    so the notification is readable at /api/social/notifications/ and is dispatched
    to FCM. Previously this wrote to the legacy routine.Notification table, which no
    API endpoint reads — those notifications were invisible to users.

    Errors PROPAGATE. `create_and_send` raises instead of returning None precisely so a
    database error reaches a retry, and a catch-all here undid that: the task logged a
    line, reported SUCCESS, and the notification was gone. A missing user is the one
    permanent failure worth discarding.
    """
    from notifications.services import NotificationService

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error(f"Failed to send notification: User {user_id} not found")
        return
    metadata = {
        # Pre-rendered message kept under context so the FCM template resolver
        # can fall back to it when no template exists for this event type.
        'context': {'message': message},
        'data': {
            'type': notif_type,
            'related_object_id': str(related_object_id) if related_object_id is not None else None,
            'related_object_type': related_object_type,
        },
    }
    NotificationService.create_and_send(
        recipient=user,
        event_type=notif_type,
        related_object_id=str(related_object_id) if related_object_id is not None else '',
        metadata=metadata,
        event_id=event_id,
    )
    logger.info(f"Notification sent to user {user_id} (type: {notif_type})")
