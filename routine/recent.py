"""The last seven days, as the week endpoint and the home screen both read them.

This was the body of `RecentActivityProgressView` and it now has a name, because the
home screen (`home/`) shows the same week, the same adherence block and the same
strength headline. A second implementation would answer different numbers from the
endpoint the app already knows, on the same screen, in the same second.

One payload, one cache entry (`recent_progress:{user}:{language}`, private cache, 120 s)
and one bust (`training_platform/signals.py recent_progress_cache_keys`, fired when a
workout completes and when a day closes). Whichever of the two endpoints is hit first
pays for it and the other is free until the next change.

The language is part of the key because the payload carries translated exercise names
(the strength headline); a cached Arabic answer was once served to the same user's
English session for two minutes.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.db.models import DurationField, ExpressionWrapper, F, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.translation import get_language

#: How long a built week stays good. The app is told the same number
#: (`refresh_after_seconds` on the home payload), so it does not poll faster than this.
CACHE_SECONDS = 120

#: How many exercises the strength headline is chosen from. Each one costs a query, and
#: the headline is a single line: six is plenty to find the one that moved most.
STRENGTH_LIMIT = 6


def cache_key(user_id, language=None) -> str:
    """The one key this payload is cached under for this user and this language.

    `training_platform/signals.py recent_progress_cache_keys` builds the same string for
    every language the platform serves; the two must not drift apart.
    """
    return f"recent_progress:{user_id}:{language or get_language() or 'ar'}"


def recent_progress(user, *, now=None) -> dict:
    """The seven-day payload: durations, the adherence block and the strength headline.

    Uncached — `cached_recent_progress` is what a view wants. Costs one aggregate for the
    durations, one for the week's outcomes, the streak's two, and one per exercise the
    strength overview looks at.
    """
    from . import strength as strength_module
    from .models import WorkoutSession
    from .outcomes import adherence

    now = now or timezone.localtime()
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = start_of_today - timedelta(days=6)

    # DB-level aggregation: push all computation to Postgres.
    qs = (
        WorkoutSession.objects
        .filter(
            user=user,
            status='completed',
            end_time__isnull=False,       # guard against active/null sessions
            start_time__gte=window_start,
        )
        # NOT `day=`: `WorkoutSession.day` is the routine day, and annotating over a
        # model field raises `ValueError: The annotation 'day' conflicts with a field
        # on the model` — this payload, the home screen's week, answered 500 to every
        # caller from the day that column was added.
        .annotate(
            session_date=TruncDate('start_time'),
            session_duration=ExpressionWrapper(
                F('end_time') - F('start_time'),
                output_field=DurationField()
            )
        )
        .values('session_date')
        .annotate(total_duration=Sum('session_duration'))
    )

    results_map = {}
    for entry in qs:
        total_secs = entry['total_duration'].total_seconds() if entry['total_duration'] else 0
        results_map[entry['session_date']] = int(total_secs)

    # Deterministic 7-day output — always include every day, even zeros.
    days = []
    for i in range(7):
        day: date = (window_start + timedelta(days=i)).date()
        days.append({
            "date": day.isoformat(),          # "2026-04-11" — clean, unambiguous
            "duration": results_map.get(day, 0),
        })

    # The same week, but as the plan: how much of what the coach asked for was actually
    # done, how many days had nothing missing, and the day that broke the streak (the
    # owner, 2026-09-20).
    week = adherence(user, window_start.date(), start_of_today.date(),
                     today=start_of_today.date())
    # One line for the home card: the exercise that moved most, and where it is heading
    # if there is enough to say (the owner, 2026-09-20). Numbers, not a sentence: the app
    # writes it in the person's own language.
    overview = strength_module.overview(user, until=start_of_today.date(),
                                        limit=STRENGTH_LIMIT,
                                        adherence_percent=week['sets']['done_percent'])
    return {
        "recent_progress": days,
        "adherence": week,
        "strength": {
            "headline": strength_module.headline(
                overview['top_movers'][0] if overview['top_movers']
                else (overview['exercises'][0] if overview['exercises'] else None)),
            "rising_count": overview['rising_count'],
            "tracked_count": overview['tracked_count'],
        },
    }


def cached_recent_progress(user, *, now=None) -> dict:
    """`recent_progress`, from the private cache when it is there.

    A cache that cannot be reached is not an outage: the payload is rebuilt and the
    write is skipped. Reading and writing it must never be able to fail the screen.
    """
    from training_platform.cache import private_cache

    key = cache_key(user.pk)
    try:
        cached = private_cache().get(key)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    data = recent_progress(user, now=now)
    try:
        private_cache().set(key, data, CACHE_SECONDS)
    except Exception:
        pass
    return data
