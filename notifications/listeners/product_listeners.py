"""Handlers for the routine-product notices (`notifications/domain/product_events.py`).

Context only, never a rendered sentence: the template is resolved at delivery inside the
recipient's language (`notifications/channels/fcm.py`). The product title IS content the
trainer wrote in two languages, so it is picked here in the recipient's saved language
(`preferred_language`; the other one when that is empty). `data.type` is the
`event_type` verbatim; `data` carries ids only — no amount, no link (spec section 7:
ROUTINE_PRODUCT notices state the fact on every build). Deduplicated per emission
(`event_id`).

Raised on failure, like the other listeners: a swallowed error would tell the
dispatcher it succeeded. A missing user or product (erased before the worker ran) is
the one failure discarded.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.domain.dispatcher import subscribe
from notifications.domain.product_events import (
    RoutineProductApprovedEvent,
    RoutineProductPurchasedEvent,
    RoutineProductRefundedEvent,
    RoutineProductRejectedEvent,
    RoutineProductSaleRefundedEvent,
    RoutineProductSoldEvent,
)
from notifications.services import NotificationService

logger = logging.getLogger(__name__)
User = get_user_model()


def _recipient_and_title(event):
    """`(recipient, title in their language)`, or `(None, None)` when either is gone."""
    from routine.models import RoutineProduct

    recipient = User.objects.filter(id=event.recipient_id).first()
    product = RoutineProduct.objects.filter(pk=event.product_id).first()
    if recipient is None or product is None:
        logger.warning("Routine product notice dropped: recipient %s or product %s is gone",
                       event.recipient_id, event.product_id)
        return None, None
    return recipient, product.title_in(getattr(recipient, "preferred_language", "") or "ar")


@subscribe(RoutineProductApprovedEvent)
def handle_routine_product_approved(event: RoutineProductApprovedEvent):
    try:
        recipient, title = _recipient_and_title(event)
        if recipient is None:
            return
        NotificationService.create_and_send(
            recipient=recipient, event_type='routine_product_approved',
            related_object_id=str(event.product_id),
            metadata={'context': {'title': title},
                      'data': {'type': 'routine_product_approved',
                               'product_id': str(event.product_id)}},
            event_id=event.event_id)
    except Exception as e:
        logger.error("Error handling RoutineProductApprovedEvent: %s", e, exc_info=True)
        raise


@subscribe(RoutineProductRejectedEvent)
def handle_routine_product_rejected(event: RoutineProductRejectedEvent):
    try:
        recipient, title = _recipient_and_title(event)
        if recipient is None:
            return
        NotificationService.create_and_send(
            recipient=recipient, event_type='routine_product_rejected',
            related_object_id=str(event.product_id),
            metadata={'context': {'title': title, 'reason': event.reason},
                      'data': {'type': 'routine_product_rejected',
                               'product_id': str(event.product_id),
                               'reason': event.reason}},
            event_id=event.event_id)
    except Exception as e:
        logger.error("Error handling RoutineProductRejectedEvent: %s", e, exc_info=True)
        raise


@subscribe(RoutineProductSoldEvent)
def handle_routine_product_sold(event: RoutineProductSoldEvent):
    try:
        recipient, title = _recipient_and_title(event)
        if recipient is None:
            return
        NotificationService.create_and_send(
            recipient=recipient, event_type='routine_product_sold',
            related_object_id=str(event.purchase_id),
            metadata={'context': {'title': title},
                      'data': {'type': 'routine_product_sold',
                               'product_id': str(event.product_id),
                               'purchase_id': str(event.purchase_id)}},
            event_id=event.event_id)
    except Exception as e:
        logger.error("Error handling RoutineProductSoldEvent: %s", e, exc_info=True)
        raise


@subscribe(RoutineProductPurchasedEvent)
def handle_routine_product_purchased(event: RoutineProductPurchasedEvent):
    from routine.models import Routine

    try:
        recipient, title = _recipient_and_title(event)
        if recipient is None:
            return
        # The buyer's copy; "" when none exists any more (FCM data is strings only, so
        # never null — API_CONTRACT.md §4 says so; BACKEND_BUGS B-170).
        routine_id = (Routine.objects.filter(purchase_id=event.purchase_id)
                      .values_list('pk', flat=True).first())
        NotificationService.create_and_send(
            recipient=recipient, event_type='routine_product_purchased',
            related_object_id=str(event.purchase_id),
            metadata={'context': {'title': title},
                      'data': {'type': 'routine_product_purchased',
                               'product_id': str(event.product_id),
                               'purchase_id': str(event.purchase_id),
                               'routine_id': str(routine_id or '')}},
            event_id=event.event_id)
    except Exception as e:
        logger.error("Error handling RoutineProductPurchasedEvent: %s", e, exc_info=True)
        raise


@subscribe(RoutineProductRefundedEvent)
def handle_routine_product_refunded(event: RoutineProductRefundedEvent):
    try:
        recipient, title = _recipient_and_title(event)
        if recipient is None:
            return
        NotificationService.create_and_send(
            recipient=recipient, event_type='routine_product_refunded',
            related_object_id=str(event.purchase_id),
            metadata={'context': {'title': title},
                      'data': {'type': 'routine_product_refunded',
                               'product_id': str(event.product_id),
                               'purchase_id': str(event.purchase_id)}},
            event_id=event.event_id)
    except Exception as e:
        logger.error("Error handling RoutineProductRefundedEvent: %s", e, exc_info=True)
        raise


@subscribe(RoutineProductSaleRefundedEvent)
def handle_routine_product_sale_refunded(event: RoutineProductSaleRefundedEvent):
    try:
        recipient, title = _recipient_and_title(event)
        if recipient is None:
            return
        NotificationService.create_and_send(
            recipient=recipient, event_type='routine_product_sale_refunded',
            related_object_id=str(event.purchase_id),
            metadata={'context': {'title': title},
                      'data': {'type': 'routine_product_sale_refunded',
                               'product_id': str(event.product_id),
                               'purchase_id': str(event.purchase_id)}},
            event_id=event.event_id)
    except Exception as e:
        logger.error("Error handling RoutineProductSaleRefundedEvent: %s", e, exc_info=True)
        raise
