"""How strong someone is getting, and where that is heading (the owner, 2026-09-20).

The owner asked for "expected enhancements in progress … read how his weight grows in
chest while keeping the set volume, and expect how stronger he will be there in two
weeks". Everything here is read from sets already logged (`ExerciseSetLog`) and from how
much of the plan was actually done (`routine/outcomes.py`). Nothing new is collected.

Three things live here:

  `points(...)`      one strength point per exercise per training day: the best set of
                     that day, its estimated one-rep max, and the day's volume.
  `trend(...)`       the direction over the last weeks, the change since the first week,
                     and the personal best.
  `projection(...)`  where that trend reaches in two weeks, as a RANGE, and only when
                     there is enough to say it.

Honesty rules, which are also product rules:

* A range, never a single number, and never a promise. The band comes from how far the
  person's own weeks sit from their trend line, so a jumpy history gives a wide band.
* Silence beats a guess: fewer than `MIN_POINTS` training days for that exercise, or a
  span shorter than `MIN_SPAN_DAYS`, answers `insufficient_data` and says what is missing.
* A flat or falling trend is never dressed up or dramatised: it reads as "holding
  steady", with the missed sets named as the lever, because that is the part the person
  controls.
* Nothing here claims a health outcome — no calories, no weight loss, no muscle gained,
  no "faster results". Those are neither derivable from this data nor safe to publish.

The maths is a weighted least-squares line over weekly bests. Not a model anyone has to
trust: it is explainable to a client and to a coach, deterministic, and testable.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import F, Max, Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

#: How far back a trend looks. Long enough to see a direction, short enough that a
#: training block from six months ago does not decide today's answer.
TREND_WEEKS = 8

#: How far ahead a projection reaches. Two weeks is what the owner asked for, and it is
#: about as far as a line through a few weeks of training can honestly carry.
HORIZON_DAYS = 14

#: The least a projection needs: this many training days of that exercise, spread over at
#: least this many days. One heavy session is not a trend.
MIN_POINTS = 3
MIN_SPAN_DAYS = 14

#: A change smaller than this, per week, is not a direction — it is noise.
FLAT_PERCENT_PER_WEEK = 0.5

#: Brzycki is meaningful to about ten reps; past this the set says more about endurance.
MAX_REPS_FOR_ESTIMATE = 12


def one_rep_max(weight, reps) -> float:
    """Brzycki's estimate of a one-rep maximum, in kilograms.

    The same formula the set-log serializer has always answered, in one place now, with
    the same guard: the divisor `37 - reps` is zero at 37 reps and negative beyond it, so
    a long set reports the weight that was actually lifted.
    """
    if not weight or not reps or reps <= 0:
        return 0.0
    if reps >= 36:
        return round(float(weight), 1)
    return round(float(weight) * (36 / (37 - float(reps))), 1)


def points(user, exercise_id, *, since=None, until=None) -> list[dict]:
    """One entry per training DAY of this exercise, oldest first.

    The best set of the day decides the day: the highest estimated one-rep max, and among
    equals the heavier weight. Sets with no weight or no reps (a run, a stretch, a day
    logged a whole day at a time) carry no strength information and are left out — they
    are counted in adherence instead.
    """
    from .models import ExerciseSetLog

    until = until or timezone.localdate()
    since = since or until - timedelta(weeks=TREND_WEEKS)
    # A set re-sent without the app's id writes a second row (there is no unique key on
    # the set number), so the day's numbered sets are collapsed to one each before
    # anything is summed. Unnumbered sets all count.
    seen: dict = {}
    logs = []
    for date, number, weight, reps in (ExerciseSetLog.objects
                                       .filter(user_exercise_progress__user=user,
                                               user_exercise_progress__exercise_id=exercise_id,
                                               date__gte=since, date__lte=until,
                                               weight__gt=0, reps__gt=0)
                                       .order_by('date', 'set_number', 'id')
                                       .values_list('date', 'set_number', 'weight', 'reps')):
        key = (date, number)
        if number is not None:
            if key in seen and (weight, reps) <= seen[key]:
                continue
            seen[key] = (weight, reps)
        logs.append((date, weight, reps))

    by_date: dict = {}
    for date, weight, reps in logs:
        estimate = one_rep_max(weight, reps)
        entry = by_date.get(date)
        if entry is None or (estimate, weight) > (entry['estimated_1rm'], entry['best_weight']):
            entry = by_date.setdefault(date, {})
            entry.update({'date': date, 'estimated_1rm': estimate,
                          'best_weight': float(weight), 'best_reps': int(reps),
                          'volume_kg': entry.get('volume_kg', 0.0), 'sets': entry.get('sets', 0)})
        entry = by_date[date]
        entry['volume_kg'] = round(entry.get('volume_kg', 0.0) + float(weight) * int(reps), 2)
        entry['sets'] = entry.get('sets', 0) + 1
    return [by_date[date] for date in sorted(by_date)]


def _median(values) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return 0.0
    middle = count // 2
    if count % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _weekly_points(series, until) -> list[tuple]:
    """`[(weeks_before_now, that week's typical day)]`, oldest first.

    The week's value is the MEDIAN of its training days, not its best one: a single
    heavy attempt should not tilt a trend, and a deload week should not look like a
    collapse. `x` counts real weeks back from `until`, so a month away from the gym
    leaves a real gap in the line instead of vanishing.
    """
    weeks: dict = {}
    for entry in series:
        index = (until - entry['date']).days // 7
        weeks.setdefault(index, []).append(entry['estimated_1rm'])
    return [(-index, _median(values)) for index, values in sorted(weeks.items(), reverse=True)]


def _fit(observations) -> tuple:
    """A robust line through `[(x, y)]`: Theil-Sen.

    Returns `(slope per week, value at x=0, spread)`. The slope is the MEDIAN of the
    slopes between every pair of weeks, so one outlying week cannot steer it — least
    squares would, and the person would be told they are getting weaker because they
    deloaded once. The spread is the median absolute deviation of the residuals, scaled
    to read like a standard deviation, which is what the projection's band is built from.
    """
    pairs = [((y2 - y1) / (x2 - x1))
             for index, (x1, y1) in enumerate(observations)
             for x2, y2 in observations[index + 1:]
             if x2 != x1]
    if not pairs:
        return 0.0, (observations[0][1] if observations else 0.0), 0.0
    slope = _median(pairs)
    intercept = _median([y - slope * x for x, y in observations])
    residuals = [abs(y - (intercept + slope * x)) for x, y in observations]
    spread = 1.4826 * _median(residuals)
    return slope, intercept, spread


def _direction(slope, level) -> str:
    if not level:
        return 'steady'
    percent = 100 * slope / level
    if percent > FLAT_PERCENT_PER_WEEK:
        return 'rising'
    if percent < -FLAT_PERCENT_PER_WEEK:
        return 'easing'
    return 'steady'


def trend(user, exercise_id, *, until=None, series=None) -> dict:
    """Where this exercise stands and which way it is going.

    `change_percent` compares the first and the last training day in the window, so it is
    a fact about what was lifted, not about the fitted line.
    """
    until = until or timezone.localdate()
    series = points(user, exercise_id, until=until) if series is None else series
    if not series:
        return {'exercise_id': exercise_id, 'points': [], 'direction': 'steady',
                'sessions': 0, 'best': None, 'change_percent': None,
                'latest_estimated_1rm': None}
    first, last = series[0], series[-1]
    change = None
    if first['estimated_1rm']:
        change = round(100 * (last['estimated_1rm'] - first['estimated_1rm'])
                       / first['estimated_1rm'], 1)
    weekly = _weekly_points(series, until)
    slope = _fit(weekly)[0] if len(weekly) >= 2 else 0.0
    best = max(series, key=lambda entry: (entry['estimated_1rm'], entry['best_weight']))
    return {
        'exercise_id': exercise_id,
        'sessions': len(series),
        'points': series,
        'latest_estimated_1rm': last['estimated_1rm'],
        'latest': {'date': last['date'], 'weight': last['best_weight'],
                   'reps': last['best_reps'], 'volume_kg': last['volume_kg']},
        'best': {'date': best['date'], 'weight': best['best_weight'],
                 'reps': best['best_reps'], 'estimated_1rm': best['estimated_1rm']},
        'change_percent': change,
        'direction': _direction(slope, last['estimated_1rm']),
        'per_week_kg': round(slope, 2),
    }


def projection(user, exercise_id, *, until=None, series=None, adherence_percent=None) -> dict:
    """Where the trend reaches in `HORIZON_DAYS`, as a range — or why it is too early.

    `insufficient_data` is not a failure: it is the honest answer until there are
    `MIN_POINTS` training days of this exercise spread over `MIN_SPAN_DAYS`. `needs`
    says what is still missing, so the app can ask for it.
    """
    until = until or timezone.localdate()
    series = points(user, exercise_id, until=until) if series is None else series
    span = (series[-1]['date'] - series[0]['date']).days if len(series) >= 2 else 0
    if len(series) < MIN_POINTS or span < MIN_SPAN_DAYS:
        return {
            'available': False,
            'reason': 'insufficient_data',
            'needs': {'sessions': max(MIN_POINTS - len(series), 0),
                      'days': max(MIN_SPAN_DAYS - span, 0)},
            'sessions': len(series),
        }

    weekly = _weekly_points(series, until)
    slope, intercept, spread = _fit(weekly)
    level = intercept                                   # the line's value at "now"
    weeks_ahead = HORIZON_DAYS / 7
    middle = level + slope * weeks_ahead
    # The band is how far this person's own weeks sit from their line, never narrower
    # than 2% — a run of identical weeks must not read as certainty about the future.
    band = max(spread, 0.02 * middle)
    low, high = middle - band, middle + band
    direction = _direction(slope, level)
    if direction != 'rising':
        # Nothing is promised downwards: the honest statement is that it holds around
        # where it is, and the missed sets are the part the person controls.
        low, high = min(low, level - band), max(high, level)
    confidence = 'low'
    if len(weekly) >= 5 and spread <= 0.03 * max(level, 1):
        confidence = 'high'
    elif len(weekly) >= 4 and spread <= 0.06 * max(level, 1):
        confidence = 'medium'
    return {
        'available': True,
        'horizon_days': HORIZON_DAYS,
        'direction': direction,
        'from_estimated_1rm': round(level, 1),
        'low': round(max(low, 0), 1),
        'high': round(max(high, 0), 1),
        'per_week_kg': round(slope, 2),
        'confidence': confidence,
        'weeks_counted': len(weekly),
        # What it assumes: the training that produced the line keeps happening. The
        # figure is the share of PLANNED sets actually done (routine/outcomes.py).
        'assumes_sets_done_percent': adherence_percent,
    }


def personal_best(user, exercise_id, *, before=None):
    """The heaviest set ever logged for this exercise, or None.

    Heaviest, not "best estimated one-rep max": that is what `analytics/summary` has
    always counted as a record (`_records_in_window`, routine/views.py), and one product
    must not hold two definitions of its own word. `before` excludes the set being
    judged, so "is this a personal best?" asks about everything that came before it.
    """
    from django.db.models import Max

    from .models import ExerciseSetLog

    rows = (ExerciseSetLog.objects
            .filter(user_exercise_progress__user=user,
                    user_exercise_progress__exercise_id=exercise_id,
                    weight__gt=0, reps__gt=0))
    if before is not None:
        rows = rows.exclude(pk=before)
    heaviest = rows.aggregate(weight=Max('weight'))['weight']
    if heaviest is None:
        return None
    best = (rows.filter(weight=heaviest).order_by('-reps', '-date')
            .values('weight', 'reps', 'date').first())
    return {'weight': float(best['weight']), 'reps': int(best['reps']),
            'date': best['date'],
            'estimated_1rm': one_rep_max(best['weight'], best['reps'])}


def is_personal_record(log) -> bool:
    """True when this set is heavier than everything this person has logged for that
    exercise.

    The very first set of an exercise is not a record — there is nothing to beat, and a
    "new best!" on a first attempt is noise. The day's other sets count, so the heaviest
    set of a session is the only one that can claim it.
    """
    if not log.weight or not log.reps:
        return False
    progress = log.user_exercise_progress
    if progress is None:
        return False
    previous = personal_best(progress.user, progress.exercise_id, before=log.pk)
    if previous is None:
        return False
    return float(log.weight) > previous['weight']


# ------------------------------------------------------------------ many exercises
def recent_exercise_ids(user, *, since=None, limit=12) -> list[int]:
    """The exercises this person actually trained lately, most-trained first."""
    from .models import ExerciseSetLog

    since = since or timezone.localdate() - timedelta(weeks=TREND_WEEKS)
    rows = (ExerciseSetLog.objects
            .filter(user_exercise_progress__user=user, date__gte=since,
                    weight__gt=0, reps__gt=0)
            .values('user_exercise_progress__exercise_id')
            .annotate(days=Max('date'), sets=Sum(1))
            .order_by('-sets')[:limit])
    return [row['user_exercise_progress__exercise_id'] for row in rows]


def overview(user, *, until=None, limit=12, adherence_percent=None) -> dict:
    """Every exercise this person is training, with its trend and any projection.

    Ordered by how much it moved: what the app shows first is where the person is
    getting stronger. `top_movers` and `plateaus` are the same rows, split, so a screen
    can show either without asking twice.
    """
    from .models import Exercise

    until = until or timezone.localdate()
    ids = recent_exercise_ids(user, limit=limit)
    # Instances, not `.values_list('name')`: the name is a translated field and a values
    # query returns the raw column, which is how English names reached Arabic screens
    # elsewhere.
    names = {exercise.pk: exercise.name for exercise in Exercise.objects.filter(pk__in=ids)}
    exercises = []
    for exercise_id in ids:
        series = points(user, exercise_id, until=until)
        if not series:
            continue
        summary = trend(user, exercise_id, until=until, series=series)
        summary['exercise_name'] = names.get(exercise_id)
        summary['projection'] = projection(user, exercise_id, until=until, series=series,
                                           adherence_percent=adherence_percent)
        # The per-day series belongs to the one-exercise view, not to a list of twelve.
        summary.pop('points', None)
        exercises.append(summary)
    exercises.sort(key=lambda row: (row['change_percent'] is None, -(row['change_percent'] or 0)))
    rising = [row for row in exercises if row['direction'] == 'rising']
    return {
        'exercises': exercises,
        'top_movers': rising[:3],
        'plateaus': [row for row in exercises if row['direction'] != 'rising'][:3],
        'rising_count': len(rising),
        'tracked_count': len(exercises),
    }


def headline(summary) -> dict:
    """The one line a card shows, as data the app renders in the person's language.

    Returned as a `kind` plus numbers rather than a sentence, so the app writes it in
    Arabic or English without the server guessing the screen's wording. Nothing here
    claims a health outcome.
    """
    if not summary or not summary.get('sessions'):
        return {'kind': 'no_data'}
    projected = summary.get('projection') or {}
    if projected.get('available') and projected['direction'] == 'rising':
        return {'kind': 'rising_with_projection',
                'exercise_id': summary['exercise_id'],
                'exercise_name': summary.get('exercise_name'),
                'change_percent': summary.get('change_percent'),
                'from_kg': projected['from_estimated_1rm'],
                'low_kg': projected['low'], 'high_kg': projected['high'],
                'horizon_days': projected['horizon_days'],
                'confidence': projected['confidence']}
    if (summary.get('change_percent') or 0) > 0:
        return {'kind': 'rising',
                'exercise_id': summary['exercise_id'],
                'exercise_name': summary.get('exercise_name'),
                'change_percent': summary['change_percent'],
                'weeks': TREND_WEEKS}
    return {'kind': 'holding',
            'exercise_id': summary['exercise_id'],
            'exercise_name': summary.get('exercise_name'),
            'best_kg': (summary.get('best') or {}).get('weight')}
