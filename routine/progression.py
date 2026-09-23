"""What to change next on one exercise, as a code a coach can argue with.

The owner asked for a rating of how hard a session felt and for something useful to be
made of it (BACKEND_DELTA item 11). This reads three things that already exist and
invents nothing:

  `WorkoutSession.effort_rating`   1-5, from the person who trained, per session
  `routine/outcomes.py adherence`  the share of PLANNED sets actually done
  `routine/strength.py trend`      which way the working weight is going

and answers a CODE plus the numbers behind it — never a rendered sentence, exactly as
`strength.headline` does, so the app writes it in Arabic or English and the coach sees
the figures the rule used rather than a verdict to trust.

The ladder, in order; the first one that matches wins. One sentence each, because a
coach has to be able to disagree with it out loud:

  `not_enough_data`  fewer than MIN_SESSIONS training days of this exercise in the
                     window, or none of the last EFFORT_SESSIONS sessions holding it
                     was rated — there is nothing to reason from, and a guess dressed
                     as a suggestion is worse than silence.
  `deload`           the typical effort is at or above DELOAD_EFFORT, or it is at or
                     above HARD_EFFORT while the trend is easing — the weight is
                     already winning, and asking for more of it is how people get hurt.
  `hold`             less than MIN_ADHERENCE percent of the last four weeks' planned
                     sets were done, or the typical effort is at or above HARD_EFFORT —
                     finish the plan at this weight before the plan gets heavier.
  `add_weight`       the typical effort is at or below EASY_EFFORT — the bar is light
                     enough that reps are no longer the limit.
  `add_reps`         everything left: the effort sits between EASY and HARD, so the
                     next step is one more repetition per set, not more weight.

Rules this is built under, the same ones `routine/strength.py` states:

* No health claim. Nothing here says anything about weight, body fat, calories,
  injury or recovery — it is arithmetic on sets, reps and a rating of effort.
* Silence beats a guess: `not_enough_data` is a real answer and says what is missing.
* The suggested number is a STEP, never a target: `next` carries one number derived
  from the last set that was actually lifted, and the coach can overrule it.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

#: How many of the most recent sessions holding the exercise are read — rated or not;
#: the ratings among them are the effort. Three is enough to tell a hard day from a
#: hard block, and short enough that last month's training does not decide today's
#: suggestion.
EFFORT_SESSIONS = 3

#: The least history the rule needs: training days of THIS exercise inside the window
#: `routine/strength.py` uses, and at least one of them rated.
MIN_SESSIONS = 3
MIN_RATED = 1

#: The effort thresholds, on the 1-5 scale the app shows.
EASY_EFFORT = 2.5           # at or below: the bar is light
HARD_EFFORT = 4.0           # at or above: hard, hold what you have
DELOAD_EFFORT = 4.5         # at or above: everything there was, back it off

#: Below this share of the planned sets, nothing is added: the plan itself is not being
#: finished, and a heavier plan is finished less. Matches how `outcomes.attention`
#: judges a week — against what was prescribed, not against a feeling.
MIN_ADHERENCE = 80.0

#: How far a step moves. 2.5 kg is the smallest pair of plates in an ordinary gym, so a
#: smaller suggestion could not be loaded; a deload takes a tenth off.
STEP_KG = 2.5
DELOAD_FRACTION = 0.9


def _median(values) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return 0.0
    middle = count // 2
    if count % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def recent_effort(user, exercise_ids, *, until=None, since=None,
                  sessions=EFFORT_SESSIONS) -> dict:
    """`{exercise_id: [rating, ...]}` — the ratings among the last `sessions` sessions
    holding each exercise, newest first.

    The last `sessions` SESSIONS, rated or not, and then the ratings among them — the
    rule the module docstring writes out ("none of the last EFFORT_SESSIONS sessions
    holding it was rated" is `not_enough_data`). It read the last `sessions` RATED
    sessions instead, so one rating from seven weeks ago, followed by any number of
    unrated workouts, still decided `add_weight` or `deload` today (SIDE_BUGS S-111).

    ONE query for every exercise asked about, because the coach's client screen asks
    about five at once and the client's own screen may ask about twelve. A session
    "holds" an exercise when one of its set logs belongs to that exercise's progress.
    """
    from .models import WorkoutSession
    from .strength import TREND_WEEKS

    ids = [int(i) for i in exercise_ids]
    if not ids:
        return {}
    until = until or timezone.localdate()
    since = since or until - timedelta(weeks=TREND_WEEKS)
    rows = (WorkoutSession.objects
            .filter(user=user,
                    start_time__date__gte=since, start_time__date__lte=until,
                    set_logs__user_exercise_progress__exercise_id__in=ids)
            # DISTINCT: the join is over set logs, so a session of ten sets would
            # otherwise contribute its one rating ten times and a single hard workout
            # would fill the whole window.
            .values_list('set_logs__user_exercise_progress__exercise_id', 'pk',
                         'effort_rating', 'start_time')
            .distinct()
            .order_by('-start_time', '-pk'))
    collected: dict = {exercise_id: [] for exercise_id in ids}
    seen: dict = {exercise_id: 0 for exercise_id in ids}
    for exercise_id, _pk, rating, _start in rows:
        if exercise_id not in seen or seen[exercise_id] >= sessions:
            continue
        seen[exercise_id] += 1          # a session read, whether or not it was rated
        if rating is not None:
            collected[exercise_id].append(int(rating))
    return collected


def _next_step(code, latest):
    """The one number the suggestion carries, from the last set actually lifted."""
    if not latest:
        return None
    weight = latest.get('weight') or 0
    reps = latest.get('reps') or 0
    if code == 'add_weight':
        return {'weight_kg': round(float(weight) + STEP_KG, 1)} if weight else None
    if code == 'add_reps':
        return {'reps': int(reps) + 1} if reps else None
    if code == 'deload':
        # To the nearest loadable step, and never up: a deload that rounds upwards is
        # not a deload.
        eased = float(weight) * DELOAD_FRACTION
        loadable = int(eased / STEP_KG) * STEP_KG
        return {'weight_kg': round(loadable, 1)} if loadable > 0 else None
    return None


def suggest(exercise_id, *, trend_summary, adherence_percent, ratings) -> dict:
    """The progression answer for one exercise. Reads nothing: everything is passed in.

    `trend_summary` is a `routine/strength.py trend(...)` answer, `adherence_percent`
    the share of planned sets done over the last four weeks (or None when the person
    has closed no day at all), and `ratings` the recent effort ratings, newest first.

    `{code, exercise_id, effort: {typical, rated_sessions}, adherence_percent,
    direction, sessions, latest, next, needs}` — `next` is null for `hold` and
    `not_enough_data`, and `needs` is null unless the answer is `not_enough_data`.
    """
    summary = trend_summary or {}
    trained = int(summary.get('sessions') or 0)
    rated = [r for r in (ratings or []) if r]
    typical = round(_median(rated), 1) if rated else None
    latest = summary.get('latest') or None
    direction = summary.get('direction') or 'steady'

    body = {
        'exercise_id': exercise_id,
        'effort': {'typical': typical, 'rated_sessions': len(rated)},
        'adherence_percent': adherence_percent,
        'direction': direction,
        'sessions': trained,
        'latest': ({'weight': latest.get('weight'), 'reps': latest.get('reps')}
                   if latest else None),
        'next': None,
        'needs': None,
    }

    if trained < MIN_SESSIONS or len(rated) < MIN_RATED:
        body['code'] = 'not_enough_data'
        body['needs'] = {'sessions': max(MIN_SESSIONS - trained, 0),
                         'rated_sessions': max(MIN_RATED - len(rated), 0)}
        return body

    if typical >= DELOAD_EFFORT or (typical >= HARD_EFFORT and direction == 'easing'):
        code = 'deload'
    elif (adherence_percent is not None and adherence_percent < MIN_ADHERENCE) \
            or typical >= HARD_EFFORT:
        code = 'hold'
    elif typical <= EASY_EFFORT:
        code = 'add_weight'
    else:
        code = 'add_reps'
    body['code'] = code
    body['next'] = _next_step(code, body['latest'])
    return body


def suggest_many(user, summaries, *, adherence_percent=None, until=None) -> dict:
    """`{exercise_id: suggestion}` for a list of `trend`/`overview` rows, in one extra
    query for the effort of all of them together."""
    rows = [row for row in (summaries or []) if row and row.get('exercise_id')]
    ids = [row['exercise_id'] for row in rows]
    effort = recent_effort(user, ids, until=until)
    return {row['exercise_id']: suggest(row['exercise_id'], trend_summary=row,
                                        adherence_percent=adherence_percent,
                                        ratings=effort.get(row['exercise_id'], []))
            for row in rows}


def for_exercise(user, exercise_id, *, trend_summary, adherence_percent=None,
                 until=None) -> dict:
    """`suggest` for one exercise, reading its effort ratings itself."""
    effort = recent_effort(user, [exercise_id], until=until)
    return suggest(exercise_id, trend_summary=trend_summary,
                   adherence_percent=adherence_percent,
                   ratings=effort.get(int(exercise_id), []))
