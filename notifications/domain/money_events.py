"""Money notices the spec's table requires (section 7, "Notifications the brief says are
missing"; plan step 5).

* Payment confirmed — push and inbox, "receipt attached": the customer's plan payment
  cleared, and its receipt is ready in their payment history (`PaymentService.
  complete_payment`, on commit, whoever confirmed it).
* Trainer fee held, released, refunded — push and inbox, BOTH SIDES: the client and the
  trainer each hear what happened to a month of coaching money (`users/coaching.py`
  `notify_fee`). One event type per fact, sent to each side with `role` saying which
  side the recipient is on, like `coaching_ended`.

The text states the fact and carries no price, no button and no link, on every build: a
push reaches `play` and `ios` too, and a store build may not sell a plan or show a
coaching price (the spec, section 7: "Notification text sent to play and ios channels
for PLATFORM_SUB ... states the fact ... and carries no renew button, price or link").
The words live in the templates, so each recipient reads them in their own language at
delivery; only names and the month number are context. `data` carries ids only.
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class PaymentConfirmedEvent(BaseDomainEvent):
    """The customer's payment cleared; the receipt is `payments/{payment_id}/receipt/`."""
    template = NotificationTemplate(
        title=_("Payment confirmed"),
        body=_("We received your payment. Your receipt is ready in your payment history."),
    )
    recipient_id: int = 0
    payment_id: str = ""


@dataclass
class _CoachingFeeEvent(BaseDomainEvent):
    """One month of coaching money, told to one side. `actor_id` is the other person."""
    recipient_id: int = 0
    actor_id: int = 0
    other_name: str = ""
    period_id: int = 0
    month: int = 0
    #: The recipient's side: 'client' or 'trainer'.
    role: str = ""


@dataclass
class CoachingFeeHeldEvent(_CoachingFeeEvent):
    """Month 1 was charged into escrow when the trainer accepted (`take_hold`)."""
    template = NotificationTemplate(
        title=_("Coaching fee held"),
        body=_("The first month's fee for coaching between you and %(name)s is held. It "
               "is paid to the trainer when the first program is sent."),
    )


@dataclass
class CoachingFeeReleasedEvent(_CoachingFeeEvent):
    """A month's fee left escrow for the trainer: month 1 when the first program
    arrived, or a renewal. The trainer hears it when their share lands; the client hears
    it for month 1 (a renewal already tells them: `coaching_renewed`)."""
    template = NotificationTemplate(
        title=_("Coaching fee paid out"),
        body=_("The fee for month %(month)s of coaching between you and %(name)s was paid "
               "to the trainer."),
    )


@dataclass
class CoachingFeeRefundedEvent(_CoachingFeeEvent):
    """A held month went back to the client (`release_hold`): declined, left, or ended
    before the first program."""
    template = NotificationTemplate(
        title=_("Coaching fee returned"),
        body=_("The held fee for coaching between you and %(name)s was returned to the "
               "client's wallet."),
    )
