"""Retention for replaced identity documents (`services.purge_replaced_documents`)."""
import logging

from celery import shared_task

from training_platform.task_retries import no_retry_on_timeout

logger = logging.getLogger(__name__)


@shared_task(
    name="trainer_verification.purge_replaced_documents",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def purge_replaced_documents():
    """Delete the replaced documents whose 90 days are over, files and all. Daily."""
    from .services import purge_replaced_documents as purge

    removed = purge()
    if removed:
        logger.info("Purged %d replaced verification document(s)", removed)
    return removed
