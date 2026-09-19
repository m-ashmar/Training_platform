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
    recipient_id: int = 0
    subscription_id: int = 0
