"""Handlers for a trainer's withdrawals (`notifications/domain/payout_events.py`).

Context only, never a rendered sentence: the template is resolved at delivery inside the
trainer's language (`notifications/channels/fcm.py`). `data.type` is the `event_type`
verbatim, like every other push, and `related_object_id` is the withdrawal, so a tap
opens its status screen (`GET /api/v1/trainer/withdrawals/{withdrawal_id}/`).
Deduplicated per emission (`event_id`).

Raised on failure, like the other listeners: a swallowed error would tell the
dispatcher it succeeded and the retry and dead-letter write would be unreachable. A
missing user (an account erased before the worker ran) is the one failure discarded.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.domain.dispatcher import subscribe
from notifications.domain.payout_events import (
    WithdrawalFailedEvent,
    WithdrawalPaidEvent,
    WithdrawalRequestedEvent,
)
from notifications.services import NotificationService

logger = logging.getLogger(__name__)
User = get_user_model()


def _send(event, event_type, context, data):
    try:
        recipient = User.objects.get(id=event.recipient_id)
        NotificationService.create_and_send(
            recipient=recipient,
            event_type=event_type,
            related_object_id=str(event.withdrawal_id),
            metadata={'context': context, 'data': data},
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning("User not found for %s: %s", type(event).__name__, event.recipient_id)
    except Exception as e:
        logger.error("Error handling %s: %s", type(event).__name__, e, exc_info=True)
        raise


@subscribe(WithdrawalRequestedEvent)
def handle_withdrawal_requested(event: WithdrawalRequestedEvent):
    _send(event, event_type='withdrawal_requested',
          context={'number': event.number},
          data={'type': 'withdrawal_requested', 'withdrawal_id': str(event.withdrawal_id),
                'number': event.number})


@subscribe(WithdrawalPaidEvent)
def handle_withdrawal_paid(event: WithdrawalPaidEvent):
    _send(event, event_type='withdrawal_paid',
          context={'number': event.number},
          data={'type': 'withdrawal_paid', 'withdrawal_id': str(event.withdrawal_id),
                'number': event.number})


@subscribe(WithdrawalFailedEvent)
def handle_withdrawal_failed(event: WithdrawalFailedEvent):
    _send(event, event_type='withdrawal_failed',
          context={'number': event.number, 'reason': event.reason},
          data={'type': 'withdrawal_failed', 'withdrawal_id': str(event.withdrawal_id),
                'number': event.number, 'reason': event.reason})
