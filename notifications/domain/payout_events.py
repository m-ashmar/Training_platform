"""A trainer's withdrawal moved (`payouts/services.py`; the spec, section 7:
"Withdrawal requested, paid, failed" — push and in-app inbox).

The request number is the one thing each notice carries in its text: it is what the
status screen and support both go by. The words live in the template, resolved in the
trainer's language at delivery; the failure carries the operator's reason, because it
is what the trainer needs to know. A withdrawal is money moving to a person, not a
digital good, so these reach every build.

The spec's table also lists EMAIL for all three, and each is emailed: the inbox notice
these events write queues the email of the same kind (`notifications/channels/email.py`
`withdrawal_requested`, `withdrawal_paid`, `withdrawal_failed`; the request number and
the amount, no link), once per withdrawal and step, to a trainer with a verified address
whose preferences allow it.
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class WithdrawalRequestedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Withdrawal requested"),
        body=_("We received your withdrawal request %(number)s. We process requests "
               "every day before noon."),
    )
    recipient_id: int = 0
    withdrawal_id: int = 0
    number: str = ""


@dataclass
class WithdrawalPaidEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Your withdrawal arrived"),
        body=_("Withdrawal %(number)s was sent and has arrived."),
    )
    recipient_id: int = 0
    withdrawal_id: int = 0
    number: str = ""


@dataclass
class WithdrawalFailedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Your withdrawal could not be completed"),
        body=_("Withdrawal %(number)s failed and the money is back in your balance: "
               "%(reason)s"),
    )
    recipient_id: int = 0
    withdrawal_id: int = 0
    number: str = ""
    reason: str = ""
