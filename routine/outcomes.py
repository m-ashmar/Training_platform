"""What a training day planned, what was done, and what was left (the owner, 2026-09-20).

Before this, the warning before completing a day counted the unlogged sets and threw the
numbers away: `confirm_unlogged` stored `RoutineProgress.status = 'completed'` and nothing
else, so a day closed with half the sets missing was indistinguishable from a perfect one
in streaks, completion, the trainer's roster and the achievements. Nobody could answer
"how many reps did I miss this week" or "how many days did I finish everything".

`DayOutcome` answers it. One row per (user, routine, day, date), written the moment the
day is closed by ANY path:

    update_progress completed        via='completed' / 'confirmed_unlogged'
    update_progress skipped          via='skipped'
    user-exercise-progress bulk-complete   via='bulk_complete'
    a workout session finished on a day    via='session'
    the recalculation completing a day     via='auto'      (routine/models.py)
    the nightly sweep, for a day opened and never closed   via='unclosed'

`quick_log` marks a day logged after the fact in one go (BACKEND_BUGS B-157):
`update_progress` with `quick_log: true`, and every `bulk-complete`. It is kept on the
row whatever `via` ends up saying, and no later close removes it.

The plan columns are a SNAPSHOT: they are frozen on the first write, because the trainer
edits `RoutineExercise` in place and an edit must not rewrite last week. The logged
columns are refreshed by later writes for the same day, so sets synced after the close
still count — the same rule as "a completed day stays completed".

Nothing here decides what a person may do: the training refusals (a frozen or ended
coaching) are checked by the views before they call in, and a day nobody was allowed to
train is never swept (see `routine/tasks.py`).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

#: How a day was closed, strongest first. A later close never downgrades the reason: a
#: day the person closed by hand is recorded as that, even if a set arrives afterwards.
VIA_PRECEDENCE = ('confirmed_unlogged', 'completed', 'skipped', 'bulk_complete',
                  'session', 'auto', 'unclosed')


def _rank(via: str) -> int:
    return VIA_PRECEDENCE.index(via) if via in VIA_PRECEDENCE else len(VIA_PRECEDENCE)


def day_plan(routine, day) -> list[dict]:
    """The coach's prescription for one routine day, one entry per exercise row.

    `sets` is at least 1: an exercise with no set count still asks for one (a treadmill
    run). `reps`, `weight` are None when the coach did not prescribe them.
    """
    plan = []
    for row in (routine.routine_exercises.filter(day=day)
                .order_by('order', 'id')
                .values('id', 'exercise_id', 'order', 'sets', 'reps', 'weight')):
        plan.append({
            'routine_exercise_id': row['id'],
            'exercise_id': row['exercise_id'],
            'order': row['order'],
            'sets': max(row['sets'] or 0, 1),
            'reps': row['reps'],
            'weight': row['weight'],
        })
    return plan


def day_work(user, routine, date, plan) -> dict:
    """What was actually recorded that date, per PLANNED ROW of the day.

    Sets count from the set logs of that date, and a log that belongs to a session of a
    DIFFERENT routine does not count here — one exercise can sit in two routines, and
    B-011 already fixed the same confusion for "log a whole day". "Log a whole day"
    (bulk-complete) writes `completed_sets` with no set logs at all, so its number is
    taken when it is larger, and the day's reps are then unknown. A progress row the
    client marked SKIPPED is not work, however many sets it claims — the same rule the
    recalculation uses.

    An exercise may appear twice on one day (a warm-up row and a top set): the day's logs
    for it are a single pool, filled into its rows in `order`, so nothing is counted
    twice and no row's prescription is lost.

    `date` here is the set log's own column, which is a COPY of its progress row's day
    and is held to it on write (`routine/models.py ExerciseSetLog`). The progress row
    decides: this function and `refresh_routine_progress` used to read the two
    independently, so a set written with a mismatched date was counted in one figure
    and missing from the other.

    Returns `{routine_exercise_id: {'logged', 'reps', 'volume', 'from_logs', 'skipped'}}`.
    """
    from .models import ExerciseSetLog, UserExerciseProgress

    exercise_ids = {row['exercise_id'] for row in plan}
    work = {row['routine_exercise_id']: {'logged': 0, 'reps': 0, 'volume': 0.0,
                                         'from_logs': 0, 'skipped': False}
            for row in plan}
    if not exercise_ids:
        return work

    allowed = Q(workout_session__isnull=True) | Q(workout_session__routine_id=routine.pk)
    pools: dict[int, list] = {eid: [] for eid in exercise_ids}
    # One row per SET, not per row written. A set re-sent without the app's id writes a
    # second row (only `client_uuid` is unique), and counting rows let three rows for
    # two sets fill a three-set prescription: a short day read as complete.
    numbered: dict = {}
    for exercise_id, number, reps, weight in (ExerciseSetLog.objects
                                              .filter(user_exercise_progress__user=user,
                                                      user_exercise_progress__exercise_id__in=exercise_ids,
                                                      date=date)
                                              .filter(allowed)
                                              .order_by('set_number', 'id')
                                              .values_list('user_exercise_progress__exercise_id',
                                                           'set_number', 'reps', 'weight')):
        if number is None:
            pools[exercise_id].append((reps or 0, weight or 0))
            continue
        numbered[(exercise_id, number)] = (reps or 0, weight or 0)
    for (exercise_id, _number), row in sorted(numbered.items(), key=lambda item: item[0][1]):
        pools[exercise_id].append(row)

    claims, skips = {}, {}
    for exercise_id, completed, skipped in (UserExerciseProgress.objects
                                            .filter(user=user, exercise_id__in=exercise_ids,
                                                    date=date)
                                            .values_list('exercise_id', 'completed_sets',
                                                         'skipped')):
        skips[exercise_id] = skips.get(exercise_id, False) or bool(skipped)
        if not skipped:
            claims[exercise_id] = max(claims.get(exercise_id, 0), completed or 0)

    for exercise_id in exercise_ids:
        rows = [row for row in plan if row['exercise_id'] == exercise_id]
        planned = sum(row['sets'] for row in rows)
        pool = pools[exercise_id][:planned]
        # A claim from "log a whole day" tops the pool up, never beyond the plan.
        logged_total = min(max(len(pool), claims.get(exercise_id, 0)), planned)
        taken = 0
        for row in rows:
            entry = work[row['routine_exercise_id']]
            entry['skipped'] = skips.get(exercise_id, False)
            share = min(row['sets'], max(logged_total - taken, 0))
            entry['logged'] = share
            counted = pool[taken:taken + share]
            entry['from_logs'] = len(counted)
            entry['reps'] = sum(reps for reps, _weight in counted)
            entry['volume'] = round(sum(weight * reps for reps, weight in counted), 2)
            taken += share
    return work


def plan_from_detail(detail) -> list[dict]:
    """The plan as it was when the day closed, read back from the stored snapshot.

    A refresh measures new sets against THIS, never against the routine as it is today:
    the coach edits `RoutineExercise` in place, and an edit must not rewrite a closed day.
    """
    plan = []
    for row in detail or []:
        if not isinstance(row, dict) or row.get('routine_exercise_id') is None:
            return []           # written before the snapshot carried rows: re-read live
        plan.append({
            'routine_exercise_id': row['routine_exercise_id'],
            'exercise_id': row['exercise_id'],
            'order': row.get('order'),
            'sets': row.get('planned_sets') or 0,
            'reps': row.get('planned_reps'),
            'weight': row.get('planned_weight'),
        })
    return plan


def day_counts(routine, user, day, date=None, plan=None) -> dict:
    """Planned, logged and missed sets of one routine day, with the per-row detail.

    The three top-level numbers are the ones the warning before completing a day has
    always answered (`prescribed`, `logged`, `unlogged`), so that contract is unchanged.
    """
    date = date or timezone.localdate()
    plan = day_plan(routine, day) if plan is None else plan
    work = day_work(user, routine, date, plan)

    planned_sets = logged_sets = 0
    planned_reps = logged_reps = 0
    planned_volume = logged_volume = 0.0
    has_reps = has_weight = False
    reps_known = True
    detail = []
    full = partial = untouched = skipped = 0

    for row in plan:
        done = work[row['routine_exercise_id']]
        planned_sets += row['sets']
        logged_sets += done['logged']
        # What was LIFTED counts whatever the coach prescribed: a client who puts 50 kg
        # on an exercise the coach set no weight for still moved 50 kg a rep. This sat
        # inside the "weight was prescribed" branch, so the most common routine — sets
        # and reps, weight left to the client — recorded every day as 0 kg, and the home
        # week and adherence said 0.0 while analytics/strength counted the same sets
        # (BACKEND_BUGS B-037). The PLANNED volume below still exists only where the
        # coach prescribed both reps and weight: there is no plan to measure against
        # otherwise, which is why it is null rather than zero.
        logged_volume += done['volume']
        if row['reps'] is not None:
            has_reps = True
            planned_reps += row['sets'] * row['reps']
            # Only rows the coach set reps for: otherwise "missed reps" would subtract
            # work nobody asked for from a plan that never asked for it.
            logged_reps += done['reps']
            if row['weight'] is not None:
                has_weight = True
                planned_volume += row['sets'] * row['reps'] * row['weight']
        if done['logged'] > done['from_logs']:
            # Counted from "log a whole day", which records no reps at all.
            reps_known = False
        if done['logged'] >= row['sets']:
            full += 1
        elif done['logged'] > 0:
            partial += 1
        elif done['skipped']:
            skipped += 1
        else:
            untouched += 1
        detail.append({
            'routine_exercise_id': row['routine_exercise_id'],
            'exercise_id': row['exercise_id'],
            'order': row['order'],
            'planned_sets': row['sets'],
            'planned_reps': row['reps'],
            'planned_weight': row['weight'],
            'logged_sets': done['logged'],
            'logged_reps': done['reps'],
            'logged_volume_kg': done['volume'],
            'skipped': done['skipped'],
        })

    return {
        'prescribed': planned_sets,
        'logged': logged_sets,
        'unlogged': max(planned_sets - logged_sets, 0),
        'planned_reps': planned_reps if has_reps else None,
        'logged_reps': logged_reps,
        'reps_known': reps_known,
        'planned_volume_kg': round(planned_volume, 2) if has_weight else None,
        'logged_volume_kg': round(logged_volume, 2),
        'exercises_planned': len(plan),
        'exercises_full': full,
        'exercises_partial': partial,
        'exercises_untouched': untouched,
        'exercises_skipped': skipped,
        'detail': detail,
    }


@transaction.atomic
def record_day(user, routine, day, date=None, *, via, confirmed_unlogged=False,
               session=None, prescription_exact=True, quick_log=False):
    """Write (or refresh) the outcome of one closed training day. Returns the row.

    Called inside the caller's transaction, after the write that closed the day. It
    costs two or three queries and never raises into the caller: a day is recorded, or
    the training still stands and the miss is logged.

    `quick_log` (BACKEND_BUGS B-157): the day was logged after the fact in one go. Kept
    like `confirmed_unlogged` — OR-ed, so a later close that does not say it (a plain
    `update_progress`, a set synced afterwards, the recalculation) never takes it away.
    It is not a `via`: `record_day` keeps the strongest `via`, and `completed` outranks
    `bulk_complete`, so a quick-log reason would have been masked by the very close that
    set it.
    """
    from .models import DayOutcome

    date = date or timezone.localdate()
    existing = DayOutcome.objects.filter(user=user, routine=routine, day=day,
                                         date=date).first()
    # A refresh measures new sets against the plan AS IT WAS when the day closed. Read
    # live, a coach's edit the next morning would rewrite the day: a finished day could
    # become a streak break.
    frozen = plan_from_detail(existing.detail) if existing is not None else None
    counts = day_counts(routine, user, day, date, plan=frozen or None)
    logged = {
        'logged_sets': counts['logged'],
        'logged_reps': counts['logged_reps'],
        'logged_volume_kg': counts['logged_volume_kg'],
        'exercises_full': counts['exercises_full'],
        'exercises_partial': counts['exercises_partial'],
        'exercises_untouched': counts['exercises_untouched'],
        'exercises_skipped': counts['exercises_skipped'],
        'reps_known': counts['reps_known'],
        'detail': counts['detail'],
    }
    planned = {
        'planned_sets': counts['prescribed'],
        'planned_reps': counts['planned_reps'],
        'planned_volume_kg': counts['planned_volume_kg'],
        'exercises_planned': counts['exercises_planned'],
        'prescription_exact': prescription_exact,
    }

    # `planned` already describes the frozen plan whenever there is one, because the
    # counts above were measured against it.
    outcome, created = DayOutcome.objects.select_for_update().get_or_create(
        user=user, routine=routine, day=day, date=date,
        defaults={**planned, **logged, 'via': via,
                  'confirmed_unlogged': bool(confirmed_unlogged),
                  'quick_log': bool(quick_log),
                  'missed_sets': max(counts['prescribed'] - counts['logged'], 0),
                  'complete': counts['unlogged'] == 0 and via != 'skipped',
                  'workout_session': session})
    if created:
        return outcome

    # The plan stays as it was when the day was closed; the work is refreshed. `detail`
    # carries both sides, so its planned fields are the frozen ones and only the logged
    # ones move.

    for field, value in logged.items():
        setattr(outcome, field, value)
    outcome.missed_sets = max(outcome.planned_sets - min(outcome.logged_sets,
                                                         outcome.planned_sets), 0)
    if _rank(via) < _rank(outcome.via):
        outcome.via = via
    outcome.confirmed_unlogged = outcome.confirmed_unlogged or bool(confirmed_unlogged)
    outcome.quick_log = outcome.quick_log or bool(quick_log)
    # `skipped` outranks the working reasons, which is right while nothing was done and
    # wrong the moment something is: a day marked skipped in the morning and trained in
    # the evening would have stayed incomplete for ever, because `complete` refuses the
    # label rather than the facts.
    if outcome.via == 'skipped' and via != 'skipped' and outcome.logged_sets > 0:
        outcome.via = via
    outcome.complete = outcome.missed_sets == 0 and outcome.via != 'skipped'
    if session is not None and outcome.workout_session_id is None:
        outcome.workout_session = session
    outcome.save(update_fields=[*logged, 'missed_sets', 'via', 'confirmed_unlogged',
                                'quick_log', 'complete', 'workout_session', 'updated_at'])
    return outcome


def refresh_day_quietly(user, routine, day, date):
    """Bring an existing outcome's logged figures up to date, leaving its reason and its
    frozen plan alone. Does nothing when the day has no outcome — a day that was never
    closed is the nightly sweep's business, not a set log's."""
    from .models import DayOutcome

    if not DayOutcome.objects.filter(user=user, routine=routine, day=day, date=date).exists():
        return None
    return record_day_quietly(user, routine, day, date, via='auto')


def record_day_quietly(user, routine, day, date=None, **kwargs):
    """`record_day`, but a failure is logged instead of undoing the training it
    describes. The nightly sweep writes what a miss here left behind."""
    try:
        return record_day(user, routine, day, date, **kwargs)
    except Exception:       # noqa: BLE001 — the training must stand
        logger.exception("day outcome not recorded for user %s routine %s day %s",
                         getattr(user, 'pk', user), getattr(routine, 'pk', routine), day)
        return None


# --------------------------------------------------------------------- reading
#: How far back the streak helpers look. Longer than any streak the app shows, and it
#: keeps the home screen off a full-history scan.
STREAK_WINDOW_DAYS = 400


def _window_start(since):
    return since if since is not None else timezone.localdate() - timedelta(days=STREAK_WINDOW_DAYS)


def broken_dates(user, since=None) -> set:
    """Dates this person closed a training day short (or skipped it) and completed none.

    The owner (2026-09-20): a day closed with sets still unlogged does not count as a
    training day, and it BREAKS the streak. A day on which another routine was finished
    in full still counts. The free first workout neither makes nor breaks a streak
    (`_counted`).
    """
    from .models import DayOutcome

    rows = _counted(DayOutcome.objects.filter(user=user, date__gte=_window_start(since)))
    per_date = (rows.values('date')
                .annotate(complete=Count('id', filter=Q(complete=True)), total=Count('id')))
    return {row['date'] for row in per_date if row['total'] and not row['complete']}


def _counted(rows):
    """Day outcomes that count as training days: not the ones on a person's copy of the
    platform's FREE FIRST WORKOUT (Road to 10 S2.4, the owner's D10: "without a streak
    or a programme the person later loses"). That workout is logged and kept like any
    other — its session, its sets, its outcome, its history — but one free workout on
    the first day must not start a streak that breaks the next morning, nor fill a week
    tile of a programme the person does not have."""
    return rows.exclude(routine__trial=True)


def complete_dates(user, since=None) -> set:
    """Dates with at least one training day finished in full (the free first workout
    excepted: `_counted`)."""
    from .models import DayOutcome

    rows = _counted(DayOutcome.objects.filter(user=user, complete=True,
                                              date__gte=_window_start(since)))
    return set(rows.values_list('date', flat=True))


def last_break(user, since=None):
    """The most recent day that broke the streak, for the app to point at, or None."""
    from .models import DayOutcome

    rows = DayOutcome.objects.filter(user=user, complete=False, date__gte=_window_start(since))
    broken = broken_dates(user, since=since)
    row = (rows.filter(date__in=broken).order_by('-date', '-missed_sets', '-id')
           .values('date', 'routine_id', 'day', 'missed_sets', 'via',
                   'planned_sets', 'logged_sets')
           .first())
    if row is None:
        return None
    row['reason'] = 'skipped' if row.pop('via') == 'skipped' else 'sets_missed'
    return row


def training_dates(user, since=None) -> tuple:
    """(dates that count as training days, whether any of them is only a legacy guess).

    A day counts when its outcome says nothing was missed. Days closed before outcomes
    existed have no row to judge, so their `RoutineProgress` status is all there is and
    they keep counting — the answer then says `approximate`.
    """
    from .models import DayOutcome, RoutineProgress

    start = _window_start(since)
    # `complete_dates` are dates with a complete outcome and `broken_dates` are dates
    # with none, so the two are disjoint by construction: subtracting one from the other
    # only cost a second scan of the same table.
    dates = complete_dates(user, since=start)
    recorded = set(DayOutcome.objects.filter(user=user, date__gte=start)
                   .values_list('date', flat=True))
    legacy = {day for day in RoutineProgress.objects
              .filter(user=user, status='completed', date__gte=start)
              .exclude(routine__trial=True)
              .values_list('date', flat=True)
              if day is not None and day not in recorded}
    return dates | legacy, bool(legacy)


def streak_from(dates: set, today=None) -> tuple:
    """(current, longest) runs of consecutive days in `dates`. A run is current when it
    reaches today or yesterday, as the streak endpoint has always required. A day after
    `today` has not been trained yet and counts in neither (SIDE_BUGS S-112): a run
    ending in the future read as current, because `(today - day).days` is negative."""
    today = today or timezone.localdate()
    ordered = sorted(d for d in dates if d is not None and d <= today)
    longest = run = 0
    previous = None
    for day in ordered:
        run = run + 1 if (previous is not None and (day - previous).days == 1) else 1
        longest = max(longest, run)
        previous = day
    current = run if ordered and (today - ordered[-1]).days <= 1 else 0
    return current, longest


def adherence(user, start, end, today=None):
    """Everything the home screen and the trainer's screens show about a window.

    One aggregate query plus one for the streak. `approximate` is true when any day in
    the window was filled in afterwards (the backfill) or was logged a whole day at a
    time, so its reps are not known.
    """
    from .models import DayOutcome

    rows = DayOutcome.objects.filter(user=user, date__gte=start, date__lte=end)
    totals = rows.aggregate(
        days=Count('id'),
        perfect=Count('id', filter=Q(complete=True)),
        short=Count('id', filter=Q(complete=False) & ~Q(via='skipped')),
        skipped=Count('id', filter=Q(via='skipped')),
        planned_sets=Sum('planned_sets'),
        logged_sets=Sum('logged_sets'),
        missed_sets=Sum('missed_sets'),
        planned_reps=Sum('planned_reps', filter=Q(reps_known=True)),
        logged_reps=Sum('logged_reps', filter=Q(reps_known=True)),
        planned_volume=Sum('planned_volume_kg', filter=Q(reps_known=True)),
        logged_volume=Sum('logged_volume_kg', filter=Q(reps_known=True)),
        approximate=Count('id', filter=Q(prescription_exact=False) | Q(reps_known=False)),
    )
    # The same definition the streak endpoint answers, legacy days included, so the two
    # screens never disagree.
    dates, approximate_streak = training_dates(user)
    current, longest = streak_from(dates, today=today)
    planned_sets = totals['planned_sets'] or 0
    logged_sets = totals['logged_sets'] or 0
    planned_reps = totals['planned_reps']
    logged_reps = totals['logged_reps'] or 0
    planned_volume = totals['planned_volume']
    return {
        'from': start,
        'to': end,
        'days_closed': totals['days'] or 0,
        'perfect_days': totals['perfect'] or 0,
        'short_days': totals['short'] or 0,
        'skipped_days': totals['skipped'] or 0,
        'sets': {
            'planned': planned_sets,
            'logged': logged_sets,
            'missed': totals['missed_sets'] or 0,
            'done_percent': round(100 * logged_sets / planned_sets, 1) if planned_sets else None,
        },
        'reps': {
            'planned': planned_reps,
            'logged': logged_reps,
            'missed': max((planned_reps or 0) - logged_reps, 0) if planned_reps else None,
        },
        'volume_kg': {
            'planned': round(planned_volume, 2) if planned_volume else None,
            'logged': round(totals['logged_volume'] or 0, 2),
        },
        'perfect_streak': {'current': current, 'longest': longest},
        'last_break': last_break(user, since=end - timedelta(days=90)),
        'approximate': bool(totals['approximate']) or approximate_streak,
    }


def volume_kg(user, start, end) -> float:
    """The weight this person actually moved in a window, in kilograms. One aggregate.

    The same figure `adherence` reports as `volume_kg.logged`, counted the same way —
    only days whose reps are known, because a day logged whole carries no weight. It
    exists on its own so the home screen can put last week beside this one for a change
    figure without paying for a second full adherence block.
    """
    from .models import DayOutcome

    total = (DayOutcome.objects
             .filter(user=user, date__gte=start, date__lte=end, reps_known=True)
             .aggregate(moved=Sum('logged_volume_kg'))['moved'])
    return round(total or 0.0, 2)


def week_totals(user_ids, start, end) -> dict:
    """`{user_id: {planned, logged, missed, perfect_days, days}}` for a list of clients,
    in one query. The trainer's roster shows a row per client, so this must not be a
    query per client."""
    from .models import DayOutcome

    collected = {}
    for row in (DayOutcome.objects
                .filter(user_id__in=list(user_ids), date__gte=start, date__lte=end)
                .values('user_id')
                .annotate(planned=Sum('planned_sets'), logged=Sum('logged_sets'),
                          missed=Sum('missed_sets'), days=Count('id'),
                          perfect=Count('id', filter=Q(complete=True)))):
        planned = row['planned'] or 0
        collected[row['user_id']] = {
            'planned_sets': planned,
            'logged_sets': row['logged'] or 0,
            'missed_sets': row['missed'] or 0,
            'days_closed': row['days'],
            'perfect_days': row['perfect'],
            'set_completion_percent': (round(100 * (row['logged'] or 0) / planned, 1)
                                       if planned else None),
        }
    return collected


#: How many days without training make a client "quiet" — idle, to the coach. ONE
#: number: `attention()` below, the trainer's roster read of it and the attention queue
#: (`users/attention_queue.py`) all use this constant, never a literal of their own.
QUIET_DAYS = 5


def attention(trainer, client_ids, *, today=None, quiet_days=QUIET_DAYS):
    """Which of these clients need the coach's eye, most urgent first.

    One batch of queries for the whole roster, never one per client: the roster screen
    shows every client at once, and that is how the rest of this file is written.

    Signals, each with the numbers behind it so the coach can judge rather than trust:
      `frozen` / `ended`     the coaching is paused or over (users/coaching.py)
      `never_started`        assigned a routine and has never trained — no finished
                             session and no closed day
      `quiet`                nothing trained for `quiet_days` (`days`), or never trained
                             and nothing assigned (`days` null)
      `slipping`             this week's share of planned sets is well below their own
                             four-week average — measured against themselves, because a
                             client who always does 70% is steady, not slipping
      `missing_sets`         a lot of planned sets left this week
    """
    from .models import DayOutcome, Routine, WorkoutSession

    today = today or timezone.localdate()
    ids = list(client_ids)
    if not ids:
        return []
    week_start = today - timedelta(days=6)
    month_start = today - timedelta(days=27)

    week = week_totals(ids, week_start, today)
    month = week_totals(ids, month_start, today)
    last_trained = dict(WorkoutSession.objects.filter(user_id__in=ids, status='completed')
                        .values('user_id').annotate(last=Max('end_time'))
                        .values_list('user_id', 'last'))
    for user_id, last in (DayOutcome.objects.filter(user_id__in=ids)
                          .values('user_id').annotate(last=Max('date'))
                          .values_list('user_id', 'last')):
        seen = last_trained.get(user_id)
        seen_date = timezone.localdate(seen) if seen else None
        if seen_date is None or (last and last > seen_date):
            last_trained[user_id] = last
    assigned = {row['assigned_to'] for row in Routine.objects
                .filter(assigned_to__in=ids, is_active=True)
                .values('assigned_to')}

    # One read for the whole roster: asking the coaching rule per client is a handful of
    # queries each, which is what every batched read in this file exists to avoid.
    from users.coaching import refusals_for
    refusals = refusals_for(getattr(trainer, 'pk', trainer), ids, at=today)

    rows = []
    for user_id in ids:
        this_week = week.get(user_id, {})
        this_month = month.get(user_id, {})
        share = this_week.get('set_completion_percent')
        usual = this_month.get('set_completion_percent')
        last = last_trained.get(user_id)
        last_date = timezone.localdate(last) if hasattr(last, 'tzinfo') else last
        quiet = (today - last_date).days if last_date else None

        reasons, score = [], 0
        refusal = refusals.get(user_id)
        if refusal:
            # A client the platform itself refuses is not "slipping": their own notices
            # are asking them to pay, and stacking "quiet" on top would read as blame.
            # The coach is told the state, and that is the whole story.
            rows.append({
                'client_id': user_id,
                'score': 60 if refusal == 'coaching_frozen' else 40,
                'reasons': [{'code': refusal.replace('coaching_', '')}],
                'last_trained': last_date,
                'this_week': {
                    'planned_sets': this_week.get('planned_sets', 0),
                    'logged_sets': this_week.get('logged_sets', 0),
                    'missed_sets': this_week.get('missed_sets', 0),
                    'set_completion_percent': share,
                    'perfect_days': this_week.get('perfect_days', 0),
                },
                'usual_set_completion_percent': usual,
            })
            continue
        # Idle is decided by the last training, whatever wrote it. A client whose
        # workouts carry no day has no `DayOutcome` yet trained yesterday: "never closed
        # a day" called them `never_started` (score 50) and the queue then dated their
        # idle card days ahead (review of users/attention_queue.py, 2026-09-24).
        if quiet is None and user_id in assigned:
            reasons.append({'code': 'never_started'})
            score += 50
        elif quiet is None:
            reasons.append({'code': 'quiet', 'days': None})
            score += 30
        elif quiet >= quiet_days:
            reasons.append({'code': 'quiet', 'days': quiet})
            score += min(10 + quiet * 4, 50)
        if share is not None and usual is not None and usual - share >= 20:
            reasons.append({'code': 'slipping', 'this_week': share, 'usual': usual})
            score += 30
        if this_week.get('missed_sets'):
            reasons.append({'code': 'missing_sets', 'missed': this_week['missed_sets'],
                            'planned': this_week.get('planned_sets', 0)})
            score += min(this_week['missed_sets'], 20)
        if not reasons:
            continue
        rows.append({
            'client_id': user_id,
            'score': score,
            'reasons': reasons,
            'last_trained': last_date,
            'this_week': {
                'planned_sets': this_week.get('planned_sets', 0),
                'logged_sets': this_week.get('logged_sets', 0),
                'missed_sets': this_week.get('missed_sets', 0),
                'set_completion_percent': share,
                'perfect_days': this_week.get('perfect_days', 0),
            },
            'usual_set_completion_percent': usual,
        })
    rows.sort(key=lambda row: (-row['score'], row['client_id']))
    return rows
