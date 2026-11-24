"""Platform-plan notices (the owner, 2026-09-19): a reminder before the plan ends, then
"keep asking" after it ended. Sent by `subscription.send_renewal_reminders`.

One text for every build and no amount, link or call to buy: a push reaches every build,
and a store build may not sell the plan in the app. The words live in the template, so
each recipient reads them in their own language at delivery; only `%(days)s` is context.
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class SubscriptionExpiringEvent(BaseDomainEvent):
    """The plan ends within SUBSCRIPTION_RENEWAL_NOTICE_DAYS."""
    template = NotificationTemplate(
        title=_("Your subscription ends soon"),
        body=_("Your subscription ends in %(days)s days."),
    )
    recipient_id: int = 0
    subscription_id: int = 0


@dataclass
class SubscriptionExpiredEvent(BaseDomainEvent):
    """The plan ended: sent on each day of SUBSCRIPTION_LAPSED_REMINDER_DAYS after it."""
    template = NotificationTemplate(
        title=_("Your subscription has ended"),
        body=_("Your diet plan and routine are saved and you can still see them. Using "
               "them needs an active subscription."),
    )
    # A build that may not sell the plan (play, ios) is told the state only: the last
    # sentence says a subscription is needed, which that build cannot offer (Road to 10
    # R1, the manager's ruling of 2026-11-24). `notifications/build_gate.py`.
    requires = "can_purchase_platform_sub"
    store_template = NotificationTemplate(
        title=_("Your subscription has ended"),
        body=_("Your diet plan and routine are saved and you can still see them."),
    )
    recipient_id: int = 0
    subscription_id: int = 0


@dataclass
class CheckoutExpiredEvent(BaseDomainEvent):
    """A checkout the customer opened and never finished, closed by the poller.

    No amount, no link, no "pay now". It reaches only a build that can open a plan
    checkout (`requires`, below): apk and web.
    """
    template = NotificationTemplate(
        title=_("Your checkout expired"),
        body=_("You did not finish your last checkout, so it was closed. You can start "
               "again whenever you like."),
    )
    # Only a build that can open a plan checkout is told (Road to 10 R1, the manager's
    # ruling of 2026-11-24): on play and ios it invited restarting a purchase those
    # builds lock off. No store text, so those builds get no inbox row, no unread count
    # and no push. Nothing was paid, so nothing is lost. `notifications/build_gate.py`.
    requires = "can_purchase_platform_sub"
    recipient_id: int = 0
    payment_id: str = ""


@dataclass
class PaymentNeedsReviewEvent(BaseDomainEvent):
    """A transfer arrived for the customer's payment that is almost right — the
    reference with another amount, or the amount with no reference — and a person is
    checking it (BACKEND_BUGS B-051; the spec's table: push and in-app, no email).
    Reassurance only: no amount, no instruction to pay again."""
    template = NotificationTemplate(
        title=_("We are checking your payment"),
        body=_("We received a transfer that does not exactly match your payment. Our team "
               "is checking it and will let you know."),
    )
    recipient_id: int = 0
    payment_id: str = ""


@dataclass
class PaymentRejectedEvent(BaseDomainEvent):
    """An operator could not accept the transfer held for the customer's payment; the
    money goes back to its sender by hand (the spec's table: push and in-app)."""
    template = NotificationTemplate(
        title=_("We could not accept your transfer"),
        body=_("The transfer we checked could not be matched to your payment. Contact "
               "support to have it returned."),
    )
    recipient_id: int = 0
    payment_id: str = ""

