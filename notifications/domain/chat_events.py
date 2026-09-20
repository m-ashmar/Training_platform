"""A message from the person you train with (the owner, 2026-09-20).

One notice, and it carries only who wrote and the first words of what they wrote. No
amount of money, no link and no call to buy, so it reaches every build.

The words live in the TEMPLATE, so the recipient reads "New message" in their own
language at delivery; only the name and the preview travel as context.
"""
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .events import BaseDomainEvent, NotificationTemplate


@dataclass
class ChatMessageEvent(BaseDomainEvent):
    """Someone wrote in a conversation the recipient is not currently reading.

    Raised by `chat/events.py`, which also owns the rule for WHEN: only once until the
    recipient opens the thread, so a burst of messages is one push.
    """
    template = NotificationTemplate(
        title=_("New message"),
        body=_("%(sender)s: %(preview)s"),
    )
    recipient_id: int = 0
    actor_id: int = 0
    conversation_id: int = 0
    message_id: int = 0
    sender: str = ""
    preview: str = ""
