"""Telling people a message happened: the live socket, and the push for a phone that is
not listening.

Both go out from here and from nowhere else, so the REST route and the socket produce
exactly the same two side effects. Called straight after the `transaction.atomic()`
block that wrote the row — by then the write is committed, because nothing in this
project wraps a view or a consumer in a wider transaction (`ATOMIC_REQUESTS` is not
set), and a caller that is mid-transaction has already decided the message stands.
"""
import logging

logger = logging.getLogger(__name__)

#: Everyone looking at one thread. The consumer joins it on connect.
GROUP = 'chat_{}'

#: What the push may show of the message on a lock screen.
PREVIEW_CHARS = 120


def group_name(conversation_id) -> str:
    return GROUP.format(conversation_id)


def broadcast(conversation_id, event_type, payload):
    """Send one event to everyone with this thread open.

    `event_type` is the name the app reads (`message.new`, `message.read`,
    `message.deleted`) and is also what Channels turns into the consumer's handler
    (`message_new`), so the two can never drift apart.

    Never raises: a socket layer that is down must not cost the message. The row is
    written and the history endpoint still serves it.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            group_name(conversation_id), {'type': event_type, **payload})
    except Exception:       # noqa: BLE001 — a missing Redis must not fail a send
        logger.warning('could not broadcast %s on conversation %s', event_type,
                       conversation_id, exc_info=True)


def notify_new_message(message):
    """Push a new message to the person who is not reading it.

    THE DEDUP RULE, and it is deliberately one sentence long: tell someone about this
    conversation only when they have OPENED it since the last time we told them. Ten
    messages in a minute are therefore one push; the recipient reads the thread, the
    next message pushes again. It needs no timer and no counter — the state is already
    on the row (`..._last_read_at`) and in the inbox (the last `chat_message`
    notification for this conversation) — and it is explainable to a person: "we only
    nudge you once until you have looked".

    Delivery itself still obeys the recipient's settings: `NotificationService`
    dispatches through `UserNotificationPreference.is_channel_enabled`, so someone who
    turned `chat_message` off gets the inbox row and no push.

    Never raises: a failed nudge must not cost the message.
    """
    try:
        from notifications.domain.chat_events import ChatMessageEvent
        from notifications.domain.dispatcher import emit_event

        from .models import Conversation

        conversation = message.conversation
        recipient_id = conversation.other_id(message.sender_id)
        if recipient_id is None:
            return
        # Read the stamp from the ROW, never from this in-memory conversation. A
        # consumer holds one object for the whole life of a socket, and the recipient
        # may have opened the thread on their phone since it was loaded; a stale stamp
        # would swallow the second, legitimate nudge for as long as the socket lives.
        stamps = (Conversation.objects.filter(pk=conversation.pk)
                  .values('client_id', 'client_last_read_at', 'trainer_last_read_at')
                  .first()) or {}
        read_at = (stamps.get('client_last_read_at')
                   if recipient_id == stamps.get('client_id')
                   else stamps.get('trainer_last_read_at'))
        if not _should_notify(conversation.pk, recipient_id, read_at):
            return
        sender = message.sender
        emit_event(ChatMessageEvent(
            recipient_id=recipient_id,
            actor_id=message.sender_id,
            conversation_id=conversation.pk,
            message_id=message.pk,
            sender=(f"{sender.first_name} {sender.last_name}".strip()
                    or sender.username),
            preview=message.body[:PREVIEW_CHARS],
        ))
    except Exception:       # noqa: BLE001 — the message stands whatever happens here
        logger.warning('could not notify about chat message %s',
                       getattr(message, 'pk', None), exc_info=True)


def _should_notify(conversation_id, recipient_id, read_at) -> bool:
    """Has the recipient opened this thread since we last told them about it?"""
    from notifications.models import Notification

    last = (Notification.objects
            .filter(recipient_id=recipient_id, event_type='chat_message',
                    related_object_id=str(conversation_id))
            .order_by('-created_at', '-id')
            .values_list('created_at', flat=True)
            .first())
    return last is None or (read_at is not None and read_at > last)
