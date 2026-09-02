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
    """Remind each client about today's training, in their own evening.

    Runs hourly and sends only to users whose local hour — resolved through their own
    `preferred_timezone` — matches their `workout_reminder_hour`. A single daily sweep
    at a fixed UTC hour would reach a user in Damascus and a user in Berlin at
    different points in their day, and this app is bilingual and expects users outside
    one timezone.

    Two kinds of reminder, and the distinction matters:

      * **Scheduled** — the user has an active routine (today falls inside its
        start_date/end_date window) with days still not started. This is a real
        obligation the platform knows about, so the message names the routine.
      * **Drift** — no active routine window, but the user trained before and has been
        away 1-14 days. A nudge, not a schedule.

    Past 14 days with nothing scheduled, this stops. Following someone into their
    inbox forever is how an app gets its notifications switched off.

    Idempotency comes from the dedup key: related_object_id is the user's local date,
    so a retry, or the sweep running twice in one of their hours, sends nothing.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from django.contrib.auth import get_user_model
    from django.db.models import Max, Q
    from django.utils import timezone
    from django.utils.translation import gettext as _

    from notifications.services import NotificationService
    from routine.models import RoutineProgress

    User = get_user_model()
    now = timezone.now()

    candidates = (
        User.objects.filter(
            is_active=True,
            user_type="client",
            assigned_routines__isnull=False,
        )
        .annotate(last_workout=Max("workout_sessions__start_time"))
        .distinct()
    )

    sent = 0
    for user in candidates.iterator(chunk_size=500):
        try:
            tz = ZoneInfo(user.preferred_timezone or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            # A bad timezone string must not silence this user forever.
            logger.warning("user %s has an unusable timezone %r", user.pk, user.preferred_timezone)
            tz = ZoneInfo("UTC")

        local = now.astimezone(tz)
        if local.hour != user.workout_reminder_hour:
            continue

        today = local.date()

        # Is a routine actually scheduled for today?
        pending = (
            RoutineProgress.objects.filter(
                user=user,
                routine__is_active=True,
                routine__start_date__lte=today,
                status__in=("not_started", "in_progress"),
            )
            .filter(Q(routine__end_date__isnull=True) | Q(routine__end_date__gte=today))
            .select_related("routine")
            .order_by("day")
            .first()
        )

        if pending:
            message = _("Day %(day)d of %(routine)s is waiting for you.") % {
                "day": pending.day,
                "routine": pending.routine.name,
            }
            data = {
                "type": "session_reminder",
                "reason": "scheduled",
                "routine_id": str(pending.routine_id),
                "day": str(pending.day),
            }
        else:
            if user.last_workout is None:
                continue
            gap = (today - user.last_workout.astimezone(tz).date()).days
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
            data = {"type": "session_reminder", "reason": "drift", "days_since_last_workout": gap}

        result = NotificationService.create_and_send(
            recipient=user,
            event_type="session_reminder",
            related_object_id=today.isoformat(),
            metadata={"context": {"message": message}, "data": data},
        )
        if result is not None:
            sent += 1

    logger.info("workout reminders sent: %d", sent)
    return sent
