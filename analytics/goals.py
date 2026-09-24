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

**Its target comes from the plan, too** (the owner's decision 5, 2026-09-24: "I would
not allow users to manually enter a conflicting number. The system should derive the
goal from the user's plan/profile and own the calculation"). `weekly_targets` answers
it, at read time like the count, in this order:

1. `program` — the routine the home screen shows (`home/sections.py current_routine`),
   when it asks for work this week: the days of this week inside its window whose day
   of the cycle has exercises (`home/sections.py planned_training_days`) — the same
   number as the home's `week_stats.workouts.planned`, so the two never disagree. It can
   differ from week to week: a program that starts on a Wednesday asks for fewer days
   in its first week.
2. `profile` — else the training days a week the person gave at onboarding
   (`CustomUser.training_days_per_week`, 1-7).
3. Neither: there is no number the system could own. Creating a weekly goal is then
   REFUSED (`goal_target_unavailable`) rather than taking a typed one — the app sends
   the person to set their training days (or to their coach for a program), and the
   goal follows from that. An existing weekly goal whose person has since lost both is
   read at the target it was last calculated at (`target_source: "saved"`).

A target the client sends that differs from the calculated one is refused
(`goal_target_from_plan`, carrying `expected_target` and `target_source`); the same
number, or none, is accepted, and the calculated number is what is stored. The stored
column is only a snapshot: what is SERVED is recalculated on every read.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta

from django.db.models import Count
from django.utils import timezone

WORKOUT_FREQUENCY = 'workout_frequency'

#: The goal types whose progress the server counts; `update_progress` refuses them.
COUNTED_TYPES = frozenset({WORKOUT_FREQUENCY})

#: Where a weekly goal's target came from (`target_source` on the goal).
FROM_PROGRAM = 'program'
FROM_PROFILE = 'profile'
#: Neither is available now: the target it was last calculated at.
FROM_SAVED = 'saved'


class GoalTargetRefused(Exception):
    """A weekly goal's target the system cannot take: 400 with its own `code`.

    Raised by `UserGoalSerializer.validate` and answered by `UserGoalViewSet` as
    `{"error", "detail", "code", **extra}` — a DRF ValidationError would turn
    `expected_target` into a string.
    """

    def __init__(self, code, message, **extra):
        super().__init__(code)
        self.code, self.message, self.extra = code, message, extra


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


def weekly_targets(user_ids, today=None) -> dict:
    """{user_id: (target, source)} — what each person's weekly workout goal aims at THIS
    week, by the rule in the module docstring; `(None, None)` for someone with neither a
    program that asks for work this week nor a stated number of training days.

    Three queries for any number of people: their active routines (newest first, the
    order `home/sections.py current_routine` reads), the days of the chosen routines'
    cycles that have exercises, and the stated training days.
    """
    from home.sections import ROUTINES_CONSIDERED, pick_current_routine, planned_training_days
    from routine.models import Routine, RoutineExercise
    from users.models import CustomUser

    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return {}
    today = today or timezone.localdate()
    first, _last = current_week(today)

    through = Routine.assigned_to.through
    person = Routine._meta.get_field('assigned_to').m2m_reverse_field_name()
    candidates: dict = {}
    for link in (through.objects
                 .filter(**{f'{person}_id__in': ids}, routine__is_active=True)
                 .select_related('routine')
                 .order_by(f'{person}_id', '-routine__start_date', '-routine_id')):
        rows = candidates.setdefault(getattr(link, f'{person}_id'), [])
        if len(rows) < ROUTINES_CONSIDERED:
            rows.append(link.routine)
    chosen = {uid: pick_current_routine(rows, today) for uid, rows in candidates.items()}

    working: dict = {}
    routine_ids = {routine.pk for routine in chosen.values() if routine is not None}
    if routine_ids:
        for routine_id, day in (RoutineExercise.objects.filter(routine_id__in=routine_ids)
                                .values_list('routine_id', 'day').distinct()):
            working.setdefault(routine_id, set()).add(day)

    stated = dict(CustomUser.objects.filter(pk__in=ids)
                  .values_list('pk', 'training_days_per_week'))

    answer = {}
    for uid in ids:
        routine = chosen.get(uid)
        planned = (planned_training_days(routine, first, working.get(routine.pk, set()))
                   if routine is not None else 0)
        days = stated.get(uid)
        if planned > 0:
            answer[uid] = (planned, FROM_PROGRAM)
        elif isinstance(days, int) and 1 <= days <= 7:
            answer[uid] = (days, FROM_PROFILE)
        else:
            answer[uid] = (None, None)
    return answer


def weekly_target(user_id, today=None):
    """`weekly_targets` for one person: `(target, source)` or `(None, None)`."""
    return weekly_targets([user_id], today).get(user_id, (None, None))
