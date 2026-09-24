"""Routine products (`routine/products.py`, plan step 10).

Six notices: the trainer's product was approved or rejected by an operator, the trainer
sold a copy, the buyer's purchase is confirmed, and a purchase was refunded (to the
buyer, and to the trainer whose sale it was).

ROUTINE_PRODUCT is store-billed money, so the spec's rule for its notices applies to
all of them (section 7): the text states the fact and carries no price, no button and
no link — a push reaches every build, `play` and `ios` included. The product's title is
context, chosen in the recipient's language by the listener; the words around it live
in the template, so each recipient reads them in their own language at delivery.
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class RoutineProductApprovedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Your program is published"),
        body=_("%(title)s passed review and is now listed."),
    )
    recipient_id: int = 0
    product_id: int = 0


@dataclass
class RoutineProductRejectedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Your program needs changes"),
        body=_("%(title)s was not published: %(reason)s"),
    )
    recipient_id: int = 0
    product_id: int = 0
    reason: str = ""


@dataclass
class RoutineProductSoldEvent(BaseDomainEvent):
    """To the trainer. Never names the buyer: a purchase is not a coaching."""
    template = NotificationTemplate(
        title=_("You sold a program"),
        body=_("Someone bought %(title)s."),
    )
    recipient_id: int = 0
    product_id: int = 0
    purchase_id: int = 0


@dataclass
class RoutineProductPurchasedEvent(BaseDomainEvent):
    """To the buyer: their copy is in their routines."""
    template = NotificationTemplate(
        title=_("Your program is ready"),
        body=_("%(title)s is in your routines now."),
    )
    recipient_id: int = 0
    product_id: int = 0
    purchase_id: int = 0


@dataclass
class RoutineProductRefundedEvent(BaseDomainEvent):
    """To the buyer: the money is back and the copy is gone."""
    template = NotificationTemplate(
        title=_("Your program purchase was refunded"),
        body=_("%(title)s was refunded to your wallet and removed from your routines."),
    )
    recipient_id: int = 0
    product_id: int = 0
    purchase_id: int = 0


@dataclass
class RoutineProductSaleRefundedEvent(BaseDomainEvent):
    """To the trainer: their share of that sale went back."""
    template = NotificationTemplate(
        title=_("A program sale was refunded"),
        body=_("A purchase of %(title)s was refunded, and your share of it returned."),
    )
    recipient_id: int = 0
    product_id: int = 0
    purchase_id: int = 0
