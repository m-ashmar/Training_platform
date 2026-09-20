"""Handler for the chat event (chat/events.py).

Context only, never a rendered sentence: the template is resolved at delivery inside the
recipient's language (notifications/channels/fcm.py). The `type` in the FCM `data`
payload is the `event_type` verbatim, as every other listener here does, so the app
routes a tap with the one vocabulary the preferences endpoint publishes.
"""
import logging

from django.contrib.auth import get_user_model

from notifications.domain.chat_events import ChatMessageEvent
from notifications.domain.dispatcher import subscribe
from notifications.services import NotificationService

logger = logging.getLogger(__name__)
User = get_user_model()


@subscribe(ChatMessageEvent)
def handle_chat_message(event: ChatMessageEvent):
    """"Someone wrote to you" — keyed on the CONVERSATION, not on the message.

    `related_object_id` is the conversation so a tap opens the thread and so
    `chat.events._should_notify` can find the last time we nudged this person about it.
    Deduplication is still per emission (`event_id`), so a second, legitimate nudge
    after the recipient has read the thread is not swallowed as a duplicate.

    Whether this push is DELIVERED is the recipient's own setting:
    `NotificationService` dispatches through
    `UserNotificationPreference.is_channel_enabled`, so muting `chat_message` leaves the
    inbox row and sends nothing to the phone.

    Raised on failure like the other listeners: a swallowed error would tell the
    dispatcher it succeeded and the retry and dead-letter write would be unreachable.
    A missing user is the one permanent failure worth discarding.
    """
    try:
        recipient = User.objects.get(id=event.recipient_id)
        NotificationService.create_and_send(
            recipient=recipient,
            actor=User.objects.filter(id=event.actor_id).first(),
            event_type='chat_message',
            related_object_id=str(event.conversation_id),
            metadata={
                'context': {'sender': event.sender, 'preview': event.preview},
                'data': {'type': 'chat_message',
                         'conversation_id': str(event.conversation_id),
                         'message_id': str(event.message_id)},
            },
            event_id=event.event_id,
        )
    except User.DoesNotExist:
        logger.warning("User not found for ChatMessageEvent: %s", event.recipient_id)
    except Exception as e:
        logger.error("Error handling ChatMessageEvent: %s", e, exc_info=True)
        raise
