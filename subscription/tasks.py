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
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

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



@shared_task(
    name="subscription.poll_open_payments",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
)
@no_retry_on_timeout
def poll_open_payments(now=None):
    """Ask the gateway about checkouts nobody has told us about, and close the dead ones.

    THE HOLE THIS FILLS. A checkout could only ever be resolved by the gateway's webhook
    or by the customer pressing something (`PaymentReconcileView`). A webhook that never
    arrives — a dropped delivery, an outage, a customer who closed the app — left the row
    `pending` for ever. And `start_payment` hands the SAME open checkout back for the
    same subscription, so that customer could never open a new one: one lost webhook and
    they could not buy again at all.

    WHAT IT DOES, every minute:
      * leaves each checkout `PAYMENT_POLL_GRACE_SECONDS` to be told about the ordinary
        way, so this never races the webhook it exists to back up;
      * asks the gateway about the rest, oldest first, at most `PAYMENT_POLL_BATCH` a run
        (it says so when the cap binds — a cap that binds silently is a queue nobody
        sees), and completes or fails each one through `PaymentService`, which is the
        only code that moves a payment's state;
      * expires a checkout still unpaid `PAYMENT_CHECKOUT_TTL_MINUTES` after it opened,
        so the customer can start again, and tells them it was closed. Money that turns
        up for it afterwards is still honoured: `expired -> completed` is a legal
        transition precisely so a late webhook is not refused, and
        `poll_expired_checkouts` keeps asking the gateway about it for
        `PAYMENT_LATE_MONEY_WINDOW_HOURS`, because in reconcile mode there is no webhook.

    Never trusts a bare status flag: `fetch_payment_status` matches amount, currency and
    reference on the merchant account, and `complete_payment` verifies again.
    """
    from django.conf import settings
    from django.utils import timezone

    from notifications.services import NotificationService

    from .services.payment_gateways import PaymentGatewayError, PaymentGatewayService
    from .models import OPEN_PAYMENT_STATUSES, Payment
    from .services.payment_service import (InvalidPaymentTransition,
                                           PaymentService, PaymentVerificationError)

    now = now or timezone.now()
    grace = int(getattr(settings, 'PAYMENT_POLL_GRACE_SECONDS', 20))
    from .utils import checkout_ttl

    # The same window `utils.checkout_expires_at` shows the customer. One definition:
    # a screen that counts down to a different moment than the server closes on is a
    # customer transferring money against a reference that has just died.
    ttl_window = checkout_ttl()
    batch = int(getattr(settings, 'PAYMENT_POLL_BATCH', 100))

    from .models import HELD_PAYMENT_STATUSES

    rows = list(Payment.objects
                .filter(status__in=OPEN_PAYMENT_STATUSES,
                        created_at__lte=now - timedelta(seconds=grace))
                .exclude(status__in=HELD_PAYMENT_STATUSES)
                .exclude(gateway_transaction_reference='')
                .exclude(gateway_transaction_reference__isnull=True)
                .select_related('subscription__user')
                .order_by('created_at', 'id')[:batch])
    # Held rows never expire, so oldest-first they would fill the batch for good and
    # starve every new checkout (the review, L5). They are asked about in a small pass of
    # their own — only an exact transfer does anything to them — least recently asked
    # first, so every held row is reached in turn.
    held_batch = int(getattr(settings, 'PAYMENT_POLL_HELD_BATCH', 10))
    rows += list(Payment.objects
                 .filter(status__in=HELD_PAYMENT_STATUSES)
                 .exclude(gateway_transaction_reference='')
                 .exclude(gateway_transaction_reference__isnull=True)
                 .select_related('subscription__user')
                 .order_by('updated_at', 'id')[:held_batch])

    checked = completed = expired = failed = held = 0
    for payment in rows:
        checked += 1
        result = None
        try:
            gateway = PaymentGatewayService(payment.payment_method)
            result = gateway.fetch_payment_status(
                payment.gateway_transaction_reference,
                expected={'amount': payment.amount, 'currency': payment.currency,
                          'since': payment.created_at},
            )
        except (PaymentGatewayError, Exception) as exc:   # noqa: BLE001
            # One unreachable gateway must not stop the queue, and an unanswered
            # checkout is left exactly as it was: the next run asks again.
            logger.warning("poll: gateway lookup failed for payment %s: %s",
                           payment.id, exc)
            continue

        state = str((result or {}).get('status', '')).lower()
        if state in ('completed', 'success', 'succeeded', 'paid'):
            try:
                PaymentService.complete_payment(payment.id, result)
                completed += 1
            except (PaymentVerificationError, InvalidPaymentTransition) as exc:
                logger.warning("poll: refused to complete payment %s: %s", payment.id, exc)
            continue
        if state == 'needs_review':
            # The reference, with another amount or currency: held for an operator,
            # never dropped (B-051). Seeing the same transfer again is a no-op.
            try:
                PaymentService.hold_for_review(payment.id, result,
                                               reason=result.get('reason') or 'amount_differs')
                held += 1
            except (PaymentVerificationError, InvalidPaymentTransition) as exc:
                logger.warning("poll: could not hold payment %s for review: %s",
                               payment.id, exc)
            continue
        if payment.status == 'needs_review':
            # Money was seen for it: an operator decides, not the clock and not a
            # gateway flag. Only an exact transfer (above) completes it meanwhile. Touched,
            # so the held pass reaches the others before this one again.
            Payment.objects.filter(pk=payment.pk).update(updated_at=now)
            continue
        if state in ('failed', 'declined', 'rejected'):
            try:
                PaymentService.fail_payment(payment.id, 'The gateway declined it')
                failed += 1
            except InvalidPaymentTransition as exc:
                logger.warning("poll: could not fail payment %s: %s", payment.id, exc)
            continue

        if state == 'unknown':
            # The merchant account could not be read. That is evidence of nothing, so
            # nothing is closed on it: the next minute asks again.
            continue
        if payment.created_at > now - ttl_window:
            continue                      # young enough to still be being paid
        try:
            PaymentService.expire_payment(payment.id, 'Not completed within its window')
        except InvalidPaymentTransition as exc:
            logger.warning("poll: could not expire payment %s: %s", payment.id, exc)
            continue
        expired += 1
        recipient = getattr(payment.subscription, 'user', None)
        if recipient is None:
            continue
        # The inbox row is always written; a muted type simply sends no push —
        # `NotificationService` asks the preferences when it DELIVERS (B-014). This
        # used to skip the call for a muted type, so a customer who silenced the push
        # also lost the record that their checkout was closed (SIDE_BUGS S-133). Keyed
        # on the payment, so a retry of this run cannot tell anyone twice.
        NotificationService.create_and_send(
            recipient=recipient, event_type="checkout_expired",
            related_object_id=str(payment.id),
            metadata={'context': {},
                      'data': {'type': 'checkout_expired',
                               'payment_id': str(payment.id)}})

    if checked >= batch + held_batch:
        logger.warning("poll: hit the %s-row cap; the rest waits for the next minute",
                       batch)
    if completed or expired or failed or held:
        logger.info("poll: %s checked, %s completed, %s expired, %s failed, %s held",
                    checked, completed, expired, failed, held)
    return {'checked': checked, 'completed': completed, 'expired': expired,
            'failed': failed, 'held': held, 'capped': checked >= batch + held_batch}


@shared_task(
    name="subscription.poll_expired_checkouts",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
)
@no_retry_on_timeout
def poll_expired_checkouts(now=None):
    """Keep looking for money that arrives for a checkout the poller already closed.

    `poll_open_payments` expires a checkout nobody paid within its window, and the state
    machine lets `expired -> completed` precisely so late money is honoured — but only a
    webhook could ever deliver it, and in ShamCash's default reconcile mode there is no
    webhook (`verify_webhook` refuses every delivery until a secret is configured). So a
    customer who paid in the last minute of the window, or whose transfer posted late,
    kept 'expired' and got nothing (SIDE_BUGS S-077).

    Every run asks the gateway about each expired checkout that has a reference and
    opened within `PAYMENT_LATE_MONEY_WINDOW_HOURS` (72: the gateway's own lookback, so
    nothing older could be found anyway), newest first, at most `PAYMENT_POLL_BATCH`.
    Only a verified 'completed' does anything, and it goes through
    `PaymentService.complete_payment` like every other completion. 'pending', 'failed'
    and 'unknown' leave the row as it is. Scheduled every ten minutes, not every
    minute: a closed checkout is not being watched by anyone, and each ask is a
    round trip to the merchant account.
    """
    from django.conf import settings
    from django.utils import timezone

    from .models import Payment
    from .services.payment_gateways import PaymentGatewayError, PaymentGatewayService
    from .services.payment_service import (InvalidPaymentTransition,
                                           PaymentService, PaymentVerificationError)

    now = now or timezone.now()
    window = timedelta(hours=int(getattr(settings, 'PAYMENT_LATE_MONEY_WINDOW_HOURS', 72)))
    batch = int(getattr(settings, 'PAYMENT_POLL_BATCH', 100))

    rows = list(Payment.objects
                .filter(status='expired', created_at__gte=now - window)
                .exclude(gateway_transaction_reference='')
                .exclude(gateway_transaction_reference__isnull=True)
                .order_by('-created_at', '-id')[:batch])

    checked = completed = 0
    for payment in rows:
        checked += 1
        try:
            result = PaymentGatewayService(payment.payment_method).fetch_payment_status(
                payment.gateway_transaction_reference,
                expected={'amount': payment.amount, 'currency': payment.currency,
                          'since': payment.created_at},
            )
        except (PaymentGatewayError, Exception) as exc:   # noqa: BLE001
            logger.warning("late poll: gateway lookup failed for payment %s: %s",
                           payment.id, exc)
            continue
        state = str((result or {}).get('status', '')).lower()
        if state == 'needs_review':
            # Almost-right money for a closed checkout is still money: held (B-051).
            try:
                PaymentService.hold_for_review(payment.id, result,
                                               reason=result.get('reason') or 'amount_differs')
            except (PaymentVerificationError, InvalidPaymentTransition) as exc:
                logger.warning("late poll: could not hold payment %s: %s", payment.id, exc)
            continue
        if state not in ('completed', 'success', 'succeeded', 'paid'):
            continue
        try:
            PaymentService.complete_payment(payment.id, result)
            completed += 1
            logger.warning("late poll: payment %s was paid after its checkout closed; "
                           "completed", payment.id)
        except (PaymentVerificationError, InvalidPaymentTransition) as exc:
            logger.warning("late poll: refused to complete payment %s: %s", payment.id, exc)

    if checked >= batch:
        logger.warning("late poll: hit the %s-row cap; older closed checkouts wait", batch)
    return {'checked': checked, 'completed': completed, 'capped': checked >= batch}


#: A payment reference as `PaymentGatewayService._generate_reference` writes it —
#: `SHAMCASH_<milliseconds>_<8 hex>` — found anywhere in a transfer's note, in any case.
REFERENCE_IN_NOTE = re.compile(r'([A-Za-z]+)_(\d{10,})_([0-9A-Fa-f]{8})')


@shared_task(
    name="subscription.sweep_unmatched_transfers",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
)
def sweep_unmatched_transfers(now=None):
    """Find the money no checkout claimed, and put it in front of an operator.

    The pollers ask the gateway about each checkout BY ITS REFERENCE, so a transfer
    that quotes no reference — or a mistyped one — is invisible to them: the money sat
    on the merchant account and the checkout expired (the spec, section 7: "a transfer
    with the right amount and a wrong reference lands in the review queue with evidence,
    and is not lost"; BACKEND_BUGS B-051). This reads the account over the gateway's
    lookback and, for each transfer that is neither on a payment nor already queued:

      * its note names a checkout still open (or closed within the late-money window):
        left to the pollers, which match it exactly or hold it within minutes;
      * its note names a checkout paid or closed for good: queued against it
        (`duplicate_transfer` / `checkout_closed`) — money to hand back;
      * no reference, and its amount and currency fit exactly ONE open checkout opened
        before it: that checkout is held for review (`reference_missing`);
      * otherwise queued alone (`no_candidate` / `several_candidates`, with the
        candidates) for an operator to attach or dismiss.

    Every ten minutes. A window the gateway could not read in full changes nothing it
    did not see.
    """
    from django.conf import settings
    from django.db.models import Q
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    from .models import HELD_PAYMENT_STATUSES, OPEN_PAYMENT_STATUSES, Payment, TransferReview
    from .services.payment_gateways import PaymentGatewayError, PaymentGatewayService
    from .services.payment_service import PaymentError, PaymentService

    now = now or timezone.now()
    lookback = timedelta(hours=int(getattr(settings, 'PAYMENT_LATE_MONEY_WINDOW_HOURS', 72)))
    late_window = lookback
    try:
        gateway = PaymentGatewayService('shamcash')
        txns, complete = gateway.list_transactions(now - lookback, now)
    except (PaymentGatewayError, Exception) as exc:   # noqa: BLE001
        logger.warning("sweep: could not read the merchant account: %s", exc)
        return {'seen': 0, 'held': 0, 'queued': 0, 'complete': False}

    facts = [gateway.transaction_facts(t) | {'gateway_data': t} for t in txns]
    facts = [f for f in facts if f['transaction_id']]
    ids = [f['transaction_id'] for f in facts]
    known = set(Payment.objects.filter(Q(transaction_id__in=ids) | Q(gateway_event_id__in=ids))
                .values_list('transaction_id', flat=True))
    known |= set(Payment.objects.filter(gateway_event_id__in=ids)
                 .values_list('gateway_event_id', flat=True))
    known |= set(TransferReview.objects.filter(transaction_id__in=ids)
                 .values_list('transaction_id', flat=True))

    held = queued = settled = 0
    for fact in facts:
        if fact['transaction_id'] in known:
            continue
        refs = {f"{p.upper()}_{ms}_{h.lower()}"
                for p, ms, h in REFERENCE_IN_NOTE.findall(fact.get('note') or '')}
        named = (Payment.objects.filter(gateway_transaction_reference__in=refs)
                 .order_by('-created_at', '-id').first() if refs else None)
        try:
            if named is not None:
                if named.status in OPEN_PAYMENT_STATUSES:
                    # Open, or held: the open poller matches it exactly, or holds it, within
                    # the minute; a held one waits for an operator either way (the review,
                    # L2). Queuing it here raced that poller onto the same transfer.
                    continue
                if named.status == 'expired' and named.created_at >= now - late_window:
                    # Settled HERE, not left to the late poller: its batch is capped and
                    # newest-first, so behind a hundred abandoned checkouts this one was
                    # never reached and the money never surfaced (the review, L3).
                    if _exact(fact, named):
                        PaymentService.complete_payment(named.id, {
                            'status': 'completed', 'amount': fact.get('amount'),
                            'currency': fact.get('currency'),
                            'transaction_id': fact['transaction_id'],
                            'event_id': fact['transaction_id'],
                            'reference': named.gateway_transaction_reference,
                            'gateway_data': fact.get('gateway_data')})
                        settled += 1
                    elif PaymentService.hold_for_review(
                            named.id, fact, reason=_near_reason(fact, named)):
                        held += 1
                    continue
                if PaymentService.hold_for_review(named.id, fact, reason='checkout_closed'):
                    queued += 1
                continue
            try:
                amount = Decimal(str(fact.get('amount'))).quantize(Decimal('0.01'))
            except (InvalidOperation, TypeError, ValueError):
                amount = None
            occurred = fact.get('occurred_at')
            occurred = parse_datetime(occurred) if isinstance(occurred, str) else occurred
            candidates = []
            if amount is not None:
                candidates = list(
                    Payment.objects
                    .filter(status__in=OPEN_PAYMENT_STATUSES, amount=amount,
                            currency=fact.get('currency'))
                    .exclude(status__in=HELD_PAYMENT_STATUSES)
                    .exclude(gateway_transaction_reference__isnull=True)
                    .exclude(gateway_transaction_reference='')
                    .filter(**({'created_at__lte': occurred} if hasattr(occurred, 'tzinfo')
                               and occurred is not None else {}))
                    .order_by('created_at', 'id').values_list('id', flat=True))
            if len(candidates) == 1:
                if PaymentService.hold_for_review(candidates[0], fact,
                                                  reason='reference_missing'):
                    held += 1
            elif PaymentService.queue_unclaimed_transfer(fact, candidates=candidates):
                queued += 1
        except PaymentError as exc:
            logger.warning("sweep: transfer %s not queued: %s", fact['transaction_id'], exc)

    if held or queued or settled:
        logger.warning("sweep: %s transfer(s) held a checkout for review, %s queued alone, "
                       "%s settled a closed checkout", held, queued, settled)
    if not complete:
        # Said out loud: what was not read was not seen, and an account too busy to read
        # in full is a queue nobody is looking at (the review, L9).
        logger.error("sweep: the merchant account could not be read in full; transfers in "
                     "the unread part of the window are not in the review queue yet")
    return {'seen': len(facts), 'held': held, 'queued': queued, 'settled': settled,
            'complete': complete}


def _exact(fact, payment) -> bool:
    """Whether a listed transfer is exactly what `payment` asked for."""
    try:
        amount = Decimal(str(fact.get('amount'))).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return (amount == Decimal(payment.amount).quantize(Decimal('0.01'))
            and str(fact.get('currency') or '').upper() == str(payment.currency).upper())


def _near_reason(fact, payment) -> str:
    """Why a transfer naming `payment` is not its exact money."""
    currency = str(fact.get('currency') or '').upper()
    return ('currency_differs' if currency and currency != str(payment.currency).upper()
            else 'amount_differs')
