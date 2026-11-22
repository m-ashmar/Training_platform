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
                            related_object_type=None, event_id=None, extra_data=None):
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
            # The ids the app's page opens with, by name (Road to 10 S3.1): a finished
            # session carries `session_id`, `client_id`, `routine_id`. Never overrides
            # the three keys above. A task queued before this argument existed has none.
            **{k: v for k, v in (extra_data or {}).items()
               if k not in ('type', 'related_object_id', 'related_object_type')},
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

#: At most this many open days an INVOCATION (and never more than `PERIODIC_BATCH_SIZE`).
#: The rest is handed on to a fresh invocation the same night (`training_platform.
#: batching`); it used to wait for the next night, so a backlog larger than this never
#: drained and the same days were left open night after night.
SWEEP_MAX_DAYS = 500


@shared_task(
    name="routine.close_open_training_days",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def close_open_training_days(before=None, after=None):
    """Record what was left of training days that were opened and never closed.

    A page of open days per invocation (`training_platform.batching`: at most
    `PERIODIC_BATCH_SIZE`, and `SWEEP_MAX_DAYS`), in id order after `after`, and the rest
    handed on to a fresh invocation with the same `before`. `capped` says this
    invocation handed on. Each day is recorded at most once (the outcome is looked for
    first), so a redelivered page records nothing twice.

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

    from training_platform.batching import Batch, as_date, batch_size, carry
    from users.coaching import training_refusal

    from .models import DayOutcome, ExerciseSetLog, RoutineProgress
    from .outcomes import record_day_quietly

    before = as_date(before) or timezone.localdate()
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
    #
    # Work ON THAT DAY (SIDE_BUGS S-069): a set of that date whose exercise that routine
    # day prescribes, logged outside any session or in a session of that routine (and
    # of that day, when the session named one) — the counting rule of
    # `routine/outcomes.py day_work`. "Any set that date" swept every scaffold row the
    # assignment writes (one per day, all dated the start date), so training day 1 on
    # the day a routine was assigned turned days 2..N into `unclosed` misses with every
    # planned set missed.
    worked = ExerciseSetLog.objects.filter(
        user_exercise_progress__user_id=models.OuterRef('user_id'),
        date=models.OuterRef('date'),
        user_exercise_progress__exercise__routine_exercises__routine_id=models.OuterRef(
            'routine_id'),
        user_exercise_progress__exercise__routine_exercises__day=models.OuterRef('day'),
    ).filter(
        models.Q(workout_session__isnull=True)
        | (models.Q(workout_session__routine_id=models.OuterRef('routine_id'))
           & (models.Q(workout_session__day__isnull=True)
              | models.Q(workout_session__day=models.OuterRef('day')))))
    batch = Batch(size=min(batch_size(), SWEEP_MAX_DAYS))
    open_days = batch.page(
        RoutineProgress.objects
        .filter(status__in=('in_progress', 'not_started'),
                date__lt=before, date__gte=window_start)
        .annotate(already=models.Exists(recorded), worked=models.Exists(worked))
        .filter(already=False)
        .filter(models.Q(status='in_progress')
                | models.Q(exercises_completed__gt=0) | models.Q(worked=True))
        .select_related('user', 'routine'), after)

    closed = skipped = 0
    for row in open_days:
        if batch.spent():
            break
        batch.done(row.pk)
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
    # The rest, the same night, by a fresh invocation measuring against the same day.
    capped = batch.hand_on(close_open_training_days, before=carry(before))
    return {'closed': closed, 'skipped': skipped, 'capped': capped}


@shared_task(
    name="routine.send_weekly_progress",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def send_weekly_progress(now=None, phase="clients", after=None):
    """The week, once a week: a recap to each client, a digest to each trainer.

    In pages (`training_platform.batching`): each invocation takes at most
    `PERIODIC_BATCH_SIZE` people — clients first, then trainers — and hands the rest on
    to a fresh invocation with the same `now`, `phase` and the last id done. Every client
    on the platform and every roster in one task, with an FCM send per person, was the
    shape the production-readiness audit (2026-09-25) found cut off part-way under the
    300 s soft limit. A page reads only its own people's weeks and coaching states. The
    counts returned are this invocation's.

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
    from training_platform.batching import Batch, as_datetime, carry
    from users.coaching import refusals_for
    from users.models import TrainerClientRelation

    from .outcomes import attention, week_totals

    now = as_datetime(now) or timezone.now()
    today = timezone.localdate(now)
    start = today - timedelta(days=6)
    week_key = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
    User = get_user_model()
    batch = Batch()
    sent_clients = sent_trainers = 0

    # --- each client's own week ------------------------------------------------
    if phase == "clients":
        clients = batch.page(User.objects.filter(user_type='client', is_active=True)
                             .only('id', 'preferred_language'), after)
        totals = week_totals([client.pk for client in clients], start, today)
        # Who is frozen, ended or unpaid — per COACH, for this page's clients. Asking one
        # arbitrary routine (the most recently updated one assigned to the client) let a
        # client with two coaches be judged by the wrong one, and cost a handful of
        # queries per client besides.
        rosters: dict = {}
        for trainer_id, client_id in (TrainerClientRelation.objects
                                      .filter(status='approved',
                                              client_id__in=[c.pk for c in clients])
                                      .values_list('trainer_id', 'client_id')):
            rosters.setdefault(trainer_id, []).append(client_id)
        frozen: set = set()
        for trainer_id, client_ids in rosters.items():
            for client_id, refusal in refusals_for(trainer_id, client_ids).items():
                if refusal is not None:
                    frozen.add(client_id)
        sent_clients = _recaps(clients, batch, totals, frozen, week_key)
        if batch.cut:
            batch.hand_on(send_weekly_progress, now=carry(now), phase="clients")
            logger.info("Weekly progress: %s clients so far", sent_clients)
            return {'clients': sent_clients, 'trainers': 0, 'week': week_key}
        phase, after = "trainers", None

    # --- each trainer's roster -------------------------------------------------
    coaching = TrainerClientRelation.objects.filter(status='approved')
    for trainer in batch.page(User.objects.filter(
            pk__in=coaching.values('trainer_id'), is_active=True).only('id'), after):
        if batch.spent():
            break
        batch.done(trainer.pk)
        # The opt-out decides before the work, not after it: a trainer who turned the
        # digest off used to pay for a full roster scan every week to be skipped.
        if not UserNotificationPreference.is_channel_enabled(trainer, 'trainer_weekly_digest'):
            continue
        ids = list(coaching.filter(trainer=trainer).order_by('client_id')
                   .values_list('client_id', flat=True))
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
    batch.hand_on(send_weekly_progress, now=carry(now), phase="trainers")

    logger.info("Weekly progress: %s clients, %s trainers", sent_clients, sent_trainers)
    return {'clients': sent_clients, 'trainers': sent_trainers, 'week': week_key}


def _recaps(clients, batch, totals, frozen, week_key) -> int:
    """Each client's own week, for one page of clients (`send_weekly_progress`)."""
    from notifications.models import UserNotificationPreference
    from notifications.services import NotificationService

    sent = 0
    for client in clients:
        if batch.spent():
            break
        batch.done(client.pk)
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
            sent += 1
    return sent


#: Forgotten sessions a page (one UPDATE). Pages follow each other within an invocation
#: and then in fresh ones (`abandon_forgotten_sessions`); nothing user-facing waits on
#: them, because every reader applies the read rule itself (`routine/sessions.py`).
SESSION_SWEEP_MAX = 500


@shared_task(
    name="routine.abandon_forgotten_sessions",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def abandon_forgotten_sessions(now=None, after=None):
    """Close the workouts nobody ever finished (SIDE_BUGS S-022).

    `WorkoutSession.status` has carried an `abandoned` value since the model was
    written and NOTHING ever wrote it, so a client who started a workout and closed the
    app left a row reading `active` for ever. The window is the one in
    `routine/sessions.py`: a session begun more than `SESSION_MAX_SECONDS` ago was
    forgotten, not left running. That rule is applied on every read as well, so this
    task is housekeeping — it makes the column agree with what the readers already
    answer — and a night it does not run changes nobody's screen.

    **What happens to the work: nothing.** A set belongs to its
    `UserExerciseProgress`, which owns the day (`routine/models.py ExerciseSetLog`), and
    every figure built on sets — the exercise's completed sets, the day's
    `RoutineProgress`, its `DayOutcome`, adherence, volume, records — is computed from
    those rows and never from a session's status. Abandoning a session only stops the
    row claiming a workout is in progress; the sets logged inside it stay, stay counted,
    and stay editable. The day itself is closed by `close_open_training_days`, which
    runs ten minutes after this one and records what was left of it.

    `end_time` is deliberately left NULL: we do not know when the person stopped, and
    inventing a finish would put a fabricated duration into the activity chart. A
    session with no end has no duration, exactly as before.

    No lower bound on how far back it reaches, unlike `close_open_training_days`: that
    sweep RECONSTRUCTS a day and must not measure a year-old one against today's plan,
    while this one only stops a row lying about the present, which is right at any age.
    `SESSION_SWEEP_MAX` is the bound instead, oldest first so a backlog drains.

    `SESSION_SWEEP_MAX` is a PAGE, not the night's total: pages follow one another until
    none is left or `PERIODIC_BATCH_SECONDS` is spent, and then the rest is handed on to
    a fresh invocation (`training_platform.batching`) — it used to wait for the next
    night, so a backlog larger than one page never drained. `capped` says this
    invocation handed on. `after` is accepted for the hand-on and not needed: an
    abandoned session leaves the query.
    """
    from training_platform.batching import Batch, as_datetime, carry

    from .models import WorkoutSession
    from .sessions import forgotten

    now = as_datetime(now)
    batch = Batch()
    abandoned = 0
    while not batch.spent():
        stale = list(forgotten(WorkoutSession.objects.all(), now=now)
                     .order_by('start_time', 'id')
                     .values_list('pk', flat=True)[:SESSION_SWEEP_MAX])
        if not stale:
            break
        batch.handled += 1
        # `status='active'` again in the UPDATE: a session finished between the SELECT
        # and here is a completed workout, and this must not overwrite it.
        abandoned += (WorkoutSession.objects
                      .filter(pk__in=stale, status='active')
                      .update(status='abandoned'))
        if len(stale) < SESSION_SWEEP_MAX:
            break
    if abandoned:
        logger.info("Abandoned %s forgotten workout sessions", abandoned)
    capped = batch.hand_on(abandon_forgotten_sessions, now=carry(now))
    return {'abandoned': abandoned, 'capped': capped}
