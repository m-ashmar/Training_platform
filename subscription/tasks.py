"""Scheduled subscription maintenance.

`expire_subscriptions()` has existed in subscription/utils.py since the beginning,
carrying the docstring "This should be run as a scheduled task". It never was. It was
not a Celery task, had no beat entry and no management command, and a grep for its
name across the whole tree returned only its own definition — as did every other
function in that module.

The consequence is measurable in the database rather than in a log: 64 of the 72 rows
marked 'active' are already past their end_date. Nothing moves a subscription out of
'active' when its cover ends, so paid access never lapses on its own.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name="subscription.expire_lapsed_subscriptions",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
def expire_lapsed_subscriptions():
    """Move subscriptions whose cover has ended out of 'active'/'trial'.

    One set-based UPDATE, not a row-by-row loop. That matters for correctness as much
    as for speed: under READ COMMITTED, Postgres re-evaluates the WHERE clause against
    any row a concurrent transaction changed, so a subscription being activated by a
    payment webhook at this exact moment is re-checked and skipped rather than being
    expired on top of a fresh activation. The previous implementation read a queryset,
    then saved each row individually, with nothing between the read and the write.

    Idempotent by construction: a second run matches nothing, so an at-least-once
    delivery costs nothing.
    """
    from django.db.models import Q
    from django.utils import timezone

    from .models import Subscription

    now = timezone.now()

    # A trial ends at trial_end_date when it has one; everything else ends at end_date.
    lapsed = (
        Q(status='active', end_date__lt=now)
        | Q(status='trial', trial_end_date__lt=now)
        | Q(status='trial', trial_end_date__isnull=True, end_date__lt=now)
    )

    # updated_at is auto_now, which .update() does not trigger; set it explicitly so
    # the row still records when it changed.
    expired = Subscription.objects.filter(lapsed).update(status='expired', updated_at=now)

    if expired:
        logger.info("Expired %s lapsed subscription(s)", expired)
    return expired
