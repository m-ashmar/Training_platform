"""What a Celery task should and should not retry.

`autoretry_for=(Exception,)` reads as "retry anything", and it does — including
`SoftTimeLimitExceeded`, which Celery raises INSIDE the task when it overruns
`CELERY_TASK_SOFT_TIME_LIMIT` (300 s here). That is not a transient fault: the work did
not fit in the slot, and every retry restarts from scratch and overruns at the same
point. With `max_retries=3`, a job too big for its window burns four consecutive
300-second slots on a worker that runs at `--concurrency=2` — half the worker occupied
for twenty minutes, four times a day, still never finishing, while the reminder sweep,
the dead-letter drain and the subscription expiry queue behind it.

Celery's `autoretry_for` takes exception classes, not a predicate, so the exclusion has
to live inside the task. `@no_retry_on_timeout` sits under `@shared_task` and converts
the overrun into `Ignore`, which Celery records without retrying.
"""

import functools
import logging

from celery.exceptions import Ignore, SoftTimeLimitExceeded

logger = logging.getLogger(__name__)


def no_retry_on_timeout(fn):
    """Let a task fail once when it overruns, instead of three more times."""

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SoftTimeLimitExceeded:
            logger.error(
                "%s exceeded CELERY_TASK_SOFT_TIME_LIMIT; not retrying — retrying "
                "restarts the same work and overruns at the same point.",
                getattr(fn, "__name__", "task"),
            )
            raise Ignore()

    return inner
