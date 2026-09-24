"""A trainer's verification was decided (`trainer_verification/services.py`).

Two notices: accepted, and not accepted (a rejection, or a verification an operator
withdrew). The rejection carries the operator's reason, because it is the one thing the
trainer needs in order to fix the application; the words around it live in the
template, so the trainer reads them in their own language at delivery. Nothing here
sells anything or names an amount, so both reach every build.
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class TrainerVerificationApprovedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Your account is verified"),
        body=_("We accepted your documents. The verified trainer mark now shows next to "
               "your name."),
    )
    recipient_id: int = 0
    application_id: int = 0


@dataclass
class TrainerVerificationRejectedEvent(BaseDomainEvent):
    template = NotificationTemplate(
        title=_("Your verification needs changes"),
        body=_("We could not verify your account: %(reason)s"),
    )
    recipient_id: int = 0
    application_id: int = 0
    reason: str = ""
