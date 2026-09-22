"""
Payment Service — the single authority for payment state changes.
==============================================================

NO other code may set `Payment.status` to a terminal state or activate a
subscription. Views, the webhook, the poller/reconciler and admin tools all
funnel through this service, which:

  verify transition (state machine)
        -> verify amount + currency (+ transaction match)
        -> idempotency (event id / already-completed)
        -> mark payment completed
        -> generate invoice number
        -> activate / extend subscription
        -> emit `payment_completed` signal
        -> all inside one transaction.atomic()

Illegal state transitions raise InvalidPaymentTransition. Amount/currency
mismatches raise PaymentVerificationError. Both refuse to activate anything.

Lock order: the SUBSCRIPTION row first, then its payment rows. Every method here that
takes both takes them in that order, so a checkout (which starts from the subscription)
and a completion (which starts from a payment) wait for each other instead of
deadlocking.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.db import transaction
from django.dispatch import Signal
from django.utils import timezone

from policy.money import source_of_payment_method

from ..models import OPEN_PAYMENT_STATUSES, Payment, Subscription

logger = logging.getLogger(__name__)

# Fired after a payment is fully completed and its subscription activated.
# providing_args=["payment", "subscription"]
payment_completed = Signal()


class PaymentError(Exception):
    """Base class for payment-service errors."""


class InvalidPaymentTransition(PaymentError):
    """Raised when an illegal payment state transition is attempted."""


class PaymentVerificationError(PaymentError):
    """Raised when gateway-reported data does not match the payment record."""


class SubscriptionAlreadyActive(PaymentError):
    """A first payment was asked for while the subscription's cover is still running."""


class PaymentService:
    """Centralized, verified payment lifecycle operations."""

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    @classmethod
    def complete_payment(cls, payment_id, verified_data: dict) -> Payment:
        """
        Complete a payment and activate its subscription — the ONLY path to
        active access. `verified_data` must come from a trusted source (a
        signature-verified webhook, or a gateway status/transaction lookup that
        has already matched the transaction) and must contain at least:
            amount, currency, status  (+ optional transaction_id, reference, event_id)

        Idempotent: replays (same event id, or already-completed payment) return
        the existing payment without granting anything again — but a replay of a payment
        whose payout is still owed sends the payout again (see `redeliver_payout`).
        """
        with transaction.atomic():
            subscription, payment = cls._lock_subscription_and_payment(payment_id)

            # --- Idempotency: already completed → no-op replay ---
            if payment.status == 'completed':
                logger.info(f"complete_payment idempotent no-op for {payment.id} (already completed)")
                cls.redeliver_payout(payment)
                return payment

            event_id = verified_data.get('event_id')
            if event_id:
                dup = Payment.objects.filter(gateway_event_id=event_id).exclude(id=payment.id).first()
                if dup:
                    logger.warning(f"Duplicate gateway_event_id {event_id}: already on payment {dup.id}")
                    cls.redeliver_payout(dup)
                    return dup

            # --- State machine: must be legal to reach 'completed' ---
            if not payment.can_transition_to('completed'):
                raise InvalidPaymentTransition(
                    f"Payment {payment.id} cannot go {payment.status} -> completed"
                )

            # --- Verify money matches what we asked for ---
            cls._verify_amount_and_currency(payment, verified_data)

            reported_status = str(verified_data.get('status', '')).lower()
            if reported_status not in ('completed', 'success', 'succeeded', 'paid'):
                raise PaymentVerificationError(
                    f"Gateway status '{reported_status}' is not a success state"
                )

            # --- Apply completion ---
            payment.status = 'completed'
            if verified_data.get('transaction_id'):
                payment.transaction_id = str(verified_data['transaction_id'])
            if verified_data.get('reference'):
                payment.gateway_transaction_reference = str(verified_data['reference'])
            if event_id:
                payment.gateway_event_id = str(event_id)
            if verified_data.get('gateway_data') is not None:
                payment.gateway_response = verified_data['gateway_data']
            if not payment.invoice_number:
                payment.invoice_number = cls._generate_invoice_number()
            # The date on the receipt. Written once, here, and never again — see the
            # field's comment for why `updated_at` cannot answer this.
            if payment.completed_at is None:
                payment.completed_at = timezone.now()

            # --- Activate / extend the subscription, and record what that bought ---
            payment.covers_from, payment.covers_until = cls._activate_subscription(
                subscription, source_of_payment_method(payment.payment_method))
            payment.save()

            logger.info(
                f"Payment {payment.id} completed (invoice {payment.invoice_number}), "
                f"subscription {payment.subscription_id} activated"
            )

        # ON COMMIT. The comment here said "after commit-safe state", which was true of
        # this function and not of its callers: `PaymentWebhookView` wraps the whole
        # update in `transaction.atomic()`, so the send happened inside a transaction
        # that could still roll back — and the receiver moves money.
        transaction.on_commit(lambda: payment_completed.send(
            sender=cls, payment=payment, subscription=payment.subscription))
        return payment

    @classmethod
    def ensure_invoice_number(cls, payment: Payment) -> str:
        """The invoice number of a completed payment, minting it once if it has none.

        A receipt has to carry a number that is STABLE (two reads of the same payment
        are the same document) and NEVER REUSED. `complete_payment` mints one for every
        payment it completes, so the only rows that reach here without one were
        completed before the column existed — but a receipt endpoint that read
        `payment.invoice_number` and generated a fallback of its own would be a second
        generator, and two generators is how a number gets reused. There is one, the
        private `_generate_invoice_number` below, and this is the only door to it
        outside completion.

        Idempotent and race-safe: the row is locked and re-read, so two concurrent
        receipt requests for the same payment mint at most one number and both return
        it. Refuses anything that is not completed — an unpaid checkout has no invoice.
        The payment's STATE is not touched.
        """
        if payment.status != 'completed':
            raise InvalidPaymentTransition(
                f"Payment {payment.id} is {payment.status}, not completed: no invoice")
        if payment.invoice_number:
            return payment.invoice_number
        with transaction.atomic():
            locked = Payment.objects.select_for_update().get(id=payment.id)
            if not locked.invoice_number:
                locked.invoice_number = cls._generate_invoice_number()
                locked.save(update_fields=['invoice_number', 'updated_at'])
                logger.info("Minted invoice %s for payment %s completed before the "
                            "column existed", locked.invoice_number, locked.id)
            payment.invoice_number = locked.invoice_number
        return payment.invoice_number

    @classmethod
    def redeliver_payout(cls, payment: Payment) -> bool:
        """Send `payment_completed` again if this payment's payout is still owed.

        The payout runs on commit, AFTER the payment row already says 'completed', and
        every way of retrying a payment decided "done" from that status alone: the
        webhook's event-id replay check, the already-completed no-op above, the reconcile
        view's early return and the admin action. Measured with one injected fault in the
        payout: delivery 1 -> 500 with no payout legs; the gateway's redelivery -> 200,
        legs []; a new event id -> 200, legs []; reconcile -> 200, legs []. The gateway
        stops on a 200, so the trainer was never paid.

        Each of those paths now calls this, and `subscription.settle_owed_payouts` sweeps
        whatever no retry ever reaches. It is on commit for the same reason the first
        send is — the receiver moves money and the caller's transaction can still roll
        back — and outside a transaction Django runs it immediately. A failure still
        propagates, so the caller answers 500 and the gateway tries again. The receiver
        is idempotent under the payment row lock, so a payout that did land is not paid
        twice; any receiver added to `payment_completed` has to be idempotent too, because
        this is the same signal sent again.

        Returns whether a payout was (re)scheduled.
        """
        if payment.status != 'completed' or payment.payout_settled_at is not None:
            return False
        logger.warning("Payment %s is completed but its payout is still owed; sending it again",
                       payment.id)
        transaction.on_commit(lambda: payment_completed.send(
            sender=cls, payment=payment, subscription=payment.subscription))
        return True

    @classmethod
    def cancel_payment(cls, payment_id, reason: str = "") -> Payment:
        """A payment that was never taken. Legal from pending/processing.

        `cancelled` has been a declared transition from `pending` since the state machine
        was written and no service method implemented it, while the webhook classified
        `cancelled`/`canceled`/`voided` as REFUNDS — and a refund is only legal from
        `completed`. So the most common gateway event of all, a customer opening the
        payment page and walking away, was answered 400 and redelivered for ever against
        a row that could never leave `pending`.
        """
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(id=payment_id)
            if payment.status == 'cancelled':
                return payment
            if not payment.can_transition_to('cancelled'):
                raise InvalidPaymentTransition(
                    f"Payment {payment.id} cannot go {payment.status} -> cancelled")
            payment.status = 'cancelled'
            payment.gateway_error = (reason or 'Cancelled at the gateway')[:2000]
            payment.save()
            logger.info("Payment %s cancelled (%s)", payment.id, reason or 'no reason')
        return payment

    @classmethod
    def refund_payment(cls, payment_id, reason: str = "", gateway_data: dict | None = None) -> Payment:
        """Move a completed payment to refunded and take back what it paid for.

        Nothing could reach this state. `PAYMENT_STATUS_TRANSITIONS` has declared
        `'completed' -> 'refunded'` all along, the webhook had no branch for a refund
        or a chargeback, and the admin action wrote the column with `queryset.update()`
        — so the money went back and the subscription stayed active either way.

        Access is withdrawn by exactly what the refunded payment bought (`covers_from` to
        `covers_until`). If nothing paid for is left the subscription is cancelled rather
        than left nominally active with a stale date.
        """
        with transaction.atomic():
            subscription, payment = cls._lock_subscription_and_payment(payment_id)
            if payment.status == 'refunded':
                return payment
            if not payment.can_transition_to('refunded'):
                raise InvalidPaymentTransition(
                    f"Payment {payment.id} cannot go {payment.status} -> refunded"
                )

            # The same verification the completion path does. This branch did NONE, so a
            # signed webhook claiming 1.00 USD against a 5,000.00 SYP payment revoked the
            # whole subscription and unwound the whole ledger — and nothing checked a
            # refund was not for MORE than the payment was worth, because nothing checked
            # the number at all. A partial refund is refused rather than silently treated
            # as a full one: this system can only give back what it took.
            if gateway_data:
                cls._verify_amount_and_currency(payment, gateway_data)

            payment.status = 'refunded'
            if reason:
                payment.gateway_error = reason[:2000]
            payment.save()

            now = timezone.now()
            # What THIS payment bought comes off — no more, no less. An earlier version
            # set `end_date = now` whatever else had been paid, so refunding a customer's
            # FIRST month destroyed the two months they had paid for and kept. Its
            # replacement took `plan.duration_days` off whenever any other completed
            # payment existed, which assumed every completed payment had bought a period;
            # one that cleared while cover was already running had bought nothing. So
            # refunding a double charge — the normal fix for one — cancelled the month
            # the customer still paid for, and for a new subscriber landed `end_date`
            # exactly on `start_date`: `validate_row()` raised, the refund webhook
            # answered 500 on every redelivery, the admin action raised, and the trainer
            # kept both payouts.
            #
            # Every completion now buys one period and records it on the payment, so the
            # subtraction is the recorded one — and none at all for a period the
            # subscription has since lapsed past and restarted (`_cover_to_take_back`).
            remaining_until = (subscription.end_date - cls._cover_to_take_back(payment, subscription)
                               if subscription.end_date else now)
            if remaining_until > now:
                subscription.end_date = remaining_until
            else:
                # Nothing paid for is left. Clamped to now rather than written as the
                # subtraction: the subtraction can land on or before `start_date`, which
                # is a row `Subscription.clean()` rightly refuses to store.
                subscription.end_date = min(subscription.end_date or now, now)
                subscription.status = 'cancelled'
                subscription.cancelled_at = now
                subscription.auto_renew = False
            subscription.save()

            # And take the MONEY back, not just the access. `wallet.signals` credits the
            # client's wallet and pays the trainer out of it when a payment completes;
            # nothing undid either leg, so the platform refunded the card and the trainer
            # kept the payout. Inside the same transaction as the status change, so a
            # refund is one fact.
            try:
                from wallet.signals import unwind_payment

                legs = unwind_payment(payment)
            except Exception:
                logger.exception(
                    "Payment %s was refunded but the ledger could not be unwound; the "
                    "trainer payout and the minted top-up are still outstanding",
                    payment.id)
                raise

            logger.warning(
                "Payment %s refunded (%s); subscription %s now %s until %s; "
                "%d wallet leg(s) reversed",
                payment.id, reason or 'no reason given',
                subscription.id, subscription.status, subscription.end_date, legs,
            )
        return payment

    @classmethod
    def fail_payment(cls, payment_id, error_message: str = "") -> Payment:
        """Mark a payment failed (legal from pending/processing/authorized)."""
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(id=payment_id)
            if payment.status == 'failed':
                return payment
            if not payment.can_transition_to('failed'):
                raise InvalidPaymentTransition(f"Payment {payment.id} cannot go {payment.status} -> failed")
            payment.status = 'failed'
            payment.gateway_error = (error_message or 'Payment failed')[:2000]
            payment.save()
        return payment

    @classmethod
    def expire_payment(cls, payment_id, reason: str = "") -> Payment:
        """A checkout nobody finished. Legal from pending/processing/authorized.

        `expired` has been a declared transition since the state machine was written and
        nothing implemented it, so a customer who opened a checkout and walked away left
        the row `pending` for ever — and `start_payment` hands the SAME open checkout
        back for the same subscription, so that customer could never start a new one.
        The poller (`subscription.tasks.poll_open_payments`) closes them.
        """
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(id=payment_id)
            if payment.status == 'expired':
                return payment
            if not payment.can_transition_to('expired'):
                raise InvalidPaymentTransition(
                    f"Payment {payment.id} cannot go {payment.status} -> expired")
            payment.status = 'expired'
            payment.gateway_error = (reason or 'The checkout expired')[:2000]
            payment.save()
            logger.info("Payment %s expired (%s)", payment.id, reason or 'timed out')
        return payment

    @classmethod
    def mark_processing(cls, payment_id) -> Payment:
        """Optional intermediate state once the gateway has accepted the request."""
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(id=payment_id)
            if payment.status == 'processing':
                return payment
            if not payment.can_transition_to('processing'):
                raise InvalidPaymentTransition(f"Payment {payment.id} cannot go {payment.status} -> processing")
            payment.status = 'processing'
            payment.save()
        return payment

    @classmethod
    def start_renewal(cls, subscription: Subscription, initiate=None) -> Payment:
        """
        Begin a renewal: return the subscription's open checkout, opening one if it has
        none. This never mutates the subscription — activation happens later via
        complete_payment once the gateway confirms funds.

        `initiate(payment)` opens the gateway session and returns
        `(payment_method, reference, gateway_response)`; see `_checkout_under_lock`.
        Without it the payment is created without a reference.
        """
        # One checkout in flight at a time, and the check has to hold under concurrency.
        # It was a `.filter(...).first()` followed by a `.create(...)` with no lock, so
        # two real threads both found nothing and both opened one: a double-tapped Renew
        # button charged 10,000 SYP for a 5,000 SYP plan and granted 60 days. Both
        # charges then cleared verification, because both were legitimate.
        #
        # The subscription row is the lock, because it is what "a checkout for this
        # subscription" is scoped to, and the whole check-create-INITIATE happens under
        # it. The lock used to end when this function returned, before the view wrote
        # the reference: a second tap got the same payment back with no reference yet,
        # generated its own, and the last save won. 30 of 30 double taps on a barrier
        # handed the customer a reference that matched no payment, and a payment quoting
        # it was answered 400 by the webhook for ever.
        with transaction.atomic():
            locked = (Subscription.objects.select_for_update(of=('self',))
                      .select_related('plan').get(pk=subscription.pk))
            if locked.status not in ('active', 'expired'):
                raise PaymentError("Subscription is not in a renewable state")

            # ANY open payment, not only `metadata.kind='renewal'` ones. After a lapse
            # the app offers both Renew and Pay; each deduplicated only its own kind, so
            # both handed out a live reference and paying both charged twice.
            open_payments = Payment.objects.filter(
                subscription=locked, status__in=OPEN_PAYMENT_STATUSES)
            return cls._checkout_under_lock(locked, open_payments, 'renewal', initiate)

    @classmethod
    def start_payment(cls, subscription: Subscription, initiate) -> Payment:
        """The Pay button: the subscription's open checkout, opening one if it has none.

        Refused while cover is running — extending an active subscription is what renew
        is for. That check used to be the ONLY one on this path: nothing deduplicated,
        so two taps on a subscription that was pending or had lapsed each created a
        Payment with its own reference, both payable, and the second bought nothing.
        """
        with transaction.atomic():
            locked = (Subscription.objects.select_for_update(of=('self',))
                      .select_related('plan').get(pk=subscription.pk))
            if locked.is_active:
                raise SubscriptionAlreadyActive(
                    f"Subscription {locked.id} is already active; renew extends it")
            open_payments = Payment.objects.filter(
                subscription=locked, status__in=OPEN_PAYMENT_STATUSES)
            return cls._checkout_under_lock(locked, open_payments, 'initial', initiate)

    @classmethod
    def _checkout_under_lock(cls, subscription: Subscription, open_payments, kind: str,
                             initiate) -> Payment:
        """Return the open checkout, or open one. The caller holds the subscription lock.

        * An open payment that already has a reference is THE checkout: the customer may
          already be paying against it. It comes back as it is, with the gateway
          response it was opened with, and `already_open` set.
        * An open payment with no reference was never offered to anyone, so it is
          adopted rather than left beside a new one. POST /subscriptions/ writes exactly
          such a row; nothing could ever complete it, and every subscriber's history
          carried it as an orphan 'pending' payment beside the one that cleared.
        * Otherwise a new payment is created.

        The gateway is initiated while the lock is held. In transfer/reconcile mode that
        is local work; in hosted mode it is a round trip to ShamCash, and holding the
        lock across it is the point: a second tap waits and then receives the first
        tap's session instead of opening a second one. Only this subscription's row is
        held. If initiation fails the transaction rolls back and nothing is left behind.
        """
        candidates = list(open_payments.order_by('created_at', 'id'))
        for payment in candidates:
            if payment.gateway_transaction_reference:
                logger.info("Checkout %s already open for subscription %s",
                            payment.id, subscription.id)
                payment.already_open = True
                return payment
        if candidates and initiate is None:
            candidates[0].already_open = True
            return candidates[0]

        plan = subscription.plan
        payment = candidates[0] if candidates else Payment(subscription=subscription,
                                                           status='pending')
        payment.subscription = subscription
        # THE PLAN'S PRICE, CHARGED IN THE BUYER'S CURRENCY.
        #
        # A row nobody has been asked to pay yet carries no promise, so it follows the
        # plan as it is now. Hardcoding 'USD' here made every renewal unclearable: plans
        # are priced in SYP, ShamCash reports SYP, and `_verify_amount_and_currency`
        # rejected the mismatch on arrival.
        #
        # The owner (2026-09-21): a person trades in ONE currency, theirs, and every
        # service is buyable in either. So the plan keeps the price of record the
        # operator set, and what this buyer is charged is that price converted into
        # their own currency at the operator's live rate — snapshotted here, because a
        # refund reverses at what was paid and a receipt has to be able to say so.
        # A buyer already in the plan's currency converts nothing and consults no rate,
        # which is why a stale rate cannot stop a same-currency sale.
        #
        # `charge_for` raises `ExchangeRateUnavailable` (with a `code`) when the price
        # has to cross and the rate is missing or stale. It is deliberately NOT caught:
        # opening a checkout at an unconverted number would charge dollars to a pound
        # wallet. `subscription/views.py` turns it into a refusal the app can read.
        from wallet import currency_for as _currency_for

        from ..pricing import charge_for

        priced = charge_for(plan.price, plan.currency, _currency_for(subscription.user))
        payment.amount = priced.amount
        payment.currency = priced.currency
        payment.price_amount = priced.price_amount
        payment.price_currency = priced.price_currency
        payment.exchange_rate = priced.rate
        payment.exchange_rate_value = getattr(priced.rate, "rate", None)
        metadata = dict(payment.metadata or {})
        if kind == 'renewal':
            metadata['kind'] = 'renewal'
            payment.description = f"Renewal for {plan.name}"
        else:
            metadata.pop('kind', None)
            payment.description = f"Payment for {plan.name}"
        payment.metadata = metadata

        if initiate is not None:
            method, reference, response = initiate(payment)
            payment.payment_method = method
            payment.gateway_transaction_reference = reference
            payment.gateway_response = response or {}
        payment.save()
        payment.already_open = False
        return payment

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _verify_amount_and_currency(payment: Payment, verified_data: dict) -> None:
        try:
            reported_amount = Decimal(str(verified_data['amount'])).quantize(Decimal('0.01'))
        except (KeyError, InvalidOperation, TypeError):
            raise PaymentVerificationError("Gateway did not report a valid amount")

        expected_amount = Decimal(payment.amount).quantize(Decimal('0.01'))
        if reported_amount != expected_amount:
            raise PaymentVerificationError(
                f"Amount mismatch: expected {expected_amount}, gateway reported {reported_amount}"
            )

        reported_currency = str(verified_data.get('currency', payment.currency)).upper()
        if reported_currency != str(payment.currency).upper():
            raise PaymentVerificationError(
                f"Currency mismatch: expected {payment.currency}, gateway reported {reported_currency}"
            )

    @staticmethod
    def _lock_subscription_and_payment(payment_id):
        """Lock a payment's subscription, THEN the payment, and return both.

        The subscription is locked because two payments for one subscription are
        different rows: without it both would read the same end_date and both write
        base + duration, the later write overwriting the earlier. It is locked FIRST
        because a checkout starts from the subscription and then touches its payments;
        taking them the other way round here was a deadlock waiting for a webhook to land
        during a tap.

        `of=('self',)`: `select_for_update()` with `select_related('subscription__plan')`
        also locked the PLAN row, so every completion for any subscriber on a plan queued
        behind every other one, and so did creating a subscription on it (the foreign key
        check needs a share lock the update lock excludes).
        """
        subscription_id = (Payment.objects.filter(pk=payment_id)
                           .values_list('subscription_id', flat=True).first())
        if subscription_id is None:
            raise Payment.DoesNotExist(f"Payment {payment_id} does not exist")
        subscription = (Subscription.objects.select_for_update(of=('self',))
                        .select_related('plan').get(pk=subscription_id))
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        payment.subscription = subscription
        return subscription, payment

    @staticmethod
    def _activate_subscription(subscription: Subscription, source: str):
        """Grant one plan period for a payment that cleared; return the window it bought.

        `source` is where that money came from (`policy.money.EntitlementSource`); it is
        recorded and never consulted for access.

        The period runs from when the money cleared, or from the end of cover the
        customer already holds — never from a date written before they paid. The first
        payment kept the `end_date` that `Subscription.save()` derived when the PENDING
        row was created at checkout, whenever that date was still in the future: a
        30-day plan paid ten days after opening the checkout delivered 20.00 days, and
        paid on day 29 delivered one. And a payment that was not tagged as a renewal
        added nothing at all if cover was already running, yet was marked completed,
        invoiced and paid out to the trainer.

        The caller holds the subscription lock.
        """
        now = timezone.now()
        duration = timedelta(days=subscription.plan.duration_days)

        if subscription.is_active:
            # Cover the customer already holds is kept, and the new period follows it.
            # A trial's cover ends at its trial end.
            base = subscription.end_date
            if subscription.trial_end_date:
                base = min(base, subscription.trial_end_date)
        else:
            # Pending, lapsed or cancelled: nothing is running, so the paid period starts
            # now, and so does the row's own window. A start date left at checkout time
            # would show a customer who paid today a period that began ten days ago.
            base = now
            subscription.start_date = now

        subscription.end_date = base + duration
        # Paid, so no longer a trial. `is_active` refuses a row whose trial has ended, and
        # this was never cleared: a trial user who paid after the trial got
        # status='active' and no access.
        subscription.trial_end_date = None
        subscription.status = 'active'
        subscription.source = source
        # `auto_renew` is NOT touched. It is the customer's setting, and writing True on
        # every completion silently undid a customer who had turned it off.
        subscription.save()
        return base, subscription.end_date

    @staticmethod
    def _cover_to_take_back(payment: Payment, subscription: Subscription) -> timedelta:
        """How much of the subscription's current cover a refunded payment paid for.

        The length of the window it bought — not its dates. A refund that takes a period
        out of the middle of a run of renewals moves every later period earlier, so the
        later payments' recorded dates stop matching the row while their lengths still do.

        Nothing, if the subscription has restarted since that window began. Activation
        moves `start_date` to the moment the money cleared whenever cover had lapsed or
        been cancelled, so a window that began before `start_date` belongs to cover that
        already ran out: refunding January's payment in March must not take March's
        paid month with it.
        """
        began = payment.covers_from or payment.created_at
        if subscription.start_date and began and began < subscription.start_date:
            return timedelta(0)
        if payment.covers_from and payment.covers_until:
            return payment.covers_until - payment.covers_from
        # Completed before the window was recorded. One plan period is what a completion
        # buys, which is the best statement available about a row that never said — and
        # `created_at` stands in above for the window it never recorded, so a refund of an
        # old row still cannot take back cover bought after the subscription restarted.
        return timedelta(days=subscription.plan.duration_days)

    @staticmethod
    def _generate_invoice_number() -> str:
        return f"INV-{timezone.now().year}-{uuid.uuid4().hex[:12].upper()}"
