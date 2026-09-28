"""Is the background side alive — beat, the broker, and a worker that finishes jobs?

WHY THIS EXISTS. The worker and beat that move money were one unmonitored process
(the production-readiness audit, 2026-09-25). In reconcile mode the payment poller is
the only thing that completes a ShamCash checkout; the hourly renewal charges coaching
months and freezes the unpaid ones; the payout backstop pays trainers; the expiry sweep
ends lapsed plans. If that side stops — beat crashed, the worker OOM-looping, the broker
gone — paid checkouts stay pending and nothing anywhere says so: the web machine's
health check stays green, because the web machine is fine.

WHAT IT RECORDS, in `ops_cache()` (Redis DB0 in production, shared by every machine):

* `HEARTBEAT_KEY` — written by `background_heartbeat`, a beat entry every minute. Beat
  must schedule it, the broker must carry it and a worker must run it, so a fresh value
  proves all three.
* `SUCCESS_KEY` per watched job — written by the `task_success` signal
  (`training_platform/celery.py`) whenever a task that a watched beat entry schedules
  finishes without raising. A heartbeat does not prove the poller still completes: a
  job that raises every minute leaves the heartbeat perfectly healthy.
* `WATCH_KEY` per watched job — the first time the heartbeat saw the job being watched.
  A job with no success yet is "waiting" until it has been watched for its whole
  allowance, then "stale"; without this a job that is never registered, or never
  succeeds once, would read "waiting" for ever and never alert.

WHO READS IT. `GET /api/auth/health/background/` (`users/health_views.py`), for an
external monitor to poll. It is deliberately NOT the platform's machine health check:
a machine whose check fails is restarted, and a late beat must never restart the web
machines that are serving users. See fly.toml.

The watched jobs and their allowances are `settings.BACKGROUND_WATCHED_JOBS` (the same
list gets Sentry cron monitors when SENTRY_DSN is set — `training_platform/
observability.py`); the heartbeat's allowance is `BACKGROUND_HEARTBEAT_MAX_AGE_SECONDS`.
"""
import logging
import time

from celery import shared_task
from django.conf import settings

from training_platform.cache import ops_cache

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "bg:heartbeat"
SUCCESS_KEY = "bg:last_success:{}"
WATCH_KEY = "bg:watch_since:{}"

#: How long a marker outlives its writer. Long enough that a week-old value still reads
#: as "stale" (which alerts) rather than vanishing into "missing"; short enough that a
#: renamed job does not leave a key behind for ever.
MARKER_TTL_SECONDS = 30 * 24 * 3600


def watched_jobs():
    """{beat entry name: seconds a job may go without a success}."""
    return dict(getattr(settings, "BACKGROUND_WATCHED_JOBS", {}) or {})


def _entries_for_task(task_name):
    """The watched beat entries that schedule the task called `task_name`."""
    if not task_name:
        return []
    schedule = getattr(settings, "CELERY_BEAT_SCHEDULE", {}) or {}
    return [entry for entry in watched_jobs()
            if (schedule.get(entry) or {}).get("task") == task_name]


@shared_task(name="training_platform.background_heartbeat", ignore_result=True)
def background_heartbeat():
    """Write "the background side ran a job just now", and start any unstarted watch clock."""
    now = time.time()
    cache = ops_cache()
    cache.set(HEARTBEAT_KEY, now, timeout=MARKER_TTL_SECONDS)
    for entry in watched_jobs():
        # `add` writes only when the key is absent, so the clock starts once and is
        # never restarted by later heartbeats.
        cache.add(WATCH_KEY.format(entry), now, timeout=MARKER_TTL_SECONDS)
    return None


def record_success(task_name):
    """Called from the `task_success` signal for EVERY task: cheap when not watched."""
    entries = _entries_for_task(task_name)
    if not entries:
        return
    now = time.time()
    try:
        ops_cache().set_many({SUCCESS_KEY.format(e): now for e in entries},
                             timeout=MARKER_TTL_SECONDS)
    except Exception:
        # The job itself succeeded; failing it because Redis hiccuped would turn a
        # monitoring blip into a money one. The route reports the store as unreachable.
        logger.warning("could not record the success of %s", task_name, exc_info=True)


def _job_state(now, last_success, watched_since, max_age):
    if last_success is not None:
        age = max(0, int(now - float(last_success)))
        return {"status": "ok" if age <= max_age else "stale", "age_seconds": age,
                "max_age_seconds": max_age}
    if watched_since is not None and now - float(watched_since) > max_age:
        return {"status": "stale", "age_seconds": None, "max_age_seconds": max_age}
    return {"status": "waiting", "age_seconds": None, "max_age_seconds": max_age}


def report(now=None):
    """(http status, body) for the background health route. Reads only; never writes."""
    now = time.time() if now is None else now
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        # Development and the test suite run every task inline, in the request. There is
        # no worker or beat whose silence could mean anything.
        return 200, {"status": "inline",
                     "detail": "tasks run inline; there is no worker or beat to watch"}

    jobs = watched_jobs()
    keys = [HEARTBEAT_KEY] + [SUCCESS_KEY.format(e) for e in jobs] \
        + [WATCH_KEY.format(e) for e in jobs]
    try:
        values = ops_cache().get_many(keys)
    except Exception:
        logger.warning("background health: the marker store is unreachable", exc_info=True)
        return 503, {"status": "unreachable",
                     "detail": "the store the background side reports to is unreachable"}

    beat_max = int(getattr(settings, "BACKGROUND_HEARTBEAT_MAX_AGE_SECONDS", 180))
    beat = values.get(HEARTBEAT_KEY)
    if beat is None:
        heartbeat = {"status": "missing", "age_seconds": None, "max_age_seconds": beat_max}
    else:
        age = max(0, int(now - float(beat)))
        heartbeat = {"status": "ok" if age <= beat_max else "stale", "age_seconds": age,
                     "max_age_seconds": beat_max}

    states = {entry: _job_state(now, values.get(SUCCESS_KEY.format(entry)),
                                values.get(WATCH_KEY.format(entry)), int(max_age))
              for entry, max_age in jobs.items()}
    healthy = heartbeat["status"] == "ok" and all(
        s["status"] != "stale" for s in states.values())
    body = {"status": "ok" if healthy else "stale", "heartbeat": heartbeat, "jobs": states}
    return (200 if healthy else 503), body


def forget_all():
    """Delete every marker. For tests, and for an operator resetting a replaced Redis."""
    keys = [HEARTBEAT_KEY] + [k.format(e) for e in watched_jobs()
                              for k in (SUCCESS_KEY, WATCH_KEY)]
    ops_cache().delete_many(keys)
