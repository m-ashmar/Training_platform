"""Handlers for the money notices (`notifications/domain/money_events.py`).

Context only, never a rendered sentence: the template is resolved at delivery inside the
recipient's language (`notifications/channels/fcm.py`). `data.type` is the `event_type`
verbatim; `data` carries ids only — no amount, no link, on every build (the spec,
section 7). Deduplicated on the THING, not the emission: one `payment_confirmed` per
payment and one coaching-fee notice of each kind per month and person, ever (no
`event_id`, so the key is `related_object_id` — `NotificationService._generate_dedup_key`).
A month's release can be announced from two places (a renewal's listener below, and
`settle_owed_payouts` for a share paid late); the key makes that one notice.

Raised on failure, like the other listeners: a swallowed error would tell the dispatcher
it succeeded and the retry and dead-letter write would be unreachable. A missing user
(an account erased before the worker ran) is the one failure discarded.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.domain.dispatcher import subscribe
from notifications.domain.money_events import (
    CoachingFeeHeldEvent,
    CoachingFeeRefundedEvent,
    CoachingFeeReleasedEvent,
    PaymentConfirmedEvent,
)
from notifications.domain.trainer_client_events import CoachingRenewedEvent
from notifications.services import NotificationService

logger = logging.getLogger(__name__)
User = get_user_model()


@subscribe(PaymentConfirmedEvent)
def handle_payment_confirmed(event: PaymentConfirmedEvent):
    """To the customer: the payment cleared, and `data.payment_id` opens its receipt
    (`GET /api/subscription/v1/payments/{payment_id}/receipt/`)."""
    from subscription.models import Payment

    try:
        recipient = User.objects.get(id=event.recipient_id)
        invoice = (Payment.objects.filter(pk=event.payment_id)
                   .values_list('invoice_number', flat=True).first()) or ''
        NotificationService.create_and_send(
            recipient=recipient, event_type='payment_confirmed',
            related_object_id=str(event.payment_id),
            metadata={'context': {},
                      'data': {'type': 'payment_confirmed',
                               'payment_id': str(event.payment_id),
                               'invoice_number': invoice}})
    except User.DoesNotExist:
        logger.warning("User not found for PaymentConfirmedEvent: %s", event.recipient_id)
    except Exception as e:
        logger.error("Error handling PaymentConfirmedEvent: %s", e, exc_info=True)
        raise


def _send_fee(event, event_type):
    """One body for the three coaching-fee notices, to whichever side `event` names.

    Not written to an account that is no longer active when it is delivered: an erasure
    returns the erased person's held month (`release_holds_before_erasure`) and then
    retires the account in the same transaction, so on commit the notice would land in
    an inbox the erasure had just emptied. The other side is told as usual."""
    try:
        recipient = User.objects.get(id=event.recipient_id)
        if not recipient.is_active:
            logger.info("%s not written to inactive account %s", event_type, recipient.pk)
            return
        actor = User.objects.filter(id=event.actor_id).first()
        NotificationService.create_and_send(
            recipient=recipient, actor=actor, event_type=event_type,
            related_object_id=str(event.period_id),
            metadata={'context': {'name': event.other_name, 'month': event.month},
                      'data': {'type': event_type, 'period_id': str(event.period_id),
                               'month': str(event.month), 'role': event.role,
                               'user_id': str(event.actor_id)}})
    except User.DoesNotExist:
        logger.warning("User not found for %s: %s", event_type, event.recipient_id)
    except Exception as e:
        logger.error("Error handling %s: %s", event_type, e, exc_info=True)
        raise


@subscribe(CoachingFeeHeldEvent)
def handle_coaching_fee_held(event: CoachingFeeHeldEvent):
    _send_fee(event, event_type='coaching_fee_held')


@subscribe(CoachingFeeReleasedEvent)
def handle_coaching_fee_released(event: CoachingFeeReleasedEvent):
    _send_fee(event, event_type='coaching_fee_released')


@subscribe(CoachingFeeRefundedEvent)
def handle_coaching_fee_refunded(event: CoachingFeeRefundedEvent):
    _send_fee(event, event_type='coaching_fee_refunded')


@subscribe(CoachingRenewedEvent)
def handle_renewal_paid_the_trainer(event: CoachingRenewedEvent):
    """A renewal is ONE event (`users.coaching.renew`): the month was charged from the
    client and paid out in the same step. The client's notice is `coaching_renewed`
    (`trainer_client_listeners`); this is the trainer's — their fee for the new month —
    once their share has landed. A share left owed for a stale rate is announced when it
    is paid (`settle_owed_payouts`), and the key on the month keeps the two from ever
    writing it twice."""
    from users.models import CoachingPeriod

    period = (CoachingPeriod.objects.select_related('client')
              .filter(pk=event.period_id).first())
    if period is None or not period.payout_reference:
        return
    _send_fee(CoachingFeeReleasedEvent(
        recipient_id=period.trainer_id, actor_id=period.client_id,
        other_name=period.client.full_name, period_id=period.pk, month=period.number,
        role='trainer'), event_type='coaching_fee_released')
