"""The client's own calendar day.

`timezone.localtime()` resolves against `TIME_ZONE`, which is Asia/Damascus for the whole
platform. Anything that means "today" for a PERSON — a daily allowance, a streak, one
piece of advice per day — has to ask that person's timezone, or it rolls over at a random
hour of their afternoon. `CustomUser.preferred_timezone` is the platform's per-user truth;
`notifications/tasks.py` already schedules the daily reminder from it.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def day_start_for(user, now=None):
    """Midnight for `user`, as an aware datetime. Falls back to the server's day."""
    now = now or timezone.now()
    name = getattr(user, "preferred_timezone", None)
    if name:
        try:
            from zoneinfo import ZoneInfo

            return now.astimezone(ZoneInfo(name)).replace(
                hour=0, minute=0, second=0, microsecond=0)
        except Exception:
            logger.warning("user %s has an unusable timezone %r; using the server day",
                           getattr(user, "pk", None), name)
    return timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)


def today_for(user, now=None):
    """The client's calendar date."""
    return day_start_for(user, now).date()
