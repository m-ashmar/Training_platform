"""Scheduled work on user rows."""
import logging

from celery import shared_task

from training_platform.task_retries import no_retry_on_timeout

logger = logging.getLogger(__name__)


@shared_task(
    name="users.refresh_ages_from_birth_dates",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def refresh_ages_from_birth_dates():
    """Bring `age` in line with `date_of_birth` for everyone whose age has moved.

    `age` is derived on every save, so a row goes stale only on a birthday nobody saved
    it on. Scans the rows that have a date of birth and writes only the ones that
    differ, so a missed night is caught up by the next run. Idempotent.
    """
    from .models import CustomUser, age_on

    stale = []
    for user in (CustomUser.objects.filter(date_of_birth__isnull=False)
                 .only("id", "date_of_birth", "age").iterator(chunk_size=2000)):
        age = age_on(user.date_of_birth)
        if user.age != age:
            user.age = age
            stale.append(user)
    CustomUser.objects.bulk_update(stale, ["age"], batch_size=500)
    if stale:
        logger.info("Refreshed age for %d users", len(stale))
    return len(stale)
