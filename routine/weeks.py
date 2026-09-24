"""One week of a routine written onto another week of the same routine (BACKEND_BUGS
B-103; TrainerWeek's «التدرّج التلقائي»: copy this week to the next with +2.5 % or +5 %
on every load, one tap).

Weeks. A routine's days are numbered 1..`days`, and week `w` is days
`(w - 1) * 7 + 1` .. `w * 7` — the calendar weeks TrainerWeek draws. Only WHOLE weeks
exist: a 10-day routine has one week, and its days 8-10 are not a week to copy into.

What is copied. Every row of the source week, moved by the same number of days, with
its sets, reps, rest, duration, distance, note, superset number and load mode. The
LOADS are multiplied by `load_factor` and rounded to the nearest 0.5 kg (the smallest
plate step): `weight` always, and `load_value` in `percent` mode (a percentage of the
one-rep max is a load too), to the nearest 0.5 %. An RPE is an effort, not a load: it
is copied as written. A factor of exactly 1 («نفس الأوزان») copies every number as
written, unrounded. Day names travel with their days and never replace a name the
target day already has.

One write. The routine row is locked, the target week must hold no rows, and the rows
are written in one `bulk_create` inside one transaction: the whole week or nothing.
A copy that would push any load past what a row may hold (`weight` over
`MAX_WEIGHT_KG`, a percentage over 100) is refused whole, before anything is written.

Refusing, not replacing, a filled target week is deliberate: a week the trainer already
wrote is work, and a tap that silently deleted it could not be undone. To redo a week,
the app deletes its rows first (`DELETE routine-exercises/{id}/`), then copies.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.utils.translation import gettext as _

DAYS_PER_WEEK = 7
#: The multiplier a copy accepts, inclusive: from a 50 % deload to a 50 % jump. The
#: design offers 1.0, 1.025 and 1.05; the bounds only refuse a typo (owner to confirm).
LOAD_FACTOR_MIN = Decimal('0.5')
LOAD_FACTOR_MAX = Decimal('1.5')
#: Loads are rounded to this step after multiplying: the smallest plate a gym has.
LOAD_STEP = Decimal('0.5')
#: The heaviest `weight` a routine row takes (`RoutineExerciseSerializer.validate_weight`).
MAX_WEIGHT_KG = Decimal('1000')


class WeekCopyRefused(Exception):
    """A refusal with its HTTP status, its own `code`, a translated sentence and any
    extra keys the answer carries (`field`, `weeks`, `to_week`)."""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self):
        return {'error': str(self.message), 'code': self.code, **self.extra}


def weeks_in(routine) -> int:
    """How many whole weeks `routine` has."""
    return (routine.days or 0) // DAYS_PER_WEEK


def week_days(week) -> range:
    return range((week - 1) * DAYS_PER_WEEK + 1, week * DAYS_PER_WEEK + 1)


def parse_week(raw, field):
    """A week number from a request body: a whole number from 1. 400 `week_invalid`
    naming the field otherwise (missing, text, 0, 1.5, true)."""
    value = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, str) and raw.strip().isascii() and raw.strip().isdigit():
        value = int(raw.strip())
    if value is None or value < 1:
        raise WeekCopyRefused(400, 'week_invalid',
                              _('%(field)s must be a week number from 1.') % {'field': field},
                              field=field)
    return value


def parse_load_factor(raw):
    """The multiplier, as a Decimal; 1 when not sent. 400 `load_factor_invalid` for
    anything but a number, `load_factor_out_of_range` outside the bounds."""
    if raw is None:
        return Decimal('1')
    try:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise InvalidOperation
        factor = Decimal(str(raw).strip())
        if not factor.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise WeekCopyRefused(400, 'load_factor_invalid',
                              _('load_factor must be a number, such as 1.025.'),
                              field='load_factor')
    if not LOAD_FACTOR_MIN <= factor <= LOAD_FACTOR_MAX:
        raise WeekCopyRefused(
            400, 'load_factor_out_of_range',
            _('load_factor must be from %(low)s to %(high)s.')
            % {'low': LOAD_FACTOR_MIN, 'high': LOAD_FACTOR_MAX},
            field='load_factor')
    return factor


def _scaled(value, factor):
    """`value` x `factor`, to the nearest `LOAD_STEP` (a half rounds up)."""
    product = Decimal(str(value)) * factor
    steps = (product / LOAD_STEP).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return float(steps * LOAD_STEP)


def copy_week(routine, from_week, to_week, factor):
    """Write week `from_week` of `routine` onto the empty week `to_week`.

    Returns `(rows, day_names)`: the new `RoutineExercise` rows and the routine's day
    names as they now stand. Raises `WeekCopyRefused`: 400 `week_out_of_range` (with
    `field` and `weeks`), `same_week`, `week_empty` (the source week has no rows),
    `load_out_of_range`; 409 `week_not_empty` (with `to_week`).
    """
    from .models import Routine, RoutineExercise, load_value_problem, merge_day_names

    if from_week == to_week:
        raise WeekCopyRefused(400, 'same_week', _('Choose two different weeks.'))

    with transaction.atomic():
        # The lock serialises two copies into the same week, and a copy against an edit
        # of `days`: each sees the other's rows or none of them.
        locked = Routine.objects.select_for_update().get(pk=routine.pk)
        weeks = weeks_in(locked)
        for field, week in (('from_week', from_week), ('to_week', to_week)):
            if week > weeks:
                raise WeekCopyRefused(
                    400, 'week_out_of_range',
                    _('This routine has %(weeks)s whole weeks.') % {'weeks': weeks},
                    field=field, weeks=weeks)

        offset = (to_week - from_week) * DAYS_PER_WEEK
        source = list(RoutineExercise.objects
                      .filter(routine=locked, day__in=week_days(from_week))
                      .order_by('day', 'order', 'id'))
        if not source:
            raise WeekCopyRefused(400, 'week_empty',
                                  _('Week %(week)s has no exercises to copy.')
                                  % {'week': from_week}, from_week=from_week)
        if RoutineExercise.objects.filter(routine=locked,
                                          day__in=week_days(to_week)).exists():
            raise WeekCopyRefused(409, 'week_not_empty',
                                  _('Week %(week)s already has exercises. Clear it first.')
                                  % {'week': to_week}, to_week=to_week)

        same = factor == 1
        rows = []
        for rex in source:
            weight, value = rex.weight, rex.load_value
            if not same:
                if weight is not None:
                    weight = _scaled(weight, factor)
                if rex.load_mode == 'percent' and value is not None:
                    value = _scaled(value, factor)
            too_heavy = weight is not None and not 0 <= Decimal(str(weight)) <= MAX_WEIGHT_KG
            if too_heavy or load_value_problem(rex.load_mode, value) is not None:
                raise WeekCopyRefused(
                    400, 'load_out_of_range',
                    _('Day %(day)s: the new load is more than an exercise may hold. '
                      'Use a smaller increase.') % {'day': rex.day + offset},
                    day=rex.day + offset)
            rows.append(RoutineExercise(
                routine=locked, exercise_id=rex.exercise_id, day=rex.day + offset,
                order=rex.order, sets=rex.sets, reps=rex.reps, rest_time=rex.rest_time,
                duration=rex.duration, distance=rex.distance, notes=rex.notes,
                weight=weight, superset_group=rex.superset_group,
                load_mode=rex.load_mode, load_value=value))
        created = RoutineExercise.objects.bulk_create(rows)

        names = locked.day_names or {}
        moved = {str(int(day) + offset): name for day, name in names.items()
                 if str(day).isdigit() and int(day) in week_days(from_week)}
        day_names = merge_day_names(names, moved, locked.days, overwrite=False)
        if day_names != names:
            Routine.objects.filter(pk=locked.pk).update(day_names=day_names)
    return created, day_names
