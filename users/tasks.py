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
def refresh_ages_from_birth_dates(after=None):
    """Bring `age` in line with `date_of_birth` for everyone whose age has moved.

    `age` is derived on every save, so a row goes stale only on a birthday nobody saved
    it on. Scans the rows that have a date of birth and writes only the ones that
    differ, so a missed night is caught up by the next run. Idempotent.

    A page of `PERIODIC_SCAN_SIZE` rows per invocation (the work per row is a
    comparison), the rest handed on to a fresh invocation (`training_platform.batching`):
    one scan of every user under the 300 s soft limit is the shape the
    production-readiness audit (2026-09-25) found in the daily jobs. Keyset pages, not
    `.iterator()`, whose server-side cursor does not survive a transaction-pooling
    connection pooler. Returns how many ages THIS invocation refreshed.
    """
    from training_platform.batching import Batch, scan_size

    from .models import CustomUser, age_on

    batch = Batch(size=scan_size())
    stale = []
    for user in batch.page(CustomUser.objects.filter(date_of_birth__isnull=False)
                           .only("id", "date_of_birth", "age"), after):
        if batch.spent():
            break
        batch.done(user.pk)
        age = age_on(user.date_of_birth)
        if user.age != age:
            user.age = age
            stale.append(user)
    CustomUser.objects.bulk_update(stale, ["age"], batch_size=500)
    if stale:
        logger.info("Refreshed age for %d users", len(stale))
    batch.hand_on(refresh_ages_from_birth_dates)
    return len(stale)


@shared_task(
    name="users.renew_coaching_periods",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def renew_coaching_periods(phase="remind", after=None):
    """Remind 3 days ahead, charge every coaching month that has ended, freeze what the
    wallet cannot cover and keep reminding it.

    See users/coaching.py. Every step is idempotent, so a retried run charges nothing
    twice; a relation that failed is retried with the rest. Returns the renewal counts,
    plus `renewal_reminders` when any "renews soon" notice went out — this invocation's.

    In pages (`training_platform.batching`): at most `PERIODIC_BATCH_SIZE` relations an
    invocation — the reminders, then the on-time renewals, then the frozen ones with
    something to do — and the rest handed on to a fresh invocation with `phase` and the
    last relation id done. A month-end with thousands of renewals in one task under the
    300 s soft limit was cut off part-way, and the relations that sorted later waited an
    hour, or several (the production-readiness audit, 2026-09-25). The continuation is
    enqueued BEFORE a failure is raised for the retry, so one bad relation does not hold
    the rest back; the retry re-runs this page only.
    """
    from training_platform.batching import Batch

    from .coaching import remind_upcoming, renew_due

    batch = Batch()
    counts: dict = {}
    if phase == "remind":
        reminded = remind_upcoming(batch=batch, after=after)
        if reminded:
            counts['renewal_reminders'] = reminded
        if batch.hand_on(renew_coaching_periods, phase="remind"):
            logger.info("Coaching renewals: %s", counts)
            return counts
        phase, after = "on_time", None
    result = renew_due(batch=batch, phase=phase, after=after)
    counts = {**result['counts'], **counts}
    following = result.get('next')
    if following:
        renew_coaching_periods.apply_async(kwargs=following)
    if counts:
        logger.info("Coaching renewals: %s", counts)
    if result['failed']:
        raise RuntimeError(f"coaching renewal failed for relations {result['failed']}")
    return counts
