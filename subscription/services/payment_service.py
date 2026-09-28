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

EVERY TRANSITION IS AN EVENT (the spec, section 7; plan step 5). A payment's `status` is
assigned in exactly one line of this module, `_move`, and that line writes a
`PaymentEvent` — who or what (`by`, `actor`), when, from, to, and what it rested on
(`evidence`) — in the same transaction as the transition, so a transition that rolls
back takes its event with it and one that commits cannot lack it. A checkout's opening
is the first event (`_opened`, `from_status` empty). The events are immutable at the
database (migration 0016) and read by the app on `payments/{id}/status/` and
`payments/{id}/` (`events`), and by operators on the payment in the console. Every public
method that moves a state takes `by` (a `PaymentEvent.By`) and `actor` (the person, when
a person did it); callers say who they are, and anything that does not is `system`.

THE SPEC'S CHECKOUT SESSION IS THE PAYMENT ROW. Section 5 describes a checkout session
holding money_type, amount, currency, reference, expiry, status and the ShamCash payment
target; a `Payment` holds every one of them (`money_type`, `amount`, `currency`,
`gateway_transaction_reference`, `status`, and the target in
`gateway_response['instructions']`), and its expiry is `created_at +
PAYMENT_CHECKOUT_TTL_MINUTES` (`utils.checkout_expires_at`, the same two values the
poller closes on). One open checkout per subscription is enforced here under the
subscription lock, and one transfer confirms one payment through
`uniq_payment_transaction_id`. So there is no second table; the spec's states map onto
`Payment.status` like this:

    spec          Payment.status
    OPEN          pending (processing / authorized: a hosted gateway's own in-between
                  states; ShamCash transfer mode never writes them)
    MATCHING      not stored. The match runs inside one poll, reconcile, webhook or
                  sweep call, under the subscription and payment locks, and ends in the
                  same transaction: completed, needs_review, or no change. A customer's
                  "I paid" (reconcile) that finds nothing leaves it OPEN, and the
                  pollers keep looking — for 72 hours after it expires, too.
    CONFIRMED     completed
    NEEDS_REVIEW  needs_review
    REJECTED      rejected
    EXPIRED       expired (and failed / cancelled: the gateway said so)
    REFUNDED      refunded

Two transitions the spec's diagram does not draw are deliberate: `expired -> completed`
and `expired -> needs_review`, because money that arrives for a checkout the server
already closed is still the customer's money (late money, SIDE_BUGS S-077).

The web hub reads and opens these rows as sessions (`POST/GET api/v1/checkout/sessions/`,
`subscription/checkout_views.py`): a plan's through `start_payment` / `start_renewal`,
the Pay and Renew buttons' own calls, and a ROUTINE PRODUCT's through
`start_product_checkout` — a payment with a `buyer` and a `routine_product` and no
subscription (`_lock_subscription_and_payment` then locks the payment alone, and the
order continues payment -> product -> purchase -> user row -> wallets). Every other
method here treats both alike; where they differ (completion delivers the program
instead of cover, a refund reverses the sale instead of cover) the method says so. The
payment step each checkout opens through is an adapter (`subscription/payment_step.py`).
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

from ..models import (OPEN_PAYMENT_STATUSES, Payment, PaymentEvent, Subscription,
                      SubscriptionPlan, TransferReview, plan_snapshot)

#: Who or what moved a payment (`PaymentEvent.By`), for callers of this service.
By = PaymentEvent.By

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


class AmountMismatch(PaymentVerificationError):
    """The gateway reported money for this payment, but not the amount or currency it
    asked for. Not a forgery to refuse: money that is almost right, which the webhook
    and the pollers hold for an operator (`hold_for_review`) instead of dropping."""


class ReviewConflict(PaymentError):
    """An operator decision the review queue cannot take as asked: the row is closed,
    the payment is not held on it, the transfer already confirmed another payment, or
    its currency is not the payment's. The message says which."""


class PlanChanged(PaymentError):
    """The subscription moved to another plan between the caller's quote and the lock
    (`change-plan` from the customer's other device). The caller priced one plan; a
    checkout opened now would charge another."""


class SubscriptionAlreadyActive(PaymentError):
    """A first payment was asked for while the subscription's cover is still running."""


class UpgradePriceMoved(PaymentError):
    """What is left to pay for an upgrade is not what the caller showed the customer:
    a day of credit ran out, or a price or the rate moved, between the quote and the
    lock. `quote` is the figure it would charge now."""

    def __init__(self, message, quote):
        super().__init__(message)
        self.quote = quote


class CheckoutOpen(PaymentError):
    """Another checkout is open for this subscription; `payment` is it. The customer
    may be transferring against its reference right now."""

    def __init__(self, message, payment):
        super().__init__(message)
        self.payment = payment


def _gateway_facts(data: dict | None) -> dict:
    """The part of what a gateway reported that an event keeps: ids and figures, never
    the raw payload (that is `Payment.gateway_response`)."""
    if not data:
        return {}
    keep = ('transaction_id', 'event_id', 'reference', 'amount', 'currency', 'status',
            'reason')
    return {key: str(data[key]) for key in keep if data.get(key) not in (None, '')}


class PaymentService:
    """Centralized, verified payment lifecycle operations."""

    # ------------------------------------------------------------------ #
    # The one place a state moves, and its record
    # ------------------------------------------------------------------ #
    @staticmethod
    def _move(payment: Payment, to_status: str, *, by: str, actor=None,
              evidence: dict | None = None) -> PaymentEvent:
        """Set `payment.status` to `to_status` and write the event that says so.

        The ONLY assignment of a payment's status in this module (a test reads the
        source to keep it so). The event is inserted now, inside the caller's
        transaction and under its locks; the caller saves the payment row in the same
        transaction. Either both land or neither does. The caller has already decided
        the transition is legal and is a change (`can_transition_to`, and the early
        returns on the same status), so every event is a real step.
        """
        event = PaymentEvent.objects.create(
            payment=payment, from_status=payment.status or '', to_status=to_status,
            actor=actor if getattr(actor, 'pk', None) else None, actor_role=str(by),
            evidence=evidence or {})
        payment.status = to_status
        return event

    @staticmethod
    def _opened(payment: Payment, *, by: str, actor=None,
                evidence: dict | None = None) -> PaymentEvent:
        """The checkout's first event, once its row is saved: '' -> its status. Opened
        by the customer unless the caller says otherwise, and then `actor` defaults to
        the payer (the subscriber, or a product checkout's buyer)."""
        actor_id = getattr(actor, 'pk', None)
        if actor_id is None and str(by) == By.CUSTOMER:
            actor_id = payment.payer_id
        return PaymentEvent.objects.create(
            payment=payment, from_status='', to_status=payment.status,
            actor_id=actor_id, actor_role=str(by),
            evidence={'reference': payment.gateway_transaction_reference or None,
                      'amount': str(payment.amount), 'currency': payment.currency,
                      'money_type': payment.money_type,
                      'payment_method': payment.payment_method,
                      'kind': (payment.metadata or {}).get('kind') or 'initial',
                      **(evidence or {})})

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    @classmethod
    def complete_payment(cls, payment_id, verified_data: dict, *, by: str = By.SYSTEM,
                         actor=None, evidence: dict | None = None) -> Payment:
        """
        Complete a payment and activate its subscription — the ONLY path to
        active access. `verified_data` must come from a trusted source (a
        signature-verified webhook, or a gateway status/transaction lookup that
        has already matched the transaction) and must contain at least:
            amount, currency, status  (+ optional transaction_id, reference, event_id)

        Idempotent: replays (same event id, or already-completed payment) return
        the existing payment without granting anything again — but a replay of a payment
        whose payout is still owed sends the payout again (see `redeliver_payout`).

        `by`/`actor` say who or what confirmed it (`PaymentEvent.By`); the event keeps
        the gateway's ids and figures from `verified_data`, plus `evidence`. On commit
        the customer is told (`payment_confirmed`, push and inbox: the receipt is ready)
        — only for a completion, never for a replay.

        A ROUTINE PRODUCT's checkout completes the same way, from the same callers (the
        pollers, the webhook, "check my payment", the review queue, the console), and
        what it buys is delivered in the same transaction: the buyer's copy and the
        sale's ledger rows, split as a wallet sale is (`routine.products.grant_checkout`).
        If the program cannot be delivered (`checkout_refusal`: owned already, or no
        longer deliverable) the money is held for review instead and the payment is
        returned `needs_review`.
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

            if payment.routine_product_id is not None:
                # A product's program must be deliverable before its money is taken as
                # paid for it. When it is not — the buyer owns it already, or it can no
                # longer be written or paid out — the money is real and is HELD for an
                # operator, never taken for nothing (`routine.products.checkout_refusal`).
                from routine.products import checkout_refusal

                refusal = checkout_refusal(payment)
                if refusal is not None:
                    raw = verified_data.get('gateway_data')
                    raw = raw if isinstance(raw, dict) else {}
                    cls.hold_for_review(payment.id, {
                        'note': raw.get('note') or raw.get('description') or '',
                        'sender': raw.get('sender') or raw.get('from') or '',
                        **verified_data,
                        'transaction_id': verified_data.get('transaction_id') or event_id,
                    }, reason=refusal, by=by, actor=actor)
                    payment.refresh_from_db()
                    logger.warning("Payment %s: money arrived for a program that cannot be "
                                   "delivered (%s); held for review", payment.id, refusal)
                    return payment

            # --- Apply completion ---
            cls._move(payment, 'completed', by=by, actor=actor,
                      evidence={**_gateway_facts(verified_data), **(evidence or {})})
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

            # A transfer queued for review that turns out to be THIS payment's exact money
            # (the sweep saw it before the poller matched it) is settled by this
            # completion, not left open as "money to hand back" (the review, L2).
            txn = str(verified_data.get('transaction_id') or event_id or '')
            if txn:
                TransferReview.objects.filter(transaction_id=txn, status='open').update(
                    status='approved', payment=payment, resolved_at=timezone.now(),
                    resolution_note='Settled automatically: this transfer completed the payment')

            # The payer's saved ShamCash address is PROVEN when this transfer came from
            # it and confirmed their own checkout by its exact reference and amount — the
            # gateway, the pollers, the sweep's settlement, "check my payment"; never an
            # operator accepting a near miss or marking it paid. Only a proven address
            # decides which checkout a reference-less transfer is held on (S-241).
            if (by in (By.GATEWAY, By.POLLER, By.SWEEP, By.CUSTOMER)
                    and payment.gateway_transaction_reference
                    and str(verified_data.get('reference') or '')
                    == payment.gateway_transaction_reference):
                from ..payer_address import verify_sender

                verify_sender(payment.payer_id, verified_data)

            if payment.routine_product_id is not None:
                # --- A routine product: the buyer's copy and the sale's ledger rows ---
                # In THIS transaction, under the payment lock: completion and delivery are
                # one fact, so there is no payout left owed (`payout_settled_at` now) and
                # `payment_completed` — a plan's payout and its receipt email — is not
                # sent. The pushes are the sale's own (`routine_product_purchased`,
                # `routine_product_sold`), on commit.
                from routine.products import grant_checkout

                payment.save()
                grant_checkout(payment)
                payment.payout_settled_at = timezone.now()
                payment.save(update_fields=['payout_settled_at', 'updated_at'])
                logger.info("Payment %s completed (invoice %s): routine product %s delivered",
                            payment.id, payment.invoice_number, payment.routine_product_id)
                # Its receipt email, as a plan's (the spec, section 7), which rides on
                # `payment_completed` — not sent for a product. On commit, and robust:
                # `queue_receipt` never raises, and an email never costs a completion
                # (BACKEND_BUGS B-167).
                transaction.on_commit(lambda: cls._queue_receipt_email(payment),
                                      robust=True)
                return payment

            # --- Activate / extend the subscription, and record what that bought ---
            # The plan and the period THIS payment was sold (its checkout's snapshot),
            # read before the row's plan is decided: a row with no snapshot reads its
            # period from the plan the row is on. Nothing running: the plan it bought.
            # Cover running: `_dearest_running_plan` (a late transfer after a
            # `change-plan`). The period granted is the one sold, so the receipt agrees.
            bought = cls._plan_bought(payment, subscription)
            source = source_of_payment_method(payment.payment_method)
            if (payment.metadata or {}).get('kind') == 'upgrade':
                payment.covers_from, payment.covers_until = cls._apply_upgrade(
                    payment, subscription, bought, source)
            else:
                days = payment.plan_bought()['duration_days'] or bought.duration_days
                subscription.plan = (
                    cls._dearest_running_plan(subscription, clearing=payment)
                    if subscription.is_active else bought)
                payment.covers_from, payment.covers_until = cls._activate_subscription(
                    subscription, source, days)
            payment.save()

            logger.info(
                f"Payment {payment.id} completed (invoice {payment.invoice_number}), "
                f"subscription {payment.subscription_id} activated"
            )

        # The customer hears the money arrived (the spec's table: payment confirmed —
        # push and inbox). Registered BEFORE the payout and `robust`: a notice that
        # cannot be queued is logged and never stops the payout behind it, and a payout
        # that fails (it raises, for a retry) never swallows the notice of a payment
        # that did commit.
        recipient_id = payment.subscription.user_id
        transaction.on_commit(lambda: cls._notify_confirmed(recipient_id, payment.id),
                              robust=True)
        # ON COMMIT. The comment here said "after commit-safe state", which was true of
        # this function and not of its callers: `PaymentWebhookView` wraps the whole
        # update in `transaction.atomic()`, so the send happened inside a transaction
        # that could still roll back — and the receiver moves money.
        transaction.on_commit(lambda: payment_completed.send(
            sender=cls, payment=payment, subscription=payment.subscription))
        return payment

    @staticmethod
    def _queue_receipt_email(payment) -> None:
        """The receipt email of a routine product's checkout, on commit. Never raises."""
        from notifications.channels.email import queue_receipt

        queue_receipt(payment)

    @staticmethod
    def _notify_confirmed(recipient_id, payment_id) -> None:
        """`payment_confirmed` to the customer, on commit. The fact only — no amount, no
        link, no button: a push reaches every build, and a store build may not sell the
        plan (the spec, section 7). `data` carries the payment's id and invoice number;
        its receipt is `payments/{payment_id}/receipt/`."""
        from notifications.domain.dispatcher import emit_event
        from notifications.domain.money_events import PaymentConfirmedEvent

        emit_event(PaymentConfirmedEvent(recipient_id=recipient_id,
                                         payment_id=str(payment_id)))

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
    def cancel_payment(cls, payment_id, reason: str = "", *, by: str = By.SYSTEM,
                       actor=None) -> Payment:
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
            cls._move(payment, 'cancelled', by=by, actor=actor,
                      evidence={'reason': (reason or 'Cancelled at the gateway')[:500]})
            payment.gateway_error = (reason or 'Cancelled at the gateway')[:2000]
            payment.save()
            logger.info("Payment %s cancelled (%s)", payment.id, reason or 'no reason')
        return payment

    @classmethod
    def refund_payment(cls, payment_id, reason: str = "", gateway_data: dict | None = None,
                       *, by: str = By.SYSTEM, actor=None) -> Payment:
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

            cls._move(payment, 'refunded', by=by, actor=actor,
                      evidence={**_gateway_facts(gateway_data),
                                'reason': (reason or '')[:500]})
            if reason:
                payment.gateway_error = reason[:2000]
            payment.save()

            if payment.routine_product_id is not None:
                # A routine product bought through a checkout: the sale is reversed and
                # the buyer's copy revoked, as the console's purchase refund does (it
                # comes here for such a sale). Refused — and nothing moves, this state
                # included — when a wallet the sale paid into no longer holds its part.
                from routine.products import ReviewConflict as SaleNotRefundable
                from routine.products import refund_checkout

                try:
                    refund_checkout(payment, actor, reason)
                except SaleNotRefundable as exc:
                    raise PaymentError(str(exc)) from exc
                logger.warning("Payment %s refunded (%s); routine product %s sale reversed",
                               payment.id, reason or 'no reason given',
                               payment.routine_product_id)
                return payment

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
            if subscription.is_active:
                # The cover left is what the OTHER payments bought, so the plan follows
                # them — the rule completion applies (`_dearest_running_plan`). Left
                # alone, refunding the Premium payment that set the plan kept a Basic
                # period on Premium's features. `save()` re-inherits the plan's flags.
                held = cls._dearest_running_plan(subscription)
                if held is not None:
                    subscription.plan = held
            subscription.save()

            return cls._finish_refund(payment, subscription, reason)

    @classmethod
    def _finish_refund(cls, payment: Payment, subscription: Subscription, reason: str):
        """The ledger half of a refund, once the access half is written. The caller
        holds the subscription and payment locks."""
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

    # ------------------------------------------------------------------ #
    # The review queue (the spec, section 7; BACKEND_BUGS B-051)
    # ------------------------------------------------------------------ #
    @classmethod
    def hold_for_review(cls, payment_id, facts: dict, *, reason: str,
                        gateway: str = 'shamcash', by: str = By.SYSTEM,
                        actor=None, candidates=()) -> TransferReview | None:
        """Hold money that is almost right for an operator, never drop it.

        `facts` is what the gateway saw (`transaction_facts`: transaction id, amount,
        currency, note, sender, when; `gateway_data` is kept as `raw`). One queue row per
        transaction, ever: seeing the same transfer again returns the row it already has,
        and a transfer that already confirmed a payment is not queued at all (returns
        None) — one transfer confirms one payment, once.

        `candidates` are the OTHER checkouts the transfer could be for (the sweep names
        them when the sender's saved address chose this one among several), kept on the
        row for the operator. Whether the transfer's sender is the address this
        checkout's customer saved (`PayerAddress`, the spec's second signal) is written
        onto the row and into the event's evidence (`sender_address_matched`): evidence,
        never a confirmation.

        A checkout that can still take money (open, or expired within the late-money
        window) becomes `needs_review`: it keeps its reference, blocks a second checkout,
        never expires, and the customer is told a person is checking it. A checkout
        already paid, or closed for good, is left as it is and the transfer is queued
        against it (`duplicate_transfer` / `checkout_closed`): money that needs handing
        back, which only an operator can do.
        """
        txn_id = str(facts.get('transaction_id') or facts.get('event_id') or '')
        if not txn_id:
            raise PaymentVerificationError("A transfer with no transaction id cannot be held")
        from django.db.models import Q

        with transaction.atomic():
            subscription, payment = cls._lock_subscription_and_payment(payment_id)
            existing = TransferReview.objects.select_for_update().filter(
                transaction_id=txn_id).first()
            if existing is not None:
                return existing
            if Payment.objects.filter(Q(transaction_id=txn_id) | Q(gateway_event_id=txn_id)).exists():
                logger.info("Transfer %s already confirmed a payment; not queued", txn_id)
                return None
            holds = payment.status == 'needs_review' or payment.can_transition_to('needs_review')
            if holds and payment.status == 'expired':
                # Closed for good once the late-money window has passed: the customer has
                # moved on and may hold a newer checkout. The money is queued against it,
                # the row is left as it is (the review, L2).
                from django.conf import settings

                late = timedelta(hours=int(getattr(settings, 'PAYMENT_LATE_MONEY_WINDOW_HOURS', 72)))
                if payment.created_at < timezone.now() - late:
                    holds = False
            if not holds:
                reason = ('duplicate_transfer' if payment.status in ('completed', 'refunded')
                          else 'checkout_closed')
            review = cls._queue_transfer(facts, txn_id, reason=reason, payment=payment,
                                         candidates=[c for c in candidates
                                                     if str(c) != str(payment.pk)],
                                         gateway=gateway)
            if holds and payment.status != 'needs_review':
                meta = dict(payment.metadata or {})
                meta['review'] = {'reason': reason, 'transaction_id': txn_id,
                                  'held_at': timezone.now().isoformat()}
                payment.metadata = meta
                cls._move(payment, 'needs_review', by=by, actor=actor, evidence={
                    'review_id': str(review.pk), 'reason': reason, 'gateway': gateway,
                    'transaction_id': txn_id, 'amount': str(review.amount),
                    'currency': review.currency,
                    'sender_address_matched':
                        str(payment.pk) in (review.sender_match_payment_ids or [])})
                payment.save(update_fields=['status', 'metadata', 'updated_at'])
                logger.warning("Payment %s held for review (%s, transfer %s)",
                               payment.id, reason, txn_id)
                recipient_id = payment.payer_id
                transaction.on_commit(lambda: cls._notify_review(
                    'payment_needs_review', recipient_id, payment.id))
            return review

    @staticmethod
    def _queue_transfer(facts, txn_id, *, reason, payment=None, candidates=(),
                        gateway='shamcash') -> TransferReview:
        """The queue row for one transfer (the caller has checked it is new), with the
        second signal written on it: which of its checkouts belong to a customer who
        saved the transfer's sender address (`sender_match_payment_ids`)."""
        from django.utils.dateparse import parse_datetime

        from ..payer_address import sender_matches

        try:
            amount = Decimal(str(facts.get('amount'))).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            amount = Decimal('0.00')
        occurred = facts.get('occurred_at')
        if isinstance(occurred, str):
            occurred = parse_datetime(occurred)
        raw = facts.get('gateway_data')
        return TransferReview.objects.create(
            gateway=gateway, transaction_id=txn_id, amount=amount,
            currency=str(facts.get('currency') or '').upper()[:3],
            note=str(facts.get('note') or '')[:500], sender=str(facts.get('sender') or '')[:255],
            occurred_at=occurred if hasattr(occurred, 'tzinfo') else None,
            raw=raw if isinstance(raw, dict) else {}, reason=reason, payment=payment,
            candidate_payment_ids=[str(c) for c in candidates],
            sender_match_payment_ids=sender_matches(
                facts, [payment.pk if payment is not None else None, *candidates]))

    @classmethod
    def queue_unclaimed_transfer(cls, facts: dict, *, candidates=(),
                                 gateway: str = 'shamcash') -> TransferReview | None:
        """Queue a transfer no checkout can be named for: no reference, and an amount
        that fits no open checkout (`no_candidate`) or several (`several_candidates`).
        An operator attaches it (`attach_review`) or dismisses it."""
        from django.db.models import Q

        txn_id = str(facts.get('transaction_id') or '')
        if not txn_id:
            return None
        with transaction.atomic():
            if TransferReview.objects.filter(transaction_id=txn_id).exists():
                return None
            if Payment.objects.filter(Q(transaction_id=txn_id) | Q(gateway_event_id=txn_id)).exists():
                return None
            return cls._queue_transfer(
                facts, txn_id, reason='several_candidates' if candidates else 'no_candidate',
                candidates=candidates, gateway=gateway)

    @classmethod
    def approve_review(cls, review_id, operator, note: str = '') -> Payment:
        """An operator accepts a held transfer as payment for its checkout.

        The payment completes through `complete_payment`, with the transfer as the
        verified money — so everything a payment does on completion happens here too:
        the period is granted, the ledger records it, the receipt is issued. If the
        transfer's amount is not the checkout's, the operator is accepting THAT amount
        for it: the payment is recorded at what arrived (the price it was opened at is
        kept in `metadata['review']['price']`), so the ledger and the receipt say what
        was actually received. A transfer in another currency cannot be approved: reject
        it and hand the money back.
        """
        from django.db.models import Q

        payment_id = (TransferReview.objects.filter(pk=review_id)
                      .values_list('payment_id', flat=True).first())
        if payment_id is None:
            raise ReviewConflict("This transfer is not held on a checkout; attach it first")
        with transaction.atomic():
            subscription, payment = cls._lock_subscription_and_payment(payment_id)
            review = TransferReview.objects.select_for_update().get(pk=review_id)
            if review.status != 'open' or review.payment_id != payment.id:
                raise ReviewConflict(f"Review {review.id} is {review.status}")
            if payment.status != 'needs_review':
                raise ReviewConflict(f"Payment {payment.id} is {payment.status}, not held")
            if review.currency != str(payment.currency).upper():
                raise ReviewConflict(f"The transfer is in {review.currency} and the checkout "
                                     f"in {payment.currency}: reject it and hand it back")
            if Payment.objects.filter(Q(transaction_id=review.transaction_id)
                                      | Q(gateway_event_id=review.transaction_id)
                                      ).exclude(pk=payment.pk).exists():
                raise ReviewConflict(f"Transfer {review.transaction_id} already confirmed "
                                     f"another payment")
            if payment.routine_product_id is not None:
                # Accepting money for a program that cannot be delivered would take it
                # for nothing: such a transfer is rejected and handed back.
                from routine.products import checkout_refusal

                refusal = checkout_refusal(payment)
                if refusal is not None:
                    raise ReviewConflict(f"This program cannot be delivered ({refusal}): "
                                         f"reject the transfer and hand the money back")
            meta = dict(payment.metadata or {})
            record = dict(meta.get('review') or {})
            record.update({'approved_by': operator.pk if operator else None,
                           'transaction_id': review.transaction_id})
            if review.amount != payment.amount:
                record['price'] = str(payment.amount)
                payment.amount = review.amount
            meta['review'] = record
            payment.metadata = meta
            payment.save(update_fields=['amount', 'metadata', 'updated_at'])
            review.status = 'approved'
            review.resolved_by = operator
            review.resolved_at = timezone.now()
            review.resolution_note = (note or '')[:500]
            review.save(update_fields=['status', 'resolved_by', 'resolved_at',
                                       'resolution_note'])
            return cls.complete_payment(payment.id, {
                'amount': str(review.amount), 'currency': review.currency,
                'status': 'completed', 'transaction_id': review.transaction_id,
                'event_id': review.transaction_id,
                'reference': payment.gateway_transaction_reference,
                'gateway_data': review.raw or None,
            }, by=By.OPERATOR, actor=operator, evidence={
                'review_id': str(review.pk), 'note': review.resolution_note,
                **({'price': record['price']} if 'price' in record else {})})

    @classmethod
    def reject_review(cls, review_id, operator, note: str = '') -> TransferReview:
        """An operator refuses a held transfer. The money goes back to its sender by
        hand — nothing here moves it — and `note` should say so.

        Its checkout, if held on it and on no other open transfer, becomes `rejected`:
        it stops blocking, the customer can open a new one, and they are told."""
        payment_id = (TransferReview.objects.filter(pk=review_id)
                      .values_list('payment_id', flat=True).first())
        with transaction.atomic():
            payment = None
            if payment_id is not None:
                subscription, payment = cls._lock_subscription_and_payment(payment_id)
            review = TransferReview.objects.select_for_update().get(pk=review_id)
            if review.status != 'open':
                raise ReviewConflict(f"Review {review.id} is {review.status}")
            if review.payment_id != payment_id:
                # Attached to a checkout while this call waited for the lock: the decision
                # was taken on another state. Refused; ask again (the review, L6).
                raise ReviewConflict(f"Review {review.id} changed checkout; try again")
            review.status = 'rejected'
            review.resolved_by = operator
            review.resolved_at = timezone.now()
            review.resolution_note = (note or '')[:500]
            review.save(update_fields=['status', 'resolved_by', 'resolved_at',
                                       'resolution_note'])
            others = (payment is not None and TransferReview.objects.filter(
                payment=payment, status='open').exists())
            if payment is not None and payment.status == 'needs_review' and not others:
                cls._move(payment, 'rejected', by=By.OPERATOR, actor=operator, evidence={
                    'review_id': str(review.pk), 'note': review.resolution_note,
                    'transaction_id': review.transaction_id})
                payment.gateway_error = (note or 'Rejected on review')[:2000]
                payment.save(update_fields=['status', 'gateway_error', 'updated_at'])
                recipient_id = payment.payer_id
                transaction.on_commit(lambda: cls._notify_review(
                    'payment_rejected', recipient_id, payment.id))
            return review

    @classmethod
    def attach_review(cls, review_id, payment_id, operator) -> TransferReview:
        """An operator names the checkout a queued transfer was for. The checkout is
        then held on it (`needs_review`) and the operator approves or rejects as for
        any other. Only a transfer not already held on a checkout can be attached."""
        with transaction.atomic():
            subscription, payment = cls._lock_subscription_and_payment(payment_id)
            review = TransferReview.objects.select_for_update().get(pk=review_id)
            if review.status != 'open':
                raise ReviewConflict(f"Review {review.id} is {review.status}")
            if review.payment_id is not None and review.payment_id != payment.id:
                held = Payment.objects.filter(pk=review.payment_id,
                                              status='needs_review').exists()
                if held:
                    raise ReviewConflict("This transfer holds another checkout: reject it "
                                         "there first")
            if review.currency != str(payment.currency).upper():
                raise ReviewConflict(f"The transfer is in {review.currency} and the checkout "
                                     f"in {payment.currency}")
            if not (payment.status == 'needs_review'
                    or payment.can_transition_to('needs_review')):
                raise ReviewConflict(f"Payment {payment.id} is {payment.status}")
            review.payment = payment
            review.save(update_fields=['payment'])
            if payment.status != 'needs_review':
                meta = dict(payment.metadata or {})
                meta['review'] = {'reason': review.reason,
                                  'transaction_id': review.transaction_id,
                                  'held_at': timezone.now().isoformat(),
                                  'attached_by': operator.pk if operator else None}
                payment.metadata = meta
                cls._move(payment, 'needs_review', by=By.OPERATOR, actor=operator,
                          evidence={'review_id': str(review.pk), 'reason': review.reason,
                                    'attached': True,
                                    'transaction_id': review.transaction_id,
                                    'amount': str(review.amount),
                                    'currency': review.currency})
                payment.save(update_fields=['status', 'metadata', 'updated_at'])
                recipient_id = payment.payer_id
                transaction.on_commit(lambda: cls._notify_review(
                    'payment_needs_review', recipient_id, payment.id))
            return review

    @classmethod
    def dismiss_review(cls, review_id, operator, note: str = '') -> TransferReview:
        """An operator closes a queued transfer that is not a customer's payment for any
        checkout. A transfer holding a checkout cannot be dismissed: reject it."""
        with transaction.atomic():
            review = TransferReview.objects.select_for_update().get(pk=review_id)
            if review.status != 'open':
                raise ReviewConflict(f"Review {review.id} is {review.status}")
            if review.payment_id and Payment.objects.filter(
                    pk=review.payment_id, status='needs_review').exists():
                raise ReviewConflict("This transfer holds a checkout: reject it instead")
            review.status = 'dismissed'
            review.resolved_by = operator
            review.resolved_at = timezone.now()
            review.resolution_note = (note or '')[:500]
            review.save(update_fields=['status', 'resolved_by', 'resolved_at',
                                       'resolution_note'])
            return review

    @staticmethod
    def _notify_review(event_type: str, recipient_id, payment_id) -> None:
        """Push and inbox for a review decision (the spec's table: needs review, and
        rejected — both push and in-app, no email). Called on commit."""
        from notifications.services import NotificationService
        from users.models import CustomUser

        recipient = CustomUser.objects.filter(pk=recipient_id).first()
        if recipient is None:
            return
        if event_type == 'payment_needs_review':
            NotificationService.create_and_send(
                recipient=recipient, event_type="payment_needs_review",
                related_object_id=str(payment_id),
                metadata={'context': {}, 'data': {'type': 'payment_needs_review',
                                                  'payment_id': str(payment_id)}})
        else:
            NotificationService.create_and_send(
                recipient=recipient, event_type="payment_rejected",
                related_object_id=str(payment_id),
                metadata={'context': {}, 'data': {'type': 'payment_rejected',
                                                  'payment_id': str(payment_id)}})

    @classmethod
    def fail_payment(cls, payment_id, error_message: str = "", *, by: str = By.SYSTEM,
                     actor=None) -> Payment:
        """Mark a payment failed (legal from pending/processing/authorized)."""
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(id=payment_id)
            if payment.status == 'failed':
                return payment
            if not payment.can_transition_to('failed'):
                raise InvalidPaymentTransition(f"Payment {payment.id} cannot go {payment.status} -> failed")
            cls._move(payment, 'failed', by=by, actor=actor,
                      evidence={'error': (error_message or 'Payment failed')[:500]})
            payment.gateway_error = (error_message or 'Payment failed')[:2000]
            payment.save()
        return payment

    @classmethod
    def expire_payment(cls, payment_id, reason: str = "", *, by: str = By.SYSTEM,
                       actor=None) -> Payment:
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
            cls._move(payment, 'expired', by=by, actor=actor,
                      evidence={'reason': (reason or 'The checkout expired')[:500]})
            payment.gateway_error = (reason or 'The checkout expired')[:2000]
            payment.save()
            logger.info("Payment %s expired (%s)", payment.id, reason or 'timed out')
        return payment

    @classmethod
    def mark_processing(cls, payment_id, *, by: str = By.SYSTEM, actor=None) -> Payment:
        """Optional intermediate state once the gateway has accepted the request."""
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(id=payment_id)
            if payment.status == 'processing':
                return payment
            if not payment.can_transition_to('processing'):
                raise InvalidPaymentTransition(f"Payment {payment.id} cannot go {payment.status} -> processing")
            cls._move(payment, 'processing', by=by, actor=actor)
            payment.save()
        return payment

    @classmethod
    def start_renewal(cls, subscription: Subscription, initiate=None, *,
                      by: str = By.CUSTOMER, actor=None,
                      evidence: dict | None = None) -> Payment:
        """
        Begin a renewal: return the subscription's open checkout, opening one if it has
        none. This never mutates the subscription — activation happens later via
        complete_payment once the gateway confirms funds.

        `initiate(payment)` opens the gateway session and returns
        `(payment_method, reference, gateway_response)`; see `_checkout_under_lock`.
        Without it the payment is created without a reference.

        A checkout opened here writes its first event (`_opened`): by the customer
        unless `by` says otherwise; `evidence` is what the caller adds to it (the build
        that asked: `channel`, `version`).
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
            return cls._checkout_under_lock(locked, open_payments, 'renewal', initiate,
                                            by=by, actor=actor, evidence=evidence)

    @classmethod
    def start_payment(cls, subscription: Subscription, initiate,
                      expected_plan_id=None, *, by: str = By.CUSTOMER, actor=None,
                      evidence: dict | None = None) -> Payment:
        """The Pay button: the subscription's open checkout, opening one if it has none.

        Refused while cover is running — extending an active subscription is what renew
        is for. That check used to be the ONLY one on this path: nothing deduplicated,
        so two taps on a subscription that was pending or had lapsed each created a
        Payment with its own reference, both payable, and the second bought nothing.

        `expected_plan_id` is the plan the caller priced and checked the customer's
        quote against, read without this lock. If the row has moved to another plan
        since (`change-plan`), `PlanChanged` is raised rather than opening a checkout
        for a price nobody quoted.
        """
        with transaction.atomic():
            locked = (Subscription.objects.select_for_update(of=('self',))
                      .select_related('plan').get(pk=subscription.pk))
            if locked.is_active:
                raise SubscriptionAlreadyActive(
                    f"Subscription {locked.id} is already active; renew extends it")
            if expected_plan_id is not None and locked.plan_id != expected_plan_id:
                raise PlanChanged(f"Subscription {locked.id} moved from plan "
                                  f"{expected_plan_id} to {locked.plan_id}")
            open_payments = Payment.objects.filter(
                subscription=locked, status__in=OPEN_PAYMENT_STATUSES)
            return cls._checkout_under_lock(locked, open_payments, 'initial', initiate,
                                            by=by, actor=actor, evidence=evidence)

    @classmethod
    def start_upgrade(cls, subscription: Subscription, to_plan: SubscriptionPlan, initiate,
                      *, expected_from_plan_id, expected_to_pay=None,
                      gateway_name=None, by: str = By.CUSTOMER, actor=None,
                      evidence: dict | None = None) -> Payment:
        """Open the checkout that moves a RUNNING subscription onto a dearer plan.

        The owner (2026-09-23): an upgrade is allowed while a plan runs, a downgrade is
        not; what is left of the current plan is credited by days; the customer pays
        the new plan's price minus that credit, and the new plan starts, for its full
        length, when that payment clears (`_apply_upgrade`). The price is decided HERE,
        under the subscription lock (`subscription.upgrade.quote_upgrade`), and written
        onto the payment with the credit it was struck at.

        `expected_from_plan_id` is the plan the caller quoted the upgrade from, read
        without this lock; if the row has moved since, `PlanChanged`. `expected_to_pay`
        is the figure the customer was shown; if the quote under the lock differs,
        `UpgradePriceMoved` — no checkout is opened for a figure nobody saw.

        One checkout at a time, as for every other button: an open checkout that is
        this same upgrade comes back as it is (`already_open`), anything else open is
        `CheckoutOpen`. Raises `subscription.upgrade.UpgradeRefused` subclasses and
        `ExchangeRateUnavailable` from the quote.
        """
        from wallet import currency_for as _currency_for

        from ..upgrade import quote_upgrade

        with transaction.atomic():
            locked = (Subscription.objects.select_for_update(of=('self',))
                      .select_related('plan', 'user').get(pk=subscription.pk))
            if expected_from_plan_id is not None and locked.plan_id != expected_from_plan_id:
                raise PlanChanged(f"Subscription {locked.id} moved from plan "
                                  f"{expected_from_plan_id} to {locked.plan_id}")
            open_payments = list(Payment.objects
                                 .filter(subscription=locked, status__in=OPEN_PAYMENT_STATUSES)
                                 .order_by('created_at', 'id'))
            for payment in open_payments:
                if not payment.gateway_transaction_reference:
                    continue
                meta = payment.metadata or {}
                if payment.status == 'needs_review':
                    # A person is checking money that arrived for it: not payable, not
                    # this call's to hand back (the review, L1).
                    raise CheckoutOpen(f"Subscription {locked.id} has checkout "
                                       f"{payment.id} held for review", payment)
                if meta.get('kind') == 'upgrade' and payment.plan_bought()['plan_id'] == to_plan.pk:
                    payment.already_open = True
                    return payment
                raise CheckoutOpen(f"Subscription {locked.id} has checkout {payment.id} open",
                                   payment)

            quote = quote_upgrade(locked, to_plan, buyer_currency=_currency_for(locked.user),
                                  gateway_name=gateway_name)
            if expected_to_pay is not None and quote.to_pay != expected_to_pay:
                raise UpgradePriceMoved(f"Upgrade of {locked.id} now costs {quote.to_pay}, "
                                        f"not {expected_to_pay}", quote)
            # A row never offered to anyone (no reference) is adopted, as the Pay button
            # does, rather than left beside a new one.
            unoffered = [p for p in open_payments if not p.gateway_transaction_reference]
            payment = unoffered[0] if unoffered else Payment(subscription=locked,
                                                             status='pending')
            if unoffered:
                payment.created_at = timezone.now()
            payment.subscription = locked
            payment.amount = quote.to_pay
            payment.currency = quote.currency
            # The price of record is the NEW plan's: what the dearest-plan rule and the
            # receipt compare. The charge is that price minus the credit.
            payment.price_amount = quote.priced.price_amount
            payment.price_currency = quote.priced.price_currency
            payment.exchange_rate = quote.priced.rate
            payment.exchange_rate_value = getattr(quote.priced.rate, 'rate', None)
            payment.metadata = {'kind': 'upgrade', 'plan': plan_snapshot(to_plan),
                                'upgrade': quote.as_record()}
            payment.description = f"Upgrade to {to_plan.name}"
            method, reference, response = initiate(payment)
            payment.payment_method = method
            payment.gateway_transaction_reference = reference
            payment.gateway_response = response or {}
            payment.save()
            if not unoffered:
                # A row that was never offered keeps the event it was written with.
                cls._opened(payment, by=by, actor=actor, evidence=evidence)
            payment.already_open = False
            payment.quote = quote
            return payment

    @classmethod
    def start_product_checkout(cls, product_id, buyer, initiate, *, by: str = By.CUSTOMER,
                               actor=None, evidence: dict | None = None) -> Payment:
        """A routine product's checkout (the web hub, the spec's section 5): the buyer's
        open checkout for this product, opening one if there is none.

        ONE OPEN CHECKOUT PER BUYER PER PRODUCT, decided under the PRODUCT's row lock —
        the lock a wallet sale of it takes first (`routine.products.buy`) — so a double
        tap waits and then receives the first tap's checkout, reference and all
        (`already_open`); `uniq_open_product_checkout` is the database's backstop. A
        checkout held for review is still open and still the one handed back.

        A new one is priced now and snapshotted on the row (`routine.products.
        checkout_terms`): the price in the buyer's own currency at the operator's rate,
        the price of record, and the commission rate the sale will be split at — "at
        the moment the price is locked" (the spec, section 8). `initiate` is the payment
        step's opener (`subscription/payment_step.py`), run under the lock. Its first
        event is written here (`_opened`).

        Raises `routine.products.ProductNotForSale` / `AlreadyOwned` and
        `ExchangeRateUnavailable` before anything is written. The CALLER has already
        asked `require_purchase_allowed(request, ROUTINE_PRODUCT)`.
        """
        from policy.money import MoneyType

        from routine import products

        with transaction.atomic():
            product = products.lock_for_checkout(product_id)
            open_checkout = (Payment.objects
                             .filter(buyer=buyer, routine_product=product,
                                     status__in=OPEN_PAYMENT_STATUSES)
                             .order_by('created_at', 'id').first())
            if open_checkout is not None:
                logger.info("Checkout %s already open for product %s", open_checkout.id,
                            product.pk)
                open_checkout.already_open = True
                return open_checkout
            priced, snapshot = products.checkout_terms(product, buyer)
            payment = Payment(
                buyer=buyer, routine_product=product, status='pending',
                money_type=MoneyType.ROUTINE_PRODUCT,
                amount=priced.amount, currency=priced.currency,
                price_amount=priced.price_amount, price_currency=priced.price_currency,
                exchange_rate=priced.rate,
                exchange_rate_value=getattr(priced.rate, 'rate', None),
                description=f"Routine product {product.pk}",
                metadata={'kind': 'routine_product', 'product': snapshot})
            method, reference, response = initiate(payment)
            payment.payment_method = method
            payment.gateway_transaction_reference = reference
            payment.gateway_response = response or {}
            payment.save()
            cls._opened(payment, by=by, actor=actor, evidence=evidence)
            payment.already_open = False
            return payment

    @classmethod
    def _checkout_under_lock(cls, subscription: Subscription, open_payments, kind: str,
                             initiate, *, by: str = By.CUSTOMER, actor=None,
                             evidence: dict | None = None) -> Payment:
        """Return the open checkout, or open one. The caller holds the subscription lock.

        * An open payment that already has a reference is THE checkout: the customer may
          already be paying against it. It comes back as it is, with the gateway
          response it was opened with, and `already_open` set.
        * An open payment with no reference was never offered to anyone, so it is
          adopted rather than left beside a new one, and its window starts now. POST
          /subscriptions/ used to write exactly such a row (it no longer does — S-134);
          nothing could ever complete it, and every subscriber's history carried it as
          an orphan 'pending' payment beside the one that cleared. Rows written before
          that change, and a renewal reserved with no `initiate`, are still adopted.
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
                # An open UPGRADE, or a payment a person is checking (B-051), is not
                # this button's checkout: handed back as one, the Renew screen drew
                # "renew Basic" at an upgrade's price, and paying it switched the plan
                # (the review of 2026-09-23, U12). Refused, with which one it is.
                if ((payment.metadata or {}).get('kind') == 'upgrade'
                        or payment.status == 'needs_review'):
                    raise CheckoutOpen(f"Subscription {subscription.id} has "
                                       f"{payment.status} checkout {payment.id} open", payment)
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
        if candidates and initiate is not None:
            # The checkout OPENS now, whenever the adopted row was written. Its window —
            # `utils.checkout_expires_at` and the poller's expiry test — is measured from
            # `created_at`, and the adopted row kept the moment the plan was CHOSEN: a
            # plan picked yesterday and paid for today opened a checkout that was born
            # expired, and the poller closed it within a minute while the customer was
            # transferring money against its reference (SIDE_BUGS S-075). Nothing was
            # ever offered on the row before this, so its old timestamp records nothing.
            payment.created_at = timezone.now()
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
        # What this checkout sells, as it is sold now. The subscription can move to
        # another plan later (B-047), so completion, the history row and the receipt
        # read the plan from here, not from the subscription (`Payment.plan_bought`).
        metadata['plan'] = plan_snapshot(plan)
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
        if not candidates:
            # The checkout's first event. An adopted row changes no state (it was open
            # and stays open) and keeps the event it was written with.
            cls._opened(payment, by=by, actor=actor, evidence=evidence)
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
            raise AmountMismatch(
                f"Amount mismatch: expected {expected_amount}, gateway reported {reported_amount}"
            )

        reported_currency = str(verified_data.get('currency', payment.currency)).upper()
        if reported_currency != str(payment.currency).upper():
            raise AmountMismatch(
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

        A routine product's checkout has no subscription: `(None, payment)`, the payment
        row locked alone. Its own order continues payment -> product -> purchase -> the
        buyer's user row -> wallets (`routine/products.py`).
        """
        row = Payment.objects.filter(pk=payment_id).values('subscription_id').first()
        if row is None:
            raise Payment.DoesNotExist(f"Payment {payment_id} does not exist")
        if row['subscription_id'] is None:
            return None, Payment.objects.select_for_update().get(pk=payment_id)
        subscription = (Subscription.objects.select_for_update(of=('self',))
                        .select_related('plan').get(pk=row['subscription_id']))
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        payment.subscription = subscription
        return subscription, payment

    @staticmethod
    def _plan_bought(payment: Payment, subscription: Subscription) -> SubscriptionPlan:
        """The plan this payment was priced for (its checkout's snapshot), as a row.

        The subscription's own plan when the snapshot names it, when there is none (a
        row opened before snapshots), or when the snapshot's plan row is gone — plans
        are PROTECTed, so that last one is a hand edit, and the old behaviour is safe.
        """
        plan_id = payment.plan_bought()['plan_id']
        if plan_id == subscription.plan_id:
            return subscription.plan
        plan = SubscriptionPlan.objects.filter(pk=plan_id).first()
        if plan is None:
            logger.warning("Payment %s names plan %s, which does not exist; granting the "
                           "subscription's plan", payment.id, plan_id)
            return subscription.plan
        return plan

    @staticmethod
    def _price_of_record(payment: Payment):
        """What the plan this payment bought was priced at: `(amount, currency)` in the
        PLAN's currency, as snapshotted at checkout. A buyer who switches currency pays
        later plans in the other one, so the charged amounts of two payments do not
        compare; their prices of record do. Rows with no snapshot give what they charged.
        """
        if payment.price_amount is not None and payment.price_currency:
            return payment.price_amount, payment.price_currency
        return payment.amount, payment.currency

    @classmethod
    def _dearest_running_plan(cls, subscription: Subscription, clearing: Payment | None = None):
        """The plan the paid periods still running entitle the subscription to.

        `change-plan` is refused while cover runs or a checkout is open, so two paid
        periods on different plans can only meet through a LATE transfer against a
        checkout that expired before the change (`expired -> completed` is legal: the
        money is real). One rule decides the plan then, when a payment clears
        (`clearing`, the payment being completed) and when one is refunded:

        * Among this run's completed payments whose period has not ended, plus `clearing`, the
          plan with the highest price of record (`_price_of_record`). Keeping the
          cheaper one would take from a customer what they paid for; the periods stack
          either way. A tie keeps the plan the row is on.
        * Prices of record in different currencies do not compare without a rate: the
          plan the row is on stays if one of those payments bought it, else the most
          recently completed payment's plan.
        * No paid period running (a trial, an operator's grant): `clearing`'s plan, or
          None when there is nothing to decide — the caller leaves the row alone.

        The caller holds the subscription lock.
        """
        now = timezone.now()
        # This RUN's payments only (`Subscription.current_run_payments`): an immediate
        # cancel leaves its payment `completed` with `covers_until` still ahead, and
        # counting it let a Premium month the customer gave up move a Basic renewal
        # back onto Premium.
        running = list(subscription.current_run_payments()
                       .filter(covers_until__gt=now)
                       .select_related('subscription__plan')
                       .order_by('-completed_at', '-id'))
        if clearing is not None:
            running = [clearing, *[p for p in running if p.pk != clearing.pk]]
        if not running:
            return None
        priced = {}                      # plan id -> price of record, most recent first
        for p in running:
            priced.setdefault(p.plan_bought()['plan_id'], cls._price_of_record(p))
        current = subscription.plan_id
        if len(priced) == 1:
            chosen = next(iter(priced))
        elif len({currency for _amount, currency in priced.values()}) == 1:
            top = max(amount for amount, _currency in priced.values())
            dearest = [pid for pid, (amount, _c) in priced.items() if amount == top]
            chosen = current if current in dearest else dearest[0]
        else:
            chosen = current if current in priced else next(iter(priced))
        if chosen == current:
            return subscription.plan
        plan = SubscriptionPlan.objects.filter(pk=chosen).first()
        if plan is None:
            logger.warning("Paid period names plan %s, which does not exist; keeping %s",
                           chosen, current)
            return subscription.plan
        if chosen != (clearing.plan_bought()['plan_id'] if clearing else None):
            logger.warning("Subscription %s moves to plan %s, bought by an earlier payment "
                           "still running", subscription.id, chosen)
        return plan

    @classmethod
    def _apply_upgrade(cls, payment: Payment, subscription: Subscription,
                       to_plan: SubscriptionPlan, source: str):
        """Switch the plan for an upgrade payment that cleared; return its window.

        VALUE-EXACT, decided on the row as it is NOW, not as it was quoted. The credit is
        re-read (`upgrade.credit_for_run`) when the money clears: days used since the
        quote are not credited twice, a renewal paid meanwhile is credited rather than
        erased, and a run that ended has nothing left to credit (the review of
        2026-09-23, U4). The new plan then runs for what the money AND that credit buy:
        its length × (paid + credit) ÷ its price, in whole days — its full length when
        the money clears as quoted.

        Each contributor's share of that period is written down
        (`metadata['upgrade']['switch']`): the upgrade's own money, and every payment
        whose days were credited, which is marked `credited_into` so it no longer counts
        as the run's. A refund of any of them takes back exactly its share
        (`_cover_to_take_back`) — never the whole period, never nothing (U1, U2, U7).

        A switch needs cover running on a plan the new one is dearer than. Otherwise
        (cover ended, or a dearer plan is already running) nothing is credited and the
        money buys its own share of the new plan's length, by the ordinary activation.

        The caller holds the subscription and payment locks.
        """
        from ..upgrade import credit_for_run, is_dearer

        meta = dict(payment.metadata or {})
        record = dict(meta.get('upgrade') or {})
        now = timezone.now()
        new_price = Decimal(record.get('new_price') or payment.amount)
        length = int(to_plan.duration_days)
        try:
            switching = subscription.is_active and (
                subscription.plan_id == (record.get('from_plan') or {}).get('id')
                or is_dearer(to_plan, subscription.plan, payment.currency))
        except Exception:                 # a rate that cannot price the comparison
            switching = False

        if switching:
            credit, days_left, found = credit_for_run(
                subscription, buyer_currency=payment.currency, now=now)
            parts = [{'payment_id': str(part.payment_id), 'credit': part.credit}
                     for part in found]
            # Paid as quoted — within its checkout window, on the very payments the
            # credit was struck on — the quote is honoured: the customer paid the figure
            # they were shown and gets the full length they were promised, even if a
            # whole day ticked over while the transfer posted (the review, L14).
            from ..utils import checkout_ttl

            quoted = record.get('parts')
            if (quoted is not None and record.get('credit') is not None
                    and payment.created_at and now <= payment.created_at + checkout_ttl()
                    # One whole day may tick while a transfer posts; three days is not
                    # "as quoted", and the days used since are not credited twice (U4).
                    and int(record.get('days_left') or 0) - int(days_left) <= 1
                    and [q['payment_id'] for q in quoted] == [p['payment_id'] for p in parts]):
                credit = Decimal(record['credit'])
                days_left = record.get('days_left', days_left)
                parts = [{'payment_id': q['payment_id'], 'credit': Decimal(q['credit'])}
                         for q in quoted]
            value = Decimal(payment.amount) + credit
            days = max(1, int((Decimal(length) * value / new_price)
                              .to_integral_value(rounding='ROUND_HALF_UP')))
            period = days * 86400
            credited = [{'payment_id': part['payment_id'], 'credit': str(part['credit']),
                         'share_seconds': int(period * part['credit'] / value)}
                        for part in parts]
            record['switch'] = {
                'at': now.isoformat(),
                'credit_applied': str(credit),
                'days_credited': int(days_left),
                'granted_days': days,
                # The money's share is what the credited shares leave, so the shares
                # add up to the period exactly: refunding every contributor leaves
                # nothing, not a second.
                'money_share_seconds': period - sum(c['share_seconds'] for c in credited),
                'credited': credited,
                'replaced': {'plan_id': subscription.plan_id,
                             'start_date': subscription.start_date.isoformat(),
                             'end_date': subscription.end_date.isoformat()},
            }
            # The payments the credit came from are spent: their value is in this period
            # now, and they leave the run. ONLY those — one the credit never reached keeps
            # its own place, so its refund still takes back what it bought (L13).
            for spent in Payment.objects.filter(pk__in=[c['payment_id'] for c in credited]):
                spent_meta = dict(spent.metadata or {})
                spent_meta['credited_into'] = str(payment.id)
                Payment.objects.filter(pk=spent.pk).update(metadata=spent_meta)
            meta['upgrade'] = record
            meta['plan'] = {**meta.get('plan', {}), 'duration_days': days}
            payment.metadata = meta
            subscription.plan = to_plan
            subscription.start_date = now
            subscription.end_date = now + timedelta(days=days)
            subscription.trial_end_date = None
            subscription.status = 'active'
            subscription.source = source
            # `save()` re-inherits the new plan's five flags: the plan changed.
            subscription.save()
            return now, subscription.end_date

        days = max(1, int((Decimal(length) * Decimal(payment.amount) / new_price)
                          .to_integral_value(rounding='ROUND_HALF_UP')))
        record['switch'] = {'at': now.isoformat(), 'credit_applied': '0.00',
                            'days_credited': 0, 'granted_days': days, 'late': True}
        meta['upgrade'] = record
        meta['plan'] = {**meta.get('plan', {}), 'duration_days': days}
        payment.metadata = meta
        logger.warning("Upgrade payment %s cleared with nothing left to credit; granting %d "
                       "day(s) of plan %s", payment.id, days, to_plan.pk)
        subscription.plan = (cls._dearest_running_plan(subscription, clearing=payment)
                             if subscription.is_active else to_plan)
        return cls._activate_subscription(subscription, source, days)

    @staticmethod
    def _activate_subscription(subscription: Subscription, source: str,
                               days: int | None = None):
        """Grant one plan period for a payment that cleared; return the window it bought.

        `source` is where that money came from (`policy.money.EntitlementSource`); it is
        recorded and never consulted for access. `days` is the period the payment was
        SOLD (`Payment.plan_bought`), so the cover and the receipt agree even when the
        operator edits the plan while the transfer is in flight. Omitted, the
        subscription's plan's duration.

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
        duration = timedelta(days=days or subscription.plan.duration_days)

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
    def _upgrade_share(payment: Payment, subscription: Subscription):
        """The part of the current cover a refunded payment paid for, when it took part in
        an upgrade; None otherwise.

        An upgrade's period was bought by its own money and by the credit for the days
        left on the payments it replaced; `_apply_upgrade` wrote each one's share of the
        period down. A payment's part is its FRACTION of the period it went into, times
        what that period still accounts for — the whole of it while it is the live one,
        or, if a later upgrade credited it in turn, the share of THAT period its
        remaining days bought, and so on down the chain (the review, L10). So refunding
        the upgrade takes back its money's part and leaves the credited days; refunding
        a credited payment takes back the days its credit became, however many upgrades
        later. Nothing, if the subscription has restarted since.
        """
        meta = payment.metadata or {}
        into = meta.get('credited_into')
        switch = (meta.get('upgrade') or {}).get('switch') or {}
        own = meta.get('kind') == 'upgrade' and switch and not switch.get('late')
        if not into and not own:
            return None
        if into:
            upgrade = Payment.objects.filter(pk=into).only('metadata').first()
            outer = ((upgrade.metadata or {}).get('upgrade') or {}).get('switch') if upgrade else None
            part = next((c for c in (outer or {}).get('credited') or []
                         if c.get('payment_id') == str(payment.pk)), None)
            if part is None:
                return timedelta(0)
            share = Decimal(part.get('share_seconds') or 0)
            period = PaymentService._period(outer)
            live = PaymentService._live_period(upgrade, subscription)
        else:
            share = Decimal(switch.get('money_share_seconds') or 0)
            period = PaymentService._period(switch)
            live = PaymentService._live_period(payment, subscription)
        # Multiplied before dividing, then rounded: a third of a period divided first
        # lost a second, and two refunds that should end the cover left two seconds of it.
        return timedelta(seconds=int((share * live / period).to_integral_value(
            rounding='ROUND_HALF_UP')))

    @staticmethod
    def _period(switch) -> Decimal:
        """An upgrade period's length in seconds, from its switch record."""
        return Decimal(max(1, int(switch.get('granted_days') or 0)) * 86400)

    @staticmethod
    def _live_period(upgrade: Payment, subscription: Subscription, depth: int = 0) -> Decimal:
        """How many seconds of the CURRENT cover an upgrade's whole period accounts for.

        All of it while it is the live switch; if a later upgrade credited it, the share
        of that one's period its remaining days bought, scaled the same way; nothing if
        the subscription restarted since."""
        from django.utils.dateparse import parse_datetime

        meta = upgrade.metadata or {}
        switch = (meta.get('upgrade') or {}).get('switch') or {}
        into = meta.get('credited_into')
        if into and depth < 20:
            outer_payment = Payment.objects.filter(pk=into).only('metadata').first()
            outer = (((outer_payment.metadata or {}).get('upgrade') or {}).get('switch')
                     if outer_payment else None)
            part = next((c for c in (outer or {}).get('credited') or []
                         if c.get('payment_id') == str(upgrade.pk)), None)
            if part is None:
                return Decimal(0)
            return (Decimal(part.get('share_seconds') or 0)
                    * PaymentService._live_period(outer_payment, subscription, depth + 1)
                    / PaymentService._period(outer))
        at = parse_datetime(switch.get('at') or '')
        if subscription.start_date and at and at < subscription.start_date:
            return Decimal(0)
        return PaymentService._period(switch)

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
        share = PaymentService._upgrade_share(payment, subscription)
        if share is not None:
            return share
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
