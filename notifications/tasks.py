import logging
import importlib
from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def process_event_task(self, event_path: str, event_data: dict):
    """
    Async task to process domain events.
    Deserializes the event and passes it to the dispatcher.
    """
    from notifications.domain.dispatcher import EventDispatcher
    
    # A malformed path is a permanent failure: retrying it 3x with backoff only
    # delays the inevitable and burns worker slots.
    if not event_path or '.' not in event_path:
        logger.error("Discarding event with malformed path: %r", event_path)
        return

    # Resolving the event class is separate from dispatching it: an unknown module or
    # class can never succeed on a retry, so it is discarded rather than re-queued.
    try:
        module_name, class_name = event_path.rsplit('.', 1)
        module = importlib.import_module(module_name)
        event_class = getattr(module, class_name)
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError) as e:
        logger.error("Discarding event with unresolvable path %r: %s", event_path, e)
        return

    try:
        
        # reconstruct event
        event = event_class.from_dict(event_data)
        
        logger.info(f"Async processing event: {class_name}")
        EventDispatcher.dispatch(event)
        
    except Exception as e:
        logger.error(f"Failed to process event {event_path}: {e}", exc_info=True)
        try:
            # Exponential backoff
            countdown = 5 * (2 ** self.request.retries)
            raise self.retry(exc=e, countdown=countdown)
        except self.MaxRetriesExceededError:
            logger.critical(f"Max retries exceeded for event {event_path}. Moving to DLQ.")
            # Avoid circular import
            try:
                from notifications.models import NotificationFailure
                import traceback
                NotificationFailure.objects.create(
                    event_type=event_path,
                    event_payload=event_data,
                    error_message=str(e),
                    stack_trace=traceback.format_exc(),
                    retry_count=self.request.retries
                )
            except Exception as dlq_error:
                 logger.critical(f"Failed to write to DLQ: {dlq_error}")


@shared_task(
    name="notifications.drain_dead_letter_queue",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def drain_dead_letter_queue():
    """Hourly: replay what failed, and shout if the queue is growing.

    The DLQ was write-only — entries accumulated and nobody was told. This both retries
    them and makes an unresolved backlog visible in the logs rather than only in an
    admin page someone has to remember to open.
    """
    from django.core.management import call_command

    from notifications.models import NotificationFailure

    pending = NotificationFailure.objects.filter(is_resolved=False).count()
    if pending:
        logger.warning("Notification dead-letter queue holds %s unresolved entries", pending)
        call_command("retry_failed_notifications", limit=100, verbosity=0)

    stuck = NotificationFailure.objects.filter(is_resolved=False, retry_count__gte=5).count()
    if stuck:
        logger.critical(
            "%s notification(s) have exhausted retries and need a human", stuck
        )
    return f"pending={pending} stuck={stuck}"


@shared_task(name="notifications.award_progress_milestones")
def award_progress_milestones(user_id: int):
    """Evaluate and send milestone notifications for one user.

    Runs on the worker rather than inline: it counts sessions and walks backwards
    through activity days to compute the streak, and none of that belongs in the
    request that just finished a workout.
    """
    from django.contrib.auth import get_user_model

    from notifications import milestones

    try:
        user = get_user_model().objects.get(pk=user_id)
    except get_user_model().DoesNotExist:
        logger.warning("milestone check for missing user %s", user_id)
        return 0

    sent = milestones.award(user)
    if sent:
        logger.info("awarded %d milestone notification(s) to user %s", sent, user_id)
    return sent


@shared_task(name="notifications.send_workout_reminders")
def send_workout_reminders():
    """Nudge clients who have a routine assigned but have not trained today.

    `session_reminder` was registered and templated but never emitted, so the platform
    had no retention loop at all — a user who drifted for a week heard nothing.

    There is deliberately no "your session is at 18:00" variant: WorkoutSession records
    `start_time` (when a session actually began) and has no scheduled-for field, so a
    time-of-day reminder would be inventing data the platform does not hold.

    Who gets one: an active client with at least one assigned routine, who has trained
    at least once before (so it is a nudge, not a cold-start pester), whose last
    workout was between 1 and 14 days ago. Past 14 days this stops rather than
    following someone into their inbox forever.

    Idempotency comes from the dedup key — related_object_id is today's date, so a
    second run on the same day, or a retry, sends nothing.
    """
    from datetime import timedelta

    from django.contrib.auth import get_user_model
    from django.db.models import Max
    from django.utils import timezone
    from django.utils.translation import gettext as _

    from notifications.services import NotificationService
    from routine.models import WorkoutSession

    today = timezone.localdate()
    User = get_user_model()

    candidates = (
        User.objects.filter(
            is_active=True,
            user_type="client",
            assigned_routines__isnull=False,
        )
        .annotate(last_workout=Max("workout_sessions__start_time"))
        .filter(last_workout__isnull=False)
        .distinct()
    )

    sent = 0
    for user in candidates.iterator(chunk_size=500):
        last = timezone.localtime(user.last_workout).date()
        gap = (today - last).days
        if not (1 <= gap <= 14):
            continue

        if gap == 1:
            message = _("You trained yesterday. Keep the streak alive.")
        elif gap <= 3:
            message = _("It has been %(days)d days since your last workout.") % {"days": gap}
        else:
            message = _(
                "It has been %(days)d days. Even a short session gets you moving again."
            ) % {"days": gap}

        result = NotificationService.create_and_send(
            recipient=user,
            event_type="session_reminder",
            related_object_id=today.isoformat(),
            metadata={
                "context": {"message": message},
                "data": {"type": "session_reminder", "days_since_last_workout": gap},
            },
        )
        if result is not None:
            sent += 1

    logger.info("workout reminders sent: %d", sent)
    return sent
