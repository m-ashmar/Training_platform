"""Retention enforcement.

Only `AITrainingData` had a purge before this; `UserActivity` and `UserSession` carry an
IP address and user agent and were kept forever. The registry now gives every source a
window, and this one task applies all of them.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name="training_platform.privacy.purge_expired_personal_data",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def purge_expired_personal_data():
    """Delete rows past the retention window declared for their source."""
    from .registry import purge_expired

    removed = purge_expired()
    if removed:
        logger.info("Retention purge removed: %s", removed)
    return removed or "nothing expired"
