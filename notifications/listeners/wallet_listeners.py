"""Handlers for money arriving in a wallet (`notifications/domain/wallet_events.py`).

Context only, never a rendered sentence: the template is resolved at delivery inside the
client's language (`notifications/channels/fcm.py`). `data.type` is the `event_type`
verbatim, like every other push, and `related_object_id` is the top-up's ledger
reference, so a tap opens the row (`GET /api/wallet/transactions/{reference_id}/`), and
the agent's receipt can say the notice was written (`GET
/api/wallet/agent/receipts/{reference_id}/` `client_notified`). Deduplicated per
emission (`event_id`).

Raised on failure, like the other listeners: a swallowed error would tell the dispatcher
it succeeded and the retry and dead-letter write would be unreachable. A missing user
(an account erased before the worker ran) is the one failure discarded.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.domain.dispatcher import subscribe
from notifications.domain.wallet_events import WalletToppedUpEvent
from notifications.services import NotificationService

logger = logging.getLogger(__name__)
User = get_user_model()


@subscribe(WalletToppedUpEvent)
def handle_wallet_topped_up(event: WalletToppedUpEvent):
    try:
        recipient = User.objects.get(id=event.recipient_id)
        NotificationService.create_and_send(
            recipient=recipient,
            event_type='wallet_topped_up',
            related_object_id=event.reference_id,
            metadata={'context': {'agent': event.agent},
                      'data': {'type': 'wallet_topped_up',
                               'reference_id': event.reference_id,
                               'amount': event.amount, 'currency': event.currency}},
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning("User not found for WalletToppedUpEvent: %s", event.recipient_id)
    except Exception as e:
        logger.error("Error handling WalletToppedUpEvent: %s", e, exc_info=True)
        raise
