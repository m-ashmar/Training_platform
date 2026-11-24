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
def send_workout_reminders(now=None, after=None):
    """Remind each client about today's training, in their own evening.

    In pages (`training_platform.batching`): each invocation takes at most
    `PERIODIC_BATCH_SIZE` candidates and hands the rest on to a fresh invocation with
    the SAME `now`, so one hour's cohort is judged by one clock however many pages it
    takes. Every candidate on the platform in one task, with an FCM send per reminder,
    is what the catch-up window below had to paper over when the 300 s soft limit cut a
    run off (the production-readiness audit, 2026-09-25); the dedup key still makes a
    redelivered page send nothing twice.

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
    from notifications.domain.routine_events import SessionReminderEvent
    from notifications.services import NotificationService
    from routine.models import RoutineProgress
    from routine.services import routine_name_for
    from training_platform.batching import Batch, as_datetime, carry
    from training_platform.i18n import LanguageContext

    User = get_user_model()
    now = as_datetime(now) or timezone.now()

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
    batch = Batch()
    for user in batch.page(candidates, after):
        if batch.spent():
            break
        batch.done(user.pk)
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

        # Is a routine actually scheduled for today? One the client may see and not
        # start — its coaching is paused or over (the owner, 2026-09-19) — is not
        # "waiting for you": the coaching's own notices ask them to pay. It is skipped,
        # not the person: another trainer's routine may still be waiting. Asked once
        # per trainer, the answer depends on nothing else.
        from users.coaching import training_refusal
        refused_by = {}

        def refused(routine):
            if routine.created_by_id not in refused_by:
                refused_by[routine.created_by_id] = training_refusal(user, routine)
            return refused_by[routine.created_by_id]

        waiting = (
            RoutineProgress.objects.filter(
                user=user,
                routine__is_active=True,
                routine__start_date__lte=today,
                status__in=("not_started", "in_progress"),
            )
            .filter(Q(routine__end_date__isnull=True) | Q(routine__end_date__gte=today))
            .select_related("routine")
            .order_by("day", "id")
        )
        pending = None
        saw_refused = False
        for candidate in waiting[:50]:
            if refused(candidate.routine):
                saw_refused = True
                continue
            pending = candidate
            break
        if not pending:
            # No day waiting that can be started. The drift nudge ("it has been N days")
            # only while some routine of theirs can still be trained on.
            routines = list(user.assigned_routines.all()[:20])
            if saw_refused and not routines:
                continue
            if routines and all(refused(r) for r in routines):
                continue

        # The sentence by its key on the template (`SessionReminderEvent.template.
        # variants`) and its params: the inbox renders it in the reader's language and a
        # push in the saved one (Road to 10, notification language P1). It was rendered
        # here with no language set, so every reminder was English, Arabic users
        # included.
        if pending:
            variant, params = "scheduled", {
                "day": pending.day,
                "routine": routine_name_for(pending.routine, user),
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
                variant, params = "yesterday", {}
            elif gap <= 3:
                variant, params = "days", {"days": gap}
            else:
                variant, params = "days_long", {"days": gap}
            data = {"type": "session_reminder", "reason": "drift", "days_since_last_workout": gap}

        # A reminder is a nudge, not a record: muted means not written at all (the
        # inbox otherwise keeps every notification, BACKEND_BUGS B-014).
        from notifications.models import UserNotificationPreference
        if not UserNotificationPreference.is_channel_enabled(user, 'session_reminder'):
            continue
        # `message`, the fallback a reader without the variant reads, in the saved
        # language.
        with LanguageContext.for_user(user):
            message = str(SessionReminderEvent.template.variants[variant]) % params
        result = NotificationService.create_and_send(
            recipient=user,
            event_type="session_reminder",
            related_object_id=today.isoformat(),
            metadata={"context": {"variant": variant, **params, "message": message},
                      "data": data},
        )
        if result is not None:
            sent += 1

    logger.info("workout reminders sent: %d", sent)
    batch.hand_on(send_workout_reminders, now=carry(now))
    return sent


# ================================================================== the email channel
#: Seconds before each retry of a notification email that did not go out: about 45
#: minutes in all. Longer than the account emails' (whose code dies in ten minutes): a
#: receipt or a withdrawal notice is still worth delivering an hour late.
EMAIL_RETRY_COUNTDOWNS = (30, 120, 600, 1800)

#: A row still `queued` (its message lost: the broker down at the commit) or `sending`
#: (its worker killed mid-send) this long after its last attempt is published again by
#: the sweep. Longer than the retry schedule above, so a retry waiting its turn is not
#: raced; the claim in `deliver` makes a race harmless anyway.
EMAIL_STALE_AFTER = timedelta(minutes=45)

#: How long an email is still worth sending at all. Older unsent rows are given up.
EMAIL_SEND_WINDOW = timedelta(days=3)


@shared_task(
    bind=True,
    name="notifications.send_email",
    max_retries=len(EMAIL_RETRY_COUNTDOWNS),
    acks_late=True,
    ignore_result=True,
    # One SMTP operation is bounded by EMAIL_TIMEOUT (10 s) and the receipt's PDF by a
    # few hundred milliseconds; this bounds the whole send, so a hung one frees its slot.
    soft_time_limit=60,
    time_limit=90,
)
def send_notification_email(self, delivery_id):
    """Send one email the notification system owes (`notifications.EmailDelivery`) — the
    worker's half of `notifications.channels.email.queue`.

    Written in the recipient's SAVED language, read now; sent only if the account may
    still be emailed (active, verified address, preferences). A transient failure is
    retried (`EMAIL_RETRY_COUNTDOWNS`); a permanent one, or the last retry's, is recorded
    `failed` and reported once (`deliver`). Returns what `deliver` returns — `sent`,
    `skipped`, the row's state when there was nothing to do, or `gone` — or `failed`.
    """
    from notifications.channels.email import EmailUndelivered, TransientEmailFailure, deliver

    try:
        return deliver(delivery_id, last_try=self.request.retries >= self.max_retries)
    except TransientEmailFailure as exc:
        raise self.retry(exc=exc.__cause__ or exc,
                         countdown=EMAIL_RETRY_COUNTDOWNS[self.request.retries])
    except EmailUndelivered:
        # Recorded and reported by `deliver`; nothing to retry.
        return "failed"


@shared_task(
    name="notifications.sweep_unsent_emails",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def sweep_unsent_emails(now=None, limit=200):
    """Hourly: publish again every email whose message was lost, and give up the ones
    that are too old or out of attempts.

    `queue` publishes after the commit and never raises into the caller, so a broker that
    was down at that moment leaves a row `queued` with no message; a worker killed between
    its claim and its send leaves one `sending`. Both are picked up here once their last
    attempt is `EMAIL_STALE_AFTER` old: `limit` a run, oldest first. A row older than
    `EMAIL_SEND_WINDOW`, or out of attempts, is recorded `failed` (`gave_up`) and logged.
    Idempotent: the claim in `deliver` sends each email once, whatever number of messages
    point at it. Returns the counts.
    """
    from django.db.models import Q
    from django.utils import timezone

    from notifications.channels.email import EMAIL_MAX_ATTEMPTS, publish
    from notifications.models import EmailDelivery
    from training_platform.batching import as_datetime

    now = as_datetime(now) or timezone.now()
    stale = now - EMAIL_STALE_AFTER
    unsent = (EmailDelivery.objects
              .filter(status__in=(EmailDelivery.QUEUED, EmailDelivery.SENDING))
              .filter(Q(last_attempt_at__lt=stale)
                      | Q(last_attempt_at__isnull=True, created_at__lt=stale)))

    given_up = (unsent.filter(Q(created_at__lt=now - EMAIL_SEND_WINDOW)
                              | Q(attempts__gte=EMAIL_MAX_ATTEMPTS))
                .update(status=EmailDelivery.FAILED, error="gave_up"))
    if given_up:
        logger.error("Gave up %s notification email(s) that never went out", given_up)

    republished = 0
    for pk in list(unsent.order_by("created_at", "id").values_list("pk", flat=True)[:limit]):
        # A `sending` row whose worker died goes back to the queue first, or the claim
        # would refuse it.
        EmailDelivery.objects.filter(pk=pk, status=EmailDelivery.SENDING) \
            .update(status=EmailDelivery.QUEUED)
        if publish(pk):
            republished += 1
    if republished:
        logger.warning("Published %s lost notification email(s) again", republished)
    return {"republished": republished, "given_up": given_up}
