"""Goals the server counts itself, and where a goal starts from.

`workout_frequency` ("train 4 times a week") had nothing moving it: `current_value`
changed only when the app posted a number by hand, and nothing reset it, so a goal met
once stayed met for ever (BACKEND_BUGS B-070). It is now COUNTED, at read time, from the
person's own workout sessions — the same rows every other training screen reads — so it
is right the instant a workout is finished and starts again from 0 every Saturday with
no job having to run.

The rule, in one place:

* **A workout** is a `routine.WorkoutSession` with `status='completed'`. An open, a
  forgotten or an abandoned one is not a workout done.
* **This week** runs Saturday to Friday, the week of the home screen's strip
  (`home/sections.py` `week_start`), on the platform's calendar (`timezone.localdate()`,
  which is what home reads). A session belongs to the week its `start_time` falls in.
* **The goal is about THIS week.** `current_value` is this week's count and
  `progress_percentage` is that count against `target_value`, capped at 100. A weekly
  goal is never `completed`: meeting this week's number does not end it, and next week
  starts again from 0.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta

from django.db.models import Count
from django.utils import timezone

WORKOUT_FREQUENCY = 'workout_frequency'

#: The goal types whose progress the server counts; `update_progress` refuses them.
COUNTED_TYPES = frozenset({WORKOUT_FREQUENCY})


def current_week(today=None):
    """(first, last) date of this week: Saturday to Friday."""
    from home.sections import week_start

    today = today or timezone.localdate()
    first = week_start(today)
    return first, first + timedelta(days=6)


def workouts_this_week(user_ids, today=None) -> dict:
    """{user_id: completed workout sessions begun this week}, in one query.

    Bounded by an aware datetime range on `start_time` rather than `start_time__date`, so
    the `(user, status, start_time)` index serves it.
    """
    from routine.models import WorkoutSession

    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return {}
    first, _last = current_week(today)
    begins = timezone.make_aware(datetime.combine(first, time.min))
    ends = timezone.make_aware(datetime.combine(first + timedelta(days=7), time.min))
    counts = {uid: 0 for uid in ids}
    rows = (WorkoutSession.objects
            .filter(user_id__in=ids, status='completed',
                    start_time__gte=begins, start_time__lt=ends)
            .values('user_id').annotate(n=Count('id')))
    for row in rows:
        counts[row['user_id']] = row['n']
    return counts


def weekly_progress(count, target) -> float:
    """This week's count against the target, 0-100."""
    if not target or target <= 0:
        return 0.0
    return max(0.0, min(100.0, count / target * 100))


def start_from_metadata(metadata, target):
    """The start the app kept in `metadata.start_value` before `start_value` existed.

    A real, finite number that is not the target itself; anything else is None. The
    migration that added the column read existing rows by the same rule.
    """
    if not isinstance(metadata, dict):
        return None
    value = metadata.get('start_value')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    if target is not None and value == float(target):
        return None
    return value
