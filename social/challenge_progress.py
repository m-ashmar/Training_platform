"""Challenge progress the server counts from logged training (BACKEND_BUGS B-075).

The 09-community design counts a challenge's progress automatically wherever it can be
measured — «كل مجموعة تسجّلها في تمارينك تُحسب تلقائياً» — and keeps a manual log only
for a challenge the app cannot measure. `ChallengeParticipation.current_value` was only
ever written by `update_progress`, so every challenge was a manual one.

**What is measured** — `Challenge.metric`, over the challenge's window, per person:

    volume_kg      Σ weight × reps of the sets they logged (sets with a weight and reps)
    sets           how many sets they logged
    training_days  how many different days they logged a set on
    workouts       how many workout sessions they finished (`status='completed'`)
    manual         nothing: `update_progress` is how the number moves (the default)

A set belongs to the day it was trained on (`ExerciseSetLog.date`, the copy of its
progress row's day); the window is the challenge's start and end dates in the server's
time zone, so a set logged on the first or last day counts whatever the hour. A workout
counts by its `start_time` inside the window. A set is counted ONCE: a set re-sent
without the app's id writes a second row with the same number, and the latest of those
rows is the set — the rule `routine/outcomes.py day_work` counts a day by, so a retried
upload cannot inflate a standing. Only sets logged against a progress row count (the
rows `routine/strength.py` and the adherence figures read); training logged before the
person joined counts if it falls inside the window — the challenge is about the month,
not about the moment of joining.

**When the number moves.**

* On `join`, for the person joining, before the standing is ranked.
* After every commit that writes or deletes a set (`ExerciseSetLog`), a progress row
  (`UserExerciseProgress`, whose delete takes its sets with it) or a workout session:
  the owner's running auto-counted challenges are recounted. ONE recount per person per
  transaction, however many sets it wrote (`schedule_recount`), after commit, so it
  reads committed rows.
* When the creator changes a running challenge's `metric` or dates: every participant,
  in one grouped query (`ChallengeViewSet.perform_update`).

A recount takes the challenge's row lock (the one `join`, `leave` and `update_progress`
take, so the lock order is unchanged), measures INSIDE it — the last writer to take the
lock reads every committed set — writes the value, the percentage and the completion
(`apply_value`, shared with `update_progress`), then re-ranks the standing
(`social/standing.py write_places`) and drops the person's cached challenge list.

**Cost.** Nothing for someone in no running auto-counted challenge beyond one indexed
query per commit. Otherwise one aggregate per challenge they are in, and the ranking
pass. Never one query per participant: a whole-challenge recount is one grouped query.

**Not done, deliberately.** No push per recount — `ChallengeProgressEvent` notifies the
person themselves, and counting on every set would send one per set; the manual log
still emits it. A challenge that is over, cancelled, hidden or not yet started is not
recounted: a finished standing is a record (the Challenge Winner badge keys on it). A
failure here is logged and swallowed — a challenge's number must never fail the set it
counts.
"""
import logging

from django.db import transaction
from django.db.models import Count, ExpressionWrapper, F, FloatField, Q, Sum
from django.dispatch import receiver
from django.db.models.signals import post_delete, post_save
from django.utils import timezone

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ the number itself
def measure(metric, user_ids, start, end) -> dict:
    """{user_id: value} for `metric` over [start, end], in ONE query.

    People with nothing in the window are absent from the answer (read them as 0).
    """
    from routine.models import ExerciseSetLog, WorkoutSession
    from .models import Challenge

    ids = sorted({uid for uid in user_ids if uid is not None})
    if not ids or metric == Challenge.METRIC_MANUAL:
        return {}

    if metric == 'workouts':
        rows = (WorkoutSession.objects
                .filter(user_id__in=ids, status='completed',
                        start_time__gte=start, start_time__lte=end)
                .order_by().values('user_id').annotate(value=Count('id'))
                .values_list('user_id', 'value'))
        return {uid: float(value or 0) for uid, value in rows}

    logged = ExerciseSetLog.objects.filter(
        user_exercise_progress__user_id__in=ids,
        date__gte=timezone.localdate(start), date__lte=timezone.localdate(end))
    # One row per SET: of the rows carrying the same number for the same exercise and
    # day (the progress row), the latest. Unnumbered rows are each a set.
    latest = (logged.filter(set_number__isnull=False)
              .order_by('user_exercise_progress_id', 'set_number', '-id')
              .distinct('user_exercise_progress_id', 'set_number')
              .values('id'))
    kept = logged.filter(Q(set_number__isnull=True) | Q(pk__in=latest))

    if metric == 'volume_kg':
        value = Sum(ExpressionWrapper(F('weight') * F('reps'), output_field=FloatField()),
                    filter=Q(weight__gt=0, reps__gt=0))
    elif metric == 'sets':
        value = Count('id')
    elif metric == 'training_days':
        value = Count('date', distinct=True)
    else:
        return {}
    rows = (kept.order_by().values('user_exercise_progress__user_id')
            .annotate(value=value)
            .values_list('user_exercise_progress__user_id', 'value'))
    return {uid: round(float(value or 0), 2) for uid, value in rows}


def apply_value(participation, challenge, value):
    """Set `current_value` and what follows from it: the percentage and the completion.

    ONE implementation for the manual log (`update_progress`) and the counted one, so
    the two cannot finish a challenge by different rules. Completion FOLLOWS the number
    in both directions: a participant corrected (or recounted) below the target is no
    longer `completed`, and is no longer eligible for the Challenge Winner badge.
    Returns the names of the fields it may have changed.
    """
    participation.current_value = value
    if challenge.target_value and challenge.target_value > 0:
        participation.progress_percentage = max(0.0, min(
            100.0, (value / challenge.target_value) * 100))
    else:
        participation.progress_percentage = 0.0
    if challenge.target_value:
        reached = value >= challenge.target_value
        if reached and participation.status != 'completed':
            participation.status = 'completed'
            participation.completed_at = timezone.now()
        elif not reached and participation.status == 'completed':
            participation.status = 'active'
            participation.completed_at = None
    return ['current_value', 'progress_percentage', 'status', 'completed_at']


# ------------------------------------------------------------------- recounting
def recount(challenge, user_ids=None) -> bool:
    """Recount `user_ids` (default: everyone in the standing) of one running challenge.

    Under the challenge's row lock, measuring inside it. Writes only the rows whose
    number moved (one bulk UPDATE) and does NOT re-rank: the caller does, once, after
    whatever else it changed (`social/standing.py write_places`). Returns whether any
    row moved. A manual, hidden or not-running challenge is left alone.
    """
    from .models import Challenge, ChallengeParticipation
    from .standing import in_standing

    with transaction.atomic():
        locked = Challenge.objects.select_for_update().filter(pk=challenge.pk).first()
        if (locked is None or not locked.auto_counted or locked.is_hidden
                or not locked.is_active):
            return False
        rows = in_standing(ChallengeParticipation.objects.filter(challenge=locked))
        if user_ids is not None:
            rows = rows.filter(user_id__in=list(user_ids))
        rows = list(rows)
        values = measure(locked.metric, [row.user_id for row in rows],
                         locked.start_date, locked.end_date)
        moved, fields = [], []
        for row in rows:
            value = values.get(row.user_id, 0.0)
            if value != row.current_value:
                fields = apply_value(row, locked, value)
                moved.append(row)
        if moved:
            ChallengeParticipation.objects.bulk_update(moved, fields)
        return bool(moved)


def recount_for_user(user_id) -> int:
    """Recount one person in every running auto-counted challenge they are in, re-rank
    each one that moved, and drop their cached challenge list. Returns how many moved."""
    from .models import Challenge, ChallengeParticipation
    from .standing import in_standing, write_places

    now = timezone.now()
    challenges = [row.challenge for row in (
        in_standing(ChallengeParticipation.objects.filter(user_id=user_id))
        .exclude(challenge__metric=Challenge.METRIC_MANUAL)
        .exclude(challenge__status__in=Challenge.TERMINAL_STATUSES)
        .filter(challenge__is_hidden=False, challenge__start_date__lte=now,
                challenge__end_date__gte=now)
        .select_related('challenge').order_by('challenge_id'))]
    moved = 0
    for challenge in challenges:
        with transaction.atomic():
            if recount(challenge, [user_id]):
                write_places(challenge)
                moved += 1
    if moved:
        from .views import ChallengeViewSet

        ChallengeViewSet.invalidate_challenges_cache_for(user_id)
    return moved


def _recount_quietly(user_id):
    try:
        recount_for_user(user_id)
    except Exception:
        # A challenge's number must never fail the training write that moved it.
        logger.warning("could not recount the challenges of user %s", user_id,
                       exc_info=True)


class _AfterCommit:
    """The one recount a transaction schedules, for every person its writes touched."""

    def __init__(self):
        self.user_ids = set()
        self.done = False

    def __call__(self):
        # Marked first: a job that has run takes no more people, even if it is still
        # listed on the connection (a test's captured callbacks stay listed).
        self.done = True
        for user_id in sorted(self.user_ids):
            _recount_quietly(user_id)


def schedule_recount(user_id):
    """Recount this person's challenges once the current transaction commits.

    Logging a whole day writes a set per row inside one transaction; this folds them
    into ONE recount by finding the job already queued on this transaction. Outside a
    transaction `on_commit` runs it straight away, the write being committed already.
    """
    if not user_id:
        return
    connection = transaction.get_connection()
    if connection.in_atomic_block:
        for entry in connection.run_on_commit:
            job = entry[1]
            if isinstance(job, _AfterCommit) and not job.done:
                job.user_ids.add(user_id)
                return
    job = _AfterCommit()
    job.user_ids.add(user_id)
    transaction.on_commit(job)


# ------------------------------------------------------------------- the triggers
def _owner_of_set(instance):
    """Whose set this is, without a query when the progress row is already loaded (the
    routine app's own receiver loads it before this one runs)."""
    from routine.models import ExerciseSetLog, UserExerciseProgress

    field = ExerciseSetLog._meta.get_field('user_exercise_progress')
    if field.is_cached(instance):
        progress = field.get_cached_value(instance)
        return progress.user_id if progress is not None else None
    if instance.user_exercise_progress_id is None:
        return None
    return (UserExerciseProgress.objects.filter(pk=instance.user_exercise_progress_id)
            .values_list('user_id', flat=True).first())


@receiver(post_save, sender='routine.ExerciseSetLog',
          dispatch_uid='social_challenge_recount_on_set_save')
@receiver(post_delete, sender='routine.ExerciseSetLog',
          dispatch_uid='social_challenge_recount_on_set_delete')
def recount_on_set(sender, instance, **kwargs):
    try:
        schedule_recount(_owner_of_set(instance))
    except Exception:
        logger.warning("could not schedule a challenge recount", exc_info=True)


@receiver(post_delete, sender='routine.UserExerciseProgress',
          dispatch_uid='social_challenge_recount_on_progress_delete')
def recount_on_progress_delete(sender, instance, **kwargs):
    try:
        schedule_recount(instance.user_id)
    except Exception:
        logger.warning("could not schedule a challenge recount", exc_info=True)


@receiver(post_save, sender='routine.WorkoutSession',
          dispatch_uid='social_challenge_recount_on_session_save')
def recount_on_session_save(sender, instance, **kwargs):
    # An open session counts in no metric; its finish (or a change to a finished one)
    # does.
    if instance.status == 'active':
        return
    try:
        schedule_recount(instance.user_id)
    except Exception:
        logger.warning("could not schedule a challenge recount", exc_info=True)


@receiver(post_delete, sender='routine.WorkoutSession',
          dispatch_uid='social_challenge_recount_on_session_delete')
def recount_on_session_delete(sender, instance, **kwargs):
    try:
        schedule_recount(instance.user_id)
    except Exception:
        logger.warning("could not schedule a challenge recount", exc_info=True)
