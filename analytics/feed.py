"""What an activity row says about the workout it records (BACKEND_BUGS B-072).

A `routine_completed` row carried only `routine_id` and `session_id`, so the activity log
could not draw «47 دقيقة · 5,480 كغ». The figures are READ from the session when the row
is shown, not copied into the row when it is written: a workout synced offline arrives
completed before its sets do, and a copy taken at that moment would say 0 kg for ever.
Read, it is right whichever order the phone sent things in — and rows written before
this existed show the same figures.

One query for a whole page, however many workouts it holds.
"""
from __future__ import annotations

from django.db.models import Count, F, FloatField, Sum

from .activity_types import ROUTINE_COMPLETED


def _session_pk(activity):
    if activity.activity_type != ROUTINE_COMPLETED:
        return None
    try:
        return int(str(activity.session_id).strip())
    except (TypeError, ValueError):
        return None


def workout_details(activities) -> dict:
    """{session pk: {session_id, routine_id, day, duration_minutes, volume_kg, sets}}.

    `volume_kg` is weight x reps summed over the session's sets, the figure
    `analytics/summary/` and `routine/outcomes.py` count; `duration_minutes` is whole
    minutes from start to end, null while the session has no end. A session since
    deleted is simply absent.
    """
    from routine.models import WorkoutSession

    pks = {pk for pk in (_session_pk(a) for a in activities) if pk is not None}
    if not pks:
        return {}
    rows = (WorkoutSession.objects.filter(pk__in=pks)
            .annotate(volume=Sum(F('set_logs__weight') * F('set_logs__reps'),
                                 output_field=FloatField()),
                      set_count=Count('set_logs'))
            .values('pk', 'routine_id', 'day', 'start_time', 'end_time', 'volume',
                    'set_count'))
    details = {}
    for row in rows:
        minutes = None
        if row['start_time'] and row['end_time']:
            minutes = max(0, round((row['end_time'] - row['start_time']).total_seconds()
                                   / 60))
        details[row['pk']] = {
            'session_id': row['pk'],
            'routine_id': row['routine_id'],
            'day': row['day'],
            'duration_minutes': minutes,
            'volume_kg': round(float(row['volume'] or 0), 2),
            'sets': row['set_count'],
        }
    return details


def workout_for(activity, details):
    pk = _session_pk(activity)
    return details.get(pk) if pk is not None else None
