"""Scheduled learning."""
import logging

from celery import shared_task

from training_platform.task_retries import no_retry_on_timeout

logger = logging.getLogger(__name__)


@shared_task(
    name="diet.planner.refresh_food_weights",
    # Not a bare `(Exception,)`: that also retries `SoftTimeLimitExceeded`, so a
    # job too big for its 300 s slot burned four consecutive slots and still
    # never finished. See training_platform/task_retries.py.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def refresh_food_weights(after=None):
    """Nightly: turn what users actually ate into planner ranking weights.

    A page of users per invocation (`training_platform.batching`), the rest handed on to
    a fresh invocation: every user with a diet history in one task under the 300 s soft
    limit was cut off part-way once the platform grew, and the later users never learned
    anything (the production-readiness audit, 2026-09-25). `update_weights` recomputes a
    user's weights from their history, so a redelivered page changes nothing.
    """
    from django.contrib.auth import get_user_model

    from training_platform.batching import Batch

    from .learning import update_weights

    from django.db import InterfaceError, OperationalError

    User = get_user_model()
    total = 0
    # Only people with a diet history: every trainer, admin and client who never took a
    # plan cost two queries a night for nothing.
    batch = Batch()
    users = batch.page(User.objects.filter(is_active=True, diet_plans__isnull=False)
                       .distinct(), after)
    for user in users:
        if batch.spent():
            break
        batch.done(user.pk)
        try:
            total += len(update_weights(user))
        except (OperationalError, InterfaceError):
            # The database, not this user. Swallowed per user, an outage logged one
            # traceback per client and the task reported success, so the retry
            # configured above never ran.
            raise
        except Exception:
            logger.exception("Weight refresh failed for user %s", user.pk)
    batch.hand_on(refresh_food_weights)
    return f"adjusted {total} food weights"
