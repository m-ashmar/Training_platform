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

**What counts** (owner, 2026-09-24, F20: challenges must not be scammable). Only
credible work reaches a challenge; the constants below are the rules, served to the
app as `counting` on every auto-counted challenge row:

* a SET counts only with 1 <= reps <= `MAX_REPS_PER_SET` and 0 <= weight <=
  `MAX_WEIGHT_KG_PER_SET` (a set without a weight is bodyweight: it counts as a set and
  a day, and adds no volume). A 1000 kg x 1000 reps set counts nothing at all;
* a set RECEIVED (`ExerciseSetLog.created_at`) more than `LATE_HOURS` after the end of
  its training day (server time zone) does not count. The journal still takes it — a
  person may back-date a year (`routine/views.py MAX_BACKLOG_DAYS`) — the challenge
  simply does not count it. Rows older than the column (null) count as on time. The
  same for a set whose weight or reps CHANGED (`ExerciseSetLog.values_changed_at`) more
  than `LATE_HOURS` after its day: an on-time set raised days later does not count;
* per person per day at most `MAX_SETS_PER_DAY` sets count (the first ones received)
  and at most `MAX_VOLUME_KG_PER_DAY` of volume;
* a TRAINING DAY counts only with at least `MIN_SETS_PER_DAY` counted sets;
* a WORKOUT counts only when completed with at least `MIN_SETS_PER_WORKOUT` counted
  sets, lasting at least `MIN_WORKOUT_MINUTES` (`end_time - start_time`), and at most
  `MAX_WORKOUTS_PER_DAY` per day. Only the session's sets trained ON its start day count
  toward it: a session back-dated a month (the session routes allow it) and filled with
  today's sets counts nothing, and sets dated on its day must arrive on time.

A day above `REVIEW_THRESHOLDS` (read before the caps) flags the participant for an
operator's review (`ChallengeParticipation.review_state`); see `recount`.

**When the number moves.**

* On `join`, for the person joining, before the standing is ranked.
* After every commit that writes or deletes a set (`ExerciseSetLog`), a progress row
  (`UserExerciseProgress`, whose delete takes its sets with it) or a workout session:
  the owner's running auto-counted challenges are recounted. ONE recount per person per
  transaction, however many sets it wrote (`schedule_recount`), after commit, so it
  reads committed rows.
* When the creator changes a running challenge's `metric` or dates: every participant,
  in one grouped query (`ChallengeViewSet.perform_update`). Once the challenge is over
  its `metric`, dates and target are fixed (`challenge_over`), since nothing recounts
  it then.

A recount takes the challenge's row lock (the one `join`, `leave` and `update_progress`
take, so the lock order is unchanged), measures INSIDE it — the last writer to take the
lock reads every committed set — writes the value, the percentage and the completion
(`apply_value`, shared with `update_progress`), then re-ranks the standing
(`social/standing.py write_places`) and drops the person's cached challenge list.

**Cost.** Nothing for someone in no running auto-counted challenge beyond one indexed
query per commit. Otherwise one query per challenge they are in, and the ranking pass.
Never one query per participant: a whole-challenge recount is one query, which hands
back at most `MAX_SETS_PER_DAY` rows per person per day (or one row per qualifying
workout); the caps and the sum over days are a bounded Python pass over those rows
(`measure_for_review`).

**Not done, deliberately.** No push per recount — `ChallengeProgressEvent` notifies the
person themselves, and counting on every set would send one per set; the manual log
still emits it. A challenge that is cancelled, hidden or not yet started is not recounted. One that
has ended is recounted for `LATE_HOURS` more (its window's late-synced sets), never
after its winner is settled (`Challenge.counting_open`; `REVIEW_HOURS` equals
`LATE_HOURS`, so settlement waits for them): a settled standing is a record (the
Challenge Winner badge keys on it). A
failure here is logged and swallowed — a challenge's number must never fail the set it
counts.
"""
import logging

import threading
from contextlib import contextmanager

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import (Case, Count, DateTimeField, ExpressionWrapper, F, FloatField,
                              Func, IntegerField, OuterRef, Q, Subquery, Sum, Value, When,
                              Window)
from django.db.models.functions import RowNumber, TruncDate
from django.dispatch import receiver
from django.db.models.signals import post_delete, post_save
from django.utils import timezone

logger = logging.getLogger(__name__)


# ------------------------------------------------------------ what counts (F20)
#: The credible-work rules (owner, 2026-09-24, F20: "challenges must not be scammable").
#: They filter what a CHALLENGE counts, nothing else: the person's own journal, strength
#: charts and adherence read every set they logged. Served to the app as `counting` on
#: every auto-counted challenge row (`COUNTING`), so the rules are stated, not hidden.
MAX_REPS_PER_SET = 30          # a set counts only with 1 <= reps <= this
MAX_WEIGHT_KG_PER_SET = 400    # ... and 0 <= weight <= this (no weight = bodyweight = 0)
MIN_SETS_PER_DAY = 3           # a TRAINING DAY needs at least this many counted sets
MAX_SETS_PER_DAY = 60          # per person per day, at most this many sets count
MAX_VOLUME_KG_PER_DAY = 30000  # ... and at most this much volume (weight x reps)
MIN_SETS_PER_WORKOUT = 3       # a WORKOUT counts only with this many counted sets,
MIN_WORKOUT_MINUTES = 10       # ... lasting this long (end_time - start_time),
MAX_WORKOUTS_PER_DAY = 2       # ... and at most this many per day
LATE_HOURS = 72                # a set received more than this after its day ended: no

COUNTING = {
    'max_reps_per_set': MAX_REPS_PER_SET,
    'max_weight_kg_per_set': MAX_WEIGHT_KG_PER_SET,
    'min_sets_per_day': MIN_SETS_PER_DAY,
    'max_sets_per_day': MAX_SETS_PER_DAY,
    'max_volume_kg_per_day': MAX_VOLUME_KG_PER_DAY,
    'min_sets_per_workout': MIN_SETS_PER_WORKOUT,
    'min_workout_minutes': MIN_WORKOUT_MINUTES,
    'max_workouts_per_day': MAX_WORKOUTS_PER_DAY,
    'late_hours': LATE_HOURS,
}

#: A participant whose credible work on any SINGLE day of the window is above one of
#: these (for the challenge's metric, read BEFORE the per-day caps) is flagged for
#: review, reason `day_over_threshold`. Flagged people keep their place; a flagged
#: first place wins nothing until an operator clears it. Never un-flagged
#: automatically. `training_days` has no threshold: a day is one day.
REVIEW_THRESHOLDS = {
    'sets': 45,
    'volume_kg': 20000,
    'workouts': MAX_WORKOUTS_PER_DAY,
}


class _CountsUntil(Func):
    """The last moment a set trained on `date` may ARRIVE and still count: the end of
    that day in the server's time zone, plus `LATE_HOURS`."""

    output_field = DateTimeField()

    def as_sql(self, compiler, connection, **extra_context):
        sql, params = compiler.compile(self.source_expressions[0])
        return (f"(({sql} + interval '1 day' + interval '{int(LATE_HOURS)} hours') "
                f"AT TIME ZONE %s)", [*params, settings.TIME_ZONE])


def credible_sets(logged):
    """The sets of `logged` a challenge counts. See the module docstring, "What counts".

    On time first (received, and last changed, in time), then one row per set
    (the latest of the rows sharing a number), then the per-set limits.
    """
    logged = logged.filter(user_exercise_progress__isnull=False)
    until = _CountsUntil(F('date'))
    on_time = (logged.filter(Q(created_at__isnull=True) | Q(created_at__lte=until))
               .filter(Q(values_changed_at__isnull=True) | Q(values_changed_at__lte=until)))
    # One row per SET: of the rows carrying the same number for the same exercise and
    # day (the progress row), the latest. Unnumbered rows are each a set.
    latest = (on_time.filter(set_number__isnull=False)
              .order_by('user_exercise_progress_id', 'set_number', '-id')
              .distinct('user_exercise_progress_id', 'set_number')
              .values('id'))
    kept = on_time.filter(Q(set_number__isnull=True) | Q(pk__in=latest))
    return kept.filter(reps__gte=1, reps__lte=MAX_REPS_PER_SET).filter(
        Q(weight__isnull=True) | Q(weight__gte=0, weight__lte=MAX_WEIGHT_KG_PER_SET))


# ------------------------------------------------------------------ the number itself
def measure(metric, user_ids, start, end) -> dict:
    """{user_id: value} for `metric` over [start, end], in ONE query.

    People with nothing in the window are absent from the answer (read them as 0).
    """
    return measure_for_review(metric, user_ids, start, end)[0]


def measure_for_review(metric, user_ids, start, end) -> tuple[dict, dict]:
    """({user_id: value}, {user_id: the LATEST day over `REVIEW_THRESHOLDS`}), ONE query.

    Someone with no day over a threshold is absent from the second dict.

    The query hands back per-day rows, at most `MAX_SETS_PER_DAY` sets per person per
    day (a window function numbers them; the SQL drops the rest), or one row per
    qualifying workout; the per-day caps and the sum over days are a Python pass over
    those rows — bounded by 60 x the window's days per person, however many rows a
    cheat wrote.
    """
    from routine.models import ExerciseSetLog, WorkoutSession
    from .models import Challenge

    ids = sorted({uid for uid in user_ids if uid is not None})
    if not ids or metric == Challenge.METRIC_MANUAL:
        return {}, {}

    values, flagged = {}, {}
    if metric == 'workouts':
        sessions = WorkoutSession.objects.filter(
            user_id__in=ids, status='completed', start_time__gte=start,
            start_time__lte=end, end_time__isnull=False,
            end_time__gte=F('start_time') + timedelta(minutes=MIN_WORKOUT_MINUTES))
        sessions = sessions.annotate(
            trained_on=TruncDate('start_time', tzinfo=timezone.get_current_timezone()))
        counted = (credible_sets(ExerciseSetLog.objects.filter(
                       workout_session__in=sessions.values('pk')))
                   .filter(workout_session_id=OuterRef('pk'), date=OuterRef('trained_on'))
                   .order_by().values('workout_session_id')
                   .annotate(n=Count('id')).values('n'))
        per_day = {}
        for uid, started in (sessions
                             .annotate(sets=Subquery(counted, output_field=IntegerField()))
                             .filter(sets__gte=MIN_SETS_PER_WORKOUT)
                             .order_by().values_list('user_id', 'trained_on')):
            key = (uid, started)
            per_day[key] = per_day.get(key, 0) + 1
        for (uid, day), workouts in per_day.items():
            values[uid] = values.get(uid, 0.0) + min(workouts, MAX_WORKOUTS_PER_DAY)
            if workouts > REVIEW_THRESHOLDS['workouts']:
                flagged[uid] = max(day, flagged.get(uid, day))
        return {uid: float(value) for uid, value in values.items()}, flagged

    if metric not in ('volume_kg', 'sets', 'training_days'):
        return {}, {}
    credible = credible_sets(ExerciseSetLog.objects.filter(
        user_exercise_progress__user_id__in=ids,
        date__gte=timezone.localdate(start), date__lte=timezone.localdate(end)))
    volume = Case(When(weight__gt=0, then=ExpressionWrapper(
                      F('weight') * F('reps'), output_field=FloatField())),
                  default=Value(0.0), output_field=FloatField())
    day = [F('user_exercise_progress__user_id'), F('date')]
    rows = (credible
            .annotate(owner=F('user_exercise_progress__user_id'), kg=volume,
                      nth=Window(RowNumber(), partition_by=day, order_by=F('id').asc()),
                      day_sets=Window(Count('id'), partition_by=day),
                      day_kg=Window(Sum(volume), partition_by=day))
            .filter(nth__lte=MAX_SETS_PER_DAY)
            .order_by()
            .values_list('owner', 'date', 'kg', 'day_sets', 'day_kg'))
    days = {}
    for uid, date, kg, day_sets, day_kg in rows:
        entry = days.setdefault((uid, date), [0, 0.0, day_sets or 0, float(day_kg or 0)])
        entry[0] += 1
        entry[1] += float(kg or 0)
    for (uid, day), (sets, kg, all_sets, all_kg) in days.items():
        if metric == 'sets':
            value, over = sets, all_sets > REVIEW_THRESHOLDS['sets']
        elif metric == 'volume_kg':
            value = min(kg, MAX_VOLUME_KG_PER_DAY)
            over = all_kg > REVIEW_THRESHOLDS['volume_kg']
        else:
            value, over = (1 if sets >= MIN_SETS_PER_DAY else 0), False
        values[uid] = values.get(uid, 0.0) + value
        if over:
            flagged[uid] = max(day, flagged.get(uid, day))
    return {uid: round(float(value), 2) for uid, value in values.items()}, flagged


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
def reapply_target(challenge) -> int:
    """Re-judge every participant against the challenge's CURRENT target.

    A target edited while the challenge runs moved nobody's number, so `recount` —
    which rewrites only the rows whose number moved — never reached them: a participant
    completed at 22 of 20 stayed `completed` at 22 of 30 and could take the Challenge
    Winner badge for a target never reached (the review, M6). Manual and counted
    challenges alike. Returns how many rows changed.
    """
    from .models import Challenge, ChallengeParticipation
    from .standing import participating

    with transaction.atomic():
        locked = Challenge.objects.select_for_update().filter(pk=challenge.pk).first()
        if locked is None:
            return 0
        changed, fields = [], []
        for row in participating(ChallengeParticipation.objects.filter(challenge=locked)):
            before = (row.progress_percentage, row.status, row.completed_at)
            fields = apply_value(row, locked, row.current_value)
            if (row.progress_percentage, row.status, row.completed_at) != before:
                changed.append(row)
        if changed:
            ChallengeParticipation.objects.bulk_update(changed, fields)
        return len(changed)


def recount(challenge, user_ids=None) -> bool:
    """Recount `user_ids` (default: everyone counted) of one running challenge.

    Under the challenge's row lock, measuring inside it. Writes only the rows whose
    number or review moved (one bulk UPDATE) and does NOT re-rank: the caller does,
    once, after whatever else it changed (`social/standing.py write_places`). Returns
    whether any row moved. A manual, hidden or not-running challenge is left alone.

    Everyone who has not dropped out is counted, a DISQUALIFIED participant included
    (their number stays true for the operator who may clear them). A participant with
    a day over `REVIEW_THRESHOLDS` who is `clear` becomes `flagged` /
    `day_over_threshold`; nobody is un-flagged here, and a disqualified person stays so.
    Someone an operator CLEARED (`clear` / `operator`) is flagged again only for a day
    over a threshold AFTER `review_cleared_through` (the days the operator looked at):
    the days already cleared do not re-flag them on every set they log, and one clear
    is not a licence to log the daily maximum from then on.
    """
    from .models import Challenge, ChallengeParticipation
    from .standing import participating

    with transaction.atomic():
        locked = Challenge.objects.select_for_update().filter(pk=challenge.pk).first()
        if locked is None or not locked.counting_open():
            return False
        rows = participating(ChallengeParticipation.objects.filter(challenge=locked))
        if user_ids is not None:
            rows = rows.filter(user_id__in=list(user_ids))
        rows = list(rows)
        values, over = measure_for_review(locked.metric, [row.user_id for row in rows],
                                          locked.start_date, locked.end_date)
        moved = []
        fields = ['current_value', 'progress_percentage', 'status', 'completed_at',
                  'review_state', 'review_reason']
        for row in rows:
            value = values.get(row.user_id, 0.0)
            changed = False
            if value != row.current_value:
                apply_value(row, locked, value)
                changed = True
            if (row.user_id in over and _flags_again(row, over[row.user_id])):
                row.review_state = ChallengeParticipation.REVIEW_FLAGGED
                row.review_reason = ChallengeParticipation.REASON_DAY_OVER_THRESHOLD
                changed = True
            if changed:
                moved.append(row)
        if moved:
            ChallengeParticipation.objects.bulk_update(moved, fields)
        return bool(moved)


def _flags_again(row, latest_over_day) -> bool:
    """Does a day over a threshold (the latest is `latest_over_day`) flag `row`?"""
    from .models import ChallengeParticipation

    if row.review_state != ChallengeParticipation.REVIEW_CLEAR:
        return False                       # flagged already, or disqualified
    if row.review_reason is None:
        return True
    cleared = row.review_cleared_through
    return cleared is None or latest_over_day > cleared


def recount_for_user(user_id) -> int:
    """Recount one person in every running auto-counted challenge they are in, re-rank
    each one that moved, and drop their cached challenge list. Returns how many moved."""
    from .models import Challenge, ChallengeParticipation
    from .standing import participating, write_places

    now = timezone.now()
    challenges = [row.challenge for row in (
        participating(ChallengeParticipation.objects.filter(user_id=user_id))
        .exclude(challenge__metric=Challenge.METRIC_MANUAL)
        .exclude(challenge__status='cancelled')
        # Running, or ended less than LATE_HOURS ago and not settled: a late-synced set
        # of the window's last days still counts (`Challenge.counting_open`).
        .filter(challenge__is_hidden=False, challenge__start_date__lte=now,
                challenge__end_date__gte=now - timedelta(hours=LATE_HOURS),
                challenge__winner_checked_at__isnull=True)
        .select_related('challenge').order_by('challenge_id'))]
    moved = 0
    touched = {user_id}
    for challenge in challenges:
        with transaction.atomic():
            if recount(challenge, [user_id]):
                write_places(challenge, moved_users=touched)
                moved += 1
    if moved:
        from home.views import bump_stamp

        from .views import ChallengeViewSet

        # Everyone whose place moved, not only the person who logged: passing a rival
        # moves the rival too, and their cached list and home card still said 1st
        # while the board said 2nd (the review, M5; BACKEND_BUGS B-086).
        for other in sorted(touched):
            ChallengeViewSet.invalidate_challenges_cache_for(other)
            bump_stamp(other)
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


_batch = threading.local()


@contextmanager
def batched_recounts():
    """Hold every recount scheduled inside this block and run each person's ONCE when
    it ends — for a request that commits many small transactions, like the offline
    sync, which writes each set in a transaction of its own. Per transaction, a sync of
    200 sets re-ranked the challenge 200 times under its lock (the review, M4)."""
    if getattr(_batch, 'user_ids', None) is not None:
        yield                               # already batching: the outer block runs them
        return
    _batch.user_ids = set()
    try:
        yield
    finally:
        user_ids, _batch.user_ids = _batch.user_ids, None
        if user_ids:
            job = _AfterCommit()
            job.user_ids |= user_ids
            transaction.on_commit(job)       # straight away outside a transaction


def schedule_recount(user_id):
    """Recount this person's challenges once the current transaction commits.

    Logging a whole day writes a set per row inside one transaction; this folds them
    into ONE recount by finding the job already queued on this transaction. Outside a
    transaction `on_commit` runs it straight away, the write being committed already.
    Inside `batched_recounts` it waits for the block to end.
    """
    if not user_id:
        return
    held = getattr(_batch, 'user_ids', None)
    if held is not None:
        held.add(user_id)
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
