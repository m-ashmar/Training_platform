from celery import shared_task
from django.contrib.auth import get_user_model
import logging
from django.db import OperationalError, InterfaceError, models

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


#: How far back the nightly sweep reaches for days left open. One backlog window: the
#: app may sync a workout up to 30 days late (routine/views.py MAX_BACKLOG_DAYS).
SWEEP_WINDOW_DAYS = 30


@shared_task(
    name="routine.close_open_training_days",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def close_open_training_days(before=None):
    """Record what was left of training days that were opened and never closed.

    A person who starts a day, logs half of it and never taps anything leaves a
    `RoutineProgress` row at `in_progress` and no outcome, so the missed work would
    simply vanish from the week (the owner, 2026-09-20: keep it). Once the day is over,
    it is recorded as `via='unclosed'`.

    A day nobody was ALLOWED to train is not a miss: while a coaching is paused or over
    the client may read the routine and not start it, so those days are skipped
    (users/coaching.py `training_refusal`). Idempotent — a day that already has an
    outcome is left alone.
    """
    from datetime import timedelta

    from django.utils import timezone

    from users.coaching import training_refusal

    from .models import DayOutcome, RoutineProgress
    from .outcomes import record_day_quietly

    before = before or timezone.localdate()
    # A trailing window, not all of history: without it the first run would reconstruct
    # every half-finished day ever left behind, measure it against TODAY's plan and turn
    # years-old days into streak breaks.
    window_start = before - timedelta(days=SWEEP_WINDOW_DAYS)
    recorded = DayOutcome.objects.filter(
        user_id=models.OuterRef('user_id'), routine_id=models.OuterRef('routine_id'),
        day=models.OuterRef('day'), date=models.OuterRef('date'))
    open_days = (RoutineProgress.objects
                 .filter(status='in_progress', date__lt=before, date__gte=window_start)
                 .annotate(already=models.Exists(recorded))
                 .filter(already=False)
                 .select_related('user', 'routine')
                 .order_by('date', 'id')[:500])

    closed = skipped = 0
    for row in open_days:
        if training_refusal(row.user, row.routine, row.date):
            skipped += 1
            continue
        # Recorded after the fact: the plan may have changed since that day, so the
        # week that holds it is shown as approximate.
        if record_day_quietly(row.user, row.routine, row.day, row.date, via='unclosed',
                              prescription_exact=row.date >= before - timedelta(days=1)):
            closed += 1
    if closed or skipped:
        logger.info("Closed %s open training days (%s skipped: not trainable)", closed, skipped)
    return {'closed': closed, 'skipped': skipped}
