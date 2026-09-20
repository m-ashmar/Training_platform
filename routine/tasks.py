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

#: At most this many open days a run. A night that hits it says so and the next run
#: takes the rest.
SWEEP_MAX_DAYS = 500


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

    from .models import DayOutcome, ExerciseSetLog, RoutineProgress
    from .outcomes import record_day_quietly

    before = before or timezone.localdate()
    # A trailing window, not all of history: without it the first run would reconstruct
    # every half-finished day ever left behind, measure it against TODAY's plan and turn
    # years-old days into streak breaks.
    window_start = before - timedelta(days=SWEEP_WINDOW_DAYS)
    recorded = DayOutcome.objects.filter(
        user_id=models.OuterRef('user_id'), routine_id=models.OuterRef('routine_id'),
        day=models.OuterRef('day'), date=models.OuterRef('date'))
    # `in_progress` is not the whole of an unfinished day. A client who does two of
    # three sets on EVERY exercise finishes none of them, so the row reads
    # `not_started` (routine/models.py counts an exercise only at its full set count) —
    # and a sweep that looked only at `in_progress` left exactly that day unrecorded:
    # the missed sets vanished and the streak was never broken, which is the loss this
    # feature exists to stop. A day nobody touched must still not become a miss, so a
    # `not_started` row is swept only when it carries work.
    worked = ExerciseSetLog.objects.filter(
        user_exercise_progress__user_id=models.OuterRef('user_id'),
        date=models.OuterRef('date'))
    open_days = (RoutineProgress.objects
                 .filter(status__in=('in_progress', 'not_started'),
                         date__lt=before, date__gte=window_start)
                 .annotate(already=models.Exists(recorded), worked=models.Exists(worked))
                 .filter(already=False)
                 .filter(models.Q(status='in_progress')
                         | models.Q(exercises_completed__gt=0) | models.Q(worked=True))
                 .select_related('user', 'routine')
                 .order_by('date', 'id')[:SWEEP_MAX_DAYS])

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
    capped = closed + skipped >= SWEEP_MAX_DAYS
    if capped:
        # Said out loud: the rest is picked up by the next run, and a cap that binds
        # every night means the window or the cap is wrong.
        logger.warning("Open-day sweep hit its %s-row cap; the remainder waits for the "
                       "next run", SWEEP_MAX_DAYS)
    return {'closed': closed, 'skipped': skipped, 'capped': capped}


def _coaching_rosters() -> dict:
    """`{trainer_id: [client_id, ...]}` for every approved coaching relation."""
    from users.models import TrainerClientRelation

    rosters: dict = {}
    for trainer_id, client_id in (TrainerClientRelation.objects
                                  .filter(status='approved')
                                  .values_list('trainer_id', 'client_id')):
        rosters.setdefault(trainer_id, []).append(client_id)
    return rosters


@shared_task(
    name="routine.send_weekly_progress",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def send_weekly_progress(now=None):
    """The week, once a week: a recap to each client, a digest to each trainer.

    The owner (2026-09-20): this is what brings people back, and what keeps a coach
    looking at their clients. Both texts are facts — sets planned and done, days with
    nothing missed, how many clients trained — with no amount of money, no link and no
    health claim, so they can reach every build.

    A client with nothing to report (no plan closed and nothing trained) is not nudged:
    the workout reminders already do that job, and a "0 of 0" is not news. A client whose
    coaching is paused or over is left out too — their own notices are asking them to
    pay, and a recap would read as if nothing had happened.

    Dedup is the ISO week, so a retry or a second run sends nothing twice.
    """
    from datetime import timedelta

    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from notifications.models import UserNotificationPreference
    from notifications.services import NotificationService
    from users.coaching import refusals_for

    from .outcomes import attention, week_totals

    now = now or timezone.now()
    today = timezone.localdate(now)
    start = today - timedelta(days=6)
    week_key = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
    User = get_user_model()

    # --- each client's own week ------------------------------------------------
    clients = list(User.objects.filter(user_type='client', is_active=True)
                   .only('id', 'preferred_language'))
    totals = week_totals([client.pk for client in clients], start, today)
    sent_clients = 0
    # Who is frozen, ended or unpaid — per COACH, for the whole roster at once. Asking
    # one arbitrary routine (the most recently updated one assigned to the client) let a
    # client with two coaches be judged by the wrong one, and cost a handful of queries
    # per client besides.
    frozen: set = set()
    for trainer_id, client_ids in _coaching_rosters().items():
        for client_id, refusal in refusals_for(trainer_id, client_ids).items():
            if refusal is not None:
                frozen.add(client_id)
    for client in clients:
        week = totals.get(client.pk)
        if not week or not week.get('planned_sets'):
            continue
        # The cheap refusals first: a recap of a week nobody was allowed to train is not
        # a recap, and someone who turned this off is not asked for one.
        if client.pk in frozen:
            continue
        if not UserNotificationPreference.is_channel_enabled(client, 'weekly_recap'):
            continue
        if NotificationService.create_and_send(
                recipient=client, event_type="weekly_recap",
                related_object_id=week_key,
                metadata={'context': {'logged': week['logged_sets'],
                                      'planned': week['planned_sets'],
                                      'perfect': week['perfect_days']},
                          'data': {'type': 'weekly_recap', 'week': week_key,
                                   'missed_sets': str(week['missed_sets'])}}) is not None:
            sent_clients += 1

    # --- each trainer's roster -------------------------------------------------
    rosters = _coaching_rosters()
    sent_trainers = 0
    for trainer in User.objects.filter(pk__in=rosters, is_active=True).only('id'):
        # The opt-out decides before the work, not after it: a trainer who turned the
        # digest off used to pay for a full roster scan every week to be skipped.
        if not UserNotificationPreference.is_channel_enabled(trainer, 'trainer_weekly_digest'):
            continue
        ids = rosters[trainer.pk]
        week = week_totals(ids, start, today)
        trained = sum(1 for client_id in ids if week.get(client_id, {}).get('logged_sets'))
        needs = attention(trainer, ids, today=today)
        if NotificationService.create_and_send(
                recipient=trainer, event_type="trainer_weekly_digest",
                related_object_id=week_key,
                metadata={'context': {'clients': len(ids), 'trained': trained,
                                      'attention': len(needs)},
                          'data': {'type': 'trainer_weekly_digest', 'week': week_key,
                                   'attention': str(len(needs))}}) is not None:
            sent_trainers += 1

    logger.info("Weekly progress: %s clients, %s trainers", sent_clients, sent_trainers)
    return {'clients': sent_clients, 'trainers': sent_trainers, 'week': week_key}
