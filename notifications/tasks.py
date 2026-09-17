import logging
import importlib
from datetime import timedelta
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import InterfaceError, OperationalError

# Transient failures worth retrying, matching routine/, diet/ and social/. The two
# beat tasks below carried no retry policy at all, so a single database or broker blip
# dropped a whole day's workout reminders, or one user's milestone, with a log line.
TRANSIENT_ERRORS = (
    OperationalError,
    InterfaceError,
    ConnectionError,
    TimeoutError,
)


from training_platform.task_retries import no_retry_on_timeout

logger = logging.getLogger(__name__)

#: How long an undelivered push is still worth retrying, and how many attempts it gets.
PUSH_RETRY_WINDOW = timedelta(hours=24)
PUSH_MAX_ATTEMPTS = 5

@shared_task(bind=True, max_retries=3)
@no_retry_on_timeout
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

    except SoftTimeLimitExceeded:
        # An overrun is not a transient fault; `SoftTimeLimitExceeded` subclasses
        # Exception, so the handler below retried it three more times from scratch.
        raise
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
    # Not a bare `(Exception,)`: that also retries `SoftTimeLimitExceeded`, so a
    # job too big for its 300 s slot burned four consecutive slots and still
    # never finished. See training_platform/task_retries.py.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def drain_dead_letter_queue():
    """Hourly: replay what failed, and shout if the queue is growing.

    The DLQ was write-only — entries accumulated and nobody was told. This both retries
    them and makes an unresolved backlog visible in the logs rather than only in an
    admin page someone has to remember to open.
    """
    from django.core.management import call_command

    from django.db.models import Q
    from django.utils import timezone

    from notifications.channels.fcm import FCMChannel
    from notifications.models import Notification, NotificationFailure
    from notifications.services import NotificationService

    pending = NotificationFailure.objects.filter(is_resolved=False).count()
    if pending:
        logger.warning("Notification dead-letter queue holds %s unresolved entries", pending)
        call_command("retry_failed_notifications", limit=100, verbosity=0)

    # And the pushes that were never delivered because the transport was down. The
    # in-app row survives an FCM outage, so the loss is invisible unless someone greps
    # worker logs; `status.fcm.status` records it and this is the only thing that reads
    # it.
    #
    # Bounded, because the unbounded version re-selected the same rows for ever: a push
    # whose devices are all gone could never leave 'failed', and once a hundred of those
    # piled up the window held nothing else, so pushes lost to a real outage were
    # starved. Only 'failed' and 'partial' (some device still owed it) are retried, only
    # for PUSH_RETRY_WINDOW (a day-old "time to train" is noise, not a notification),
    # and only PUSH_MAX_ATTEMPTS times.
    cutoff = timezone.now() - PUSH_RETRY_WINDOW
    failed_pushes = list(
        Notification.objects.filter(
            status__fcm__status__in=sorted(FCMChannel.RETRYABLE_STATUSES),
            created_at__gte=cutoff,
        )
        # Not `.exclude(attempts__gte=N)`: a JSON key that is absent compares as NULL
        # and the exclusion dropped exactly the rows a crash recorded with no count.
        .filter(Q(status__fcm__attempts__isnull=True)
                | Q(status__fcm__attempts__lt=PUSH_MAX_ATTEMPTS))
        .order_by("created_at", "id")[:100])
    delivered, broken = 0, 0
    for notification in failed_pushes:
        # Per row. The selection is deterministic and oldest-first, so one row that
        # raises something the task's retry policy does not cover took the rest of the
        # batch with it AND the dead-letter alert below — every hour, until it aged out
        # of the window. This is the only path that re-sends a push lost to an outage.
        try:
            NotificationService._dispatch_channels(notification)
            notification.refresh_from_db(fields=["status"])
        except TRANSIENT_ERRORS:
            # The database or the broker, not this row: let the task retry.
            raise
        except Exception:
            broken += 1
            logger.exception("re-dispatch failed for notification %s", notification.pk)
            continue
        if (notification.status.get("fcm") or {}).get("status") == "sent":
            delivered += 1
    if failed_pushes:
        logger.warning("Retried %s undelivered push(es); %s now delivered, %s errored",
                       len(failed_pushes), delivered, broken)

    stuck = NotificationFailure.objects.filter(is_resolved=False, retry_count__gte=5).count()
    if stuck:
        logger.critical(
            "%s notification(s) have exhausted retries and need a human", stuck
        )
    return (f"pending={pending} stuck={stuck} retried={len(failed_pushes)} "
            f"delivered={delivered} errored={broken}")


@shared_task(
    name="notifications.award_progress_milestones",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
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


#: How many hours after a user's reminder hour a missed reminder may still be sent.
#: Long enough for the next hourly sweeps to recover a run that was cut off; short enough
#: that nobody gets "time to train" long after the moment has passed.
REMINDER_CATCH_UP_HOURS = 2


@shared_task(
    name="notifications.send_workout_reminders",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
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
        # A BOUNDED catch-up after the hour the reminder was due, measured round the
        # clock. Two failures pull against each other here:
        #
        #  * Exactly-on-the-hour missed everyone a cut-off sweep did not reach: the 300 s
        #    soft limit, an OOM or a deploy cost the rest of that hour's users their
        #    reminder for the whole day, because the next run an hour later excluded them
        #    by definition. Measured: 4 due clients, killed after 2, the next run sent 0.
        #
        #  * "At or past their hour on the same local day" fixed that and broke the other
        #    direction. It compared hours on a 0-23 line, so a user whose reminder is
        #    02:00 was messaged at 21:00 — nineteen hours after it was due, as a
        #    "reminder" for a day that is nearly over. The gate caught it only because it
        #    ran in the evening.
        #
        # So: due when the local hour is the reminder hour or up to
        # REMINDER_CATCH_UP_HOURS after it, with the difference taken mod 24 so a 23:00
        # reminder is still recoverable at 01:00. That window also covers the one hour a
        # spring-forward transition removes.
        hours_late = (local.hour - user.workout_reminder_hour) % 24
        if hours_late > REMINDER_CATCH_UP_HOURS:
            continue

        # The local DATE THE REMINDER WAS DUE, not the date now. At 01:00 a 23:00
        # reminder belongs to yesterday; keying it to today would send it a second time
        # under a fresh dedup key.
        today = (local - timedelta(hours=hours_late)).date()

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
