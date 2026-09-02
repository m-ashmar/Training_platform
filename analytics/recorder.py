"""Server-side analytics recording.

`UserActivity`, `PerformanceMetric`, `UserSession` and `UserGoal` were read in 37 places
and written by the server in **none** — every row depended on the mobile client choosing
to POST it. That would be a defensible design decision except that other features
silently depend on the data:

    achievements/engine.py:130,137   reads UserActivity      -> activity achievements
    achievements/engine.py:276,328   reads PerformanceMetric -> weight-loss achievements
    achievements/engine.py:168       reads UserGoal          -> goal achievements
    achievements/signals.py:74,99    post_save on both       -> receivers never fired

So a whole class of achievements could never award, and nothing said so. This module
records the events the server already knows about, at the moment they happen.
"""
from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


def record_activity(user, activity_type: str, **details) -> None:
    """Write one UserActivity row. Never raises — analytics must not break a save."""
    from analytics.models import UserActivity

    if user is None or not getattr(user, "pk", None):
        return
    try:
        UserActivity.objects.create(
            user=user,
            activity_type=activity_type,
            session_id=str(details.pop("session_id", "") or uuid.uuid4()),
            user_agent=str(details.pop("user_agent", "") or "server"),
            ip_address=details.pop("ip_address", None),
            metadata=details or {},
        )
    except Exception:
        logger.debug("could not record %s for user %s", activity_type,
                     getattr(user, "pk", "?"), exc_info=True)


def record_metric(user, metric_type: str, value: float, unit: str = "", **extra) -> None:
    """Write one PerformanceMetric row — the series the weight achievements read."""
    from analytics.models import PerformanceMetric

    if user is None or not getattr(user, "pk", None) or value is None:
        return
    try:
        PerformanceMetric.objects.create(
            user=user, metric_type=metric_type, value=float(value),
            unit=unit or _default_unit(metric_type), notes=str(extra.get("notes", "")),
        )
    except Exception:
        logger.debug("could not record metric %s for user %s", metric_type,
                     getattr(user, "pk", "?"), exc_info=True)


def _default_unit(metric_type: str) -> str:
    return {
        "weight": "kg", "body_fat": "%", "muscle_mass": "kg",
        "workout_duration": "min", "calories_burned": "kcal",
    }.get(metric_type, "")
