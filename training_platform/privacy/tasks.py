"""Retention enforcement.

Only `AITrainingData` had a purge before this; `UserActivity` and `UserSession` carry an
IP address and user agent and were kept forever. The registry now gives every source a
window, and this one task applies all of them.
"""
import logging

from celery import shared_task

from training_platform.task_retries import no_retry_on_timeout

logger = logging.getLogger(__name__)


@shared_task(
    name="training_platform.privacy.purge_expired_personal_data",
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
def purge_expired_personal_data():
    """Delete rows past the retention window declared for their source."""
    from .registry import purge_expired

    removed = purge_expired()
    if removed:
        logger.info("Retention purge removed: %s", removed)
    return removed or "nothing expired"


@shared_task(
    name="training_platform.privacy.flush_expired_tokens",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def flush_expired_tokens():
    """Delete refresh tokens that can no longer be used.

    `ROTATE_REFRESH_TOKENS` and `BLACKLIST_AFTER_ROTATION` are both on, so every refresh
    writes an `OutstandingToken` and a `BlacklistedToken` — and nothing ever removed
    them. simplejwt ships `flushexpiredtokens` for precisely this and it was in no
    schedule, no Dockerfile and no checklist. The row holds the whole RS256 token in a
    text column, about 869 bytes: one active mobile client rotating hourly is 25 rows a
    day, so 10,000 daily users is 250,000 rows and 217 MB of heap per day.

    The blacklist rows go first, because they reference the outstanding rows.
    """
    from django.utils import timezone

    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken, OutstandingToken)

    now = timezone.now()
    blacklisted, _d = BlacklistedToken.objects.filter(
        token__expires_at__lt=now).delete()
    outstanding, _d = OutstandingToken.objects.filter(expires_at__lt=now).delete()
    if blacklisted or outstanding:
        logger.info("Flushed %d blacklisted and %d outstanding expired token(s)",
                    blacklisted, outstanding)
    return {"blacklisted": blacklisted, "outstanding": outstanding}
