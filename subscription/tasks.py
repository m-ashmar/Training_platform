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
from datetime import timedelta

from celery import shared_task

from training_platform.task_retries import no_retry_on_timeout

logger = logging.getLogger(__name__)


@shared_task(
    name="subscription.expire_lapsed_subscriptions",
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


#: How long a completed payment's payout is left to the on-commit receiver before the
#: sweep treats it as lost. That receiver runs milliseconds after the COMMIT; the margin
#: is for a worker that is slow, not for one that is working.
PAYOUT_GRACE = timedelta(minutes=10)


@shared_task(
    name="subscription.settle_owed_payouts",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def settle_owed_payouts(batch_size=200):
    """Pay the trainer for every completed payment whose payout never landed.

    The payout runs on commit, after the payment row already says 'completed'. One
    transient fault in it — a deadlock with the client's own transfer, a dropped
    connection, a worker killed between the COMMIT and the receiver — rolled the payout
    back and left nothing that remembered it was owed. The retry paths now look at
    `payout_settled_at` instead of the status, but they only run if something calls
    again: a gateway that got its 200 elsewhere, or a reconcile the customer never
    repeats, leaves the trainer unpaid. Measured before this: after one injected fault,
    redelivery, a new event id, reconcile and the admin action all left `legs=[]` and the
    trainer at 0.00.

    Sends `payment_completed` again for each, exactly as `PaymentService.redeliver_payout`
    does; the receiver is idempotent under the payment row lock, so one that lands
    meanwhile is not paid twice. One payment failing does not stop the others. Returns
    how many were sent.
    """
    from django.utils import timezone

    from .models import Payment
    from .services.payment_service import PaymentService, payment_completed

    cutoff = timezone.now() - PAYOUT_GRACE
    owed = list(
        Payment.objects.filter(status='completed', payout_settled_at__isnull=True,
                               updated_at__lt=cutoff)
        .order_by('updated_at', 'id').values_list('pk', flat=True)[:batch_size]
    )
    sent = 0
    for pk in owed:
        payment = (Payment.objects.select_related('subscription', 'subscription__user',
                                                  'subscription__plan').get(pk=pk))
        try:
            payment_completed.send(sender=PaymentService, payment=payment,
                                   subscription=payment.subscription)
            sent += 1
        except Exception:
            logger.exception("Owed payout for payment %s failed again; the next sweep retries",
                             payment.pk)
    if owed:
        logger.warning("Sent %s of %s owed payout(s)", sent, len(owed))
    return sent


@shared_task(
    name="subscription.send_renewal_reminders",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
)
@no_retry_on_timeout
def send_renewal_reminders(now=None):
    """Remind before the plan ends, and keep asking after it ended (the owner,
    2026-09-19: "we show a renewal reminder 3 days before ... after it ends ... keep
    asking to re-subscribe").

    Daily. Two passes:
      * expiring — an entitled row whose cover ends within
        SUBSCRIPTION_RENEWAL_NOTICE_DAYS gets `subscription_expiring`, once per cover
        period (keyed on its end, so a renewal that moves the end is reminded again).
      * lapsed — a frozen row (`Subscription.is_frozen`) gets `subscription_expired` for
        each step of SUBSCRIPTION_LAPSED_REMINDER_DAYS it reaches after its end (the
        latest one, so a missed run is made up), then no more.
    Every subscriber, whatever `auto_renew` says: nothing charges a plan automatically.
    The in-app row is always written; a muted type sends no push. Returns the counts.
    """
    from math import ceil

    from django.conf import settings
    from django.db.models import Q
    from django.utils import timezone

    from notifications.services import NotificationService

    from .models import Subscription

    now = now or timezone.now()
    lead = timedelta(days=int(getattr(settings, 'SUBSCRIPTION_RENEWAL_NOTICE_DAYS', 3)))
    steps = sorted(getattr(settings, 'SUBSCRIPTION_LAPSED_REMINDER_DAYS', (0, 3, 7, 14, 30)))
    counts = {'expiring': 0, 'expired': 0}

    # The whole window, every run: a run missed or cut short is made up by the next
    # one. A notice already written is found by its key and not pushed again to a device
    # that has it (the FCM channel skips tokens it already reached).
    soon = (Subscription.objects.filter(status__in=Subscription.ENTITLED_STATUSES,
                                        user__is_active=True)
            .filter(Q(end_date__gt=now, end_date__lte=now + lead)
                    | Q(trial_end_date__gt=now, trial_end_date__lte=now + lead))
            .select_related('user'))
    for sub in soon.iterator(chunk_size=500):
        end = sub.effective_end
        if not sub.is_active or end is None or not (now < end <= now + lead):
            continue
        # A cover shorter than the notice would be told "ends soon" the day it was
        # bought.
        if sub.start_date and end - sub.start_date <= lead:
            continue
        days = max(1, ceil((end - now) / timedelta(days=1)))
        if NotificationService.create_and_send(
                recipient=sub.user, event_type="subscription_expiring",
                related_object_id=f"{sub.pk}:{end.isoformat()}",
                metadata={'context': {'days': days},
                          'data': {'type': 'subscription_expiring',
                                   'subscription_id': str(sub.pk),
                                   'ends_at': end.isoformat()}}) is not None:
            counts['expiring'] += 1

    if steps:
        oldest = now - timedelta(days=steps[-1] + 1)
        lapsed = (Subscription.objects.filter(user__is_active=True)
                  .filter(Q(end_date__lte=now, end_date__gt=oldest)
                          | Q(trial_end_date__lte=now, trial_end_date__gt=oldest))
                  .select_related('user'))
        for sub in lapsed.iterator(chunk_size=500):
            end = sub.effective_end
            if end is None or end > now or not sub.is_frozen:
                continue
            # The latest step reached, so a missed day is made up; each step once.
            reached = [d for d in steps if d <= (now - end).days]
            if not reached:
                continue
            days = reached[-1]
            if NotificationService.create_and_send(
                    recipient=sub.user, event_type="subscription_expired",
                    related_object_id=f"{sub.pk}:{end.isoformat()}:{days}",
                    metadata={'context': {},
                              'data': {'type': 'subscription_expired',
                                       'subscription_id': str(sub.pk),
                                       'ended_at': end.isoformat()}}) is not None:
                counts['expired'] += 1

    logger.info("Subscription reminders: %s", counts)
    return counts

