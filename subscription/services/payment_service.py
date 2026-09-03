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
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.db import transaction
from django.dispatch import Signal
from django.utils import timezone

from ..models import Payment, Subscription

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
        the existing payment without side effects.
        """
        with transaction.atomic():
            payment = (
                Payment.objects
                .select_for_update()
                .select_related('subscription', 'subscription__plan')
                .get(id=payment_id)
            )

            # --- Idempotency: already completed → no-op replay ---
            if payment.status == 'completed':
                logger.info(f"complete_payment idempotent no-op for {payment.id} (already completed)")
                return payment

            event_id = verified_data.get('event_id')
            if event_id:
                dup = Payment.objects.filter(gateway_event_id=event_id).exclude(id=payment.id).first()
                if dup:
                    logger.warning(f"Duplicate gateway_event_id {event_id}: already on payment {dup.id}")
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
            payment.save()

            # --- Activate / extend the subscription ---
            cls._activate_subscription(payment)

            logger.info(
                f"Payment {payment.id} completed (invoice {payment.invoice_number}), "
                f"subscription {payment.subscription_id} activated"
            )

        # Signal outside the write path of the row lock but after commit-safe state.
        payment_completed.send(sender=cls, payment=payment, subscription=payment.subscription)
        return payment

    @classmethod
    def refund_payment(cls, payment_id, reason: str = "") -> Payment:
        """Move a completed payment to refunded and take back what it paid for.

        Nothing could reach this state. `PAYMENT_STATUS_TRANSITIONS` has declared
        `'completed' -> 'refunded'` all along, the webhook had no branch for a refund
        or a chargeback, and the admin action wrote the column with `queryset.update()`
        — so the money went back and the subscription stayed active either way.

        Access is withdrawn in proportion to what was refunded: a renewal loses the
        period it bought, an initial payment ends the subscription outright. If that
        leaves the end date in the past the subscription is expired rather than left
        nominally active with a stale date.
        """
        with transaction.atomic():
            payment = (
                Payment.objects
                .select_for_update()
                .select_related('subscription', 'subscription__plan')
                .get(id=payment_id)
            )
            if payment.status == 'refunded':
                return payment
            if not payment.can_transition_to('refunded'):
                raise InvalidPaymentTransition(
                    f"Payment {payment.id} cannot go {payment.status} -> refunded"
                )

            payment.status = 'refunded'
            if reason:
                payment.gateway_error = reason[:2000]
            payment.save()

            subscription = (
                Subscription.objects.select_for_update()
                .select_related('plan')
                .get(pk=payment.subscription_id)
            )
            now = timezone.now()
            is_renewal = str(payment.metadata.get('kind', '')) == 'renewal'
            if is_renewal and subscription.end_date:
                subscription.end_date = subscription.end_date - timedelta(
                    days=subscription.plan.duration_days
                )
            else:
                subscription.end_date = now

            if subscription.end_date <= now:
                subscription.status = 'cancelled'
                subscription.cancelled_at = now
                subscription.auto_renew = False
            subscription.save()

            logger.warning(
                "Payment %s refunded (%s); subscription %s now %s until %s",
                payment.id, reason or 'no reason given',
                subscription.id, subscription.status, subscription.end_date,
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
    def start_renewal(cls, subscription: Subscription) -> Payment:
        """
        Begin a renewal by creating a PENDING payment only. This never mutates
        the subscription — activation happens later via complete_payment once the
        gateway confirms funds.
        """
        if subscription.status not in ('active', 'expired'):
            raise PaymentError("Subscription is not in a renewable state")

        # One renewal in flight at a time. Nothing stopped a retried task or a
        # double-tapped button from opening several, and every one of them could be
        # completed and charged: a probe opened six and the subscription took 180 days.
        existing = Payment.objects.filter(
            subscription=subscription,
            status__in=('pending', 'processing', 'authorized'),
            metadata__kind='renewal',
        ).first()
        if existing:
            logger.info(
                "Renewal already pending for subscription %s (payment %s)",
                subscription.id, existing.id,
            )
            return existing

        return Payment.objects.create(
            subscription=subscription,
            amount=subscription.plan.price,
            # The plan's own currency. Hardcoding 'USD' here made every renewal
            # unclearable: plans are priced in SYP, ShamCash reports SYP, and
            # _verify_amount_and_currency rejected the mismatch on arrival.
            currency=subscription.plan.currency,
            status='pending',
            payment_method='shamcash',
            description=f"Renewal for {subscription.plan.name}",
            metadata={'kind': 'renewal'},
        )

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
    def _activate_subscription(payment: Payment) -> None:
        # Take the subscription's own row lock. The caller holds the *payment* row, and
        # two payments for one subscription are different rows, so without this both
        # would read the same end_date and both write base + duration — the later write
        # overwriting the earlier, and a customer who paid twice getting one period.
        subscription = (
            Subscription.objects.select_for_update()
            .select_related('plan')
            .get(pk=payment.subscription_id)
        )
        now = timezone.now()
        duration = timedelta(days=subscription.plan.duration_days)
        is_renewal = str(payment.metadata.get('kind', '')) == 'renewal'

        if is_renewal:
            base = subscription.end_date if (subscription.end_date and subscription.end_date > now) else now
            subscription.end_date = base + duration
        else:
            # Initial activation: the subscription was created with an end_date
            # already; only reset it if missing or already in the past.
            if not subscription.end_date or subscription.end_date <= now:
                subscription.end_date = now + duration

        subscription.status = 'active'
        subscription.auto_renew = True
        subscription.save()

    @staticmethod
    def _generate_invoice_number() -> str:
        return f"INV-{timezone.now().year}-{uuid.uuid4().hex[:12].upper()}"
