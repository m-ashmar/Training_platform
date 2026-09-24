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
    next message pushes again. It needs no timer and no counter, and it is explainable
    to a person: "we only nudge you once until you have looked".

    The state is on the conversation row — `..._notified_at` beside `..._last_read_at` —
    and the push is CLAIMED with one conditional UPDATE before it is emitted
    (`_claim_nudge`). It used to be read from the last `chat_message` Notification row,
    but a Celery worker writes that row after this function returns, so every message
    of a burst that outran the queue saw "no push yet" and pushed, and a read between a
    send and the worker's write left the next legitimate push swallowed (SIDE_BUGS
    S-095). The UPDATE is atomic in Postgres: of two sends racing for one nudge,
    exactly one updates the row.

    Delivery itself still obeys the recipient's settings: `NotificationService`
    dispatches through `UserNotificationPreference.is_channel_enabled`, so someone who
    turned `chat_message` off gets the inbox row and no push.

    Never raises: a failed nudge must not cost the message.
    """
    try:
        from notifications.domain.chat_events import ChatMessageEvent
        from notifications.domain.dispatcher import emit_event

        conversation = message.conversation
        recipient_id = conversation.other_id(message.sender_id)
        if recipient_id is None:
            return
        if not _claim_nudge(conversation, recipient_id):
            return
        sender = message.sender
        emit_event(ChatMessageEvent(
            recipient_id=recipient_id,
            actor_id=message.sender_id,
            conversation_id=conversation.pk,
            message_id=message.pk,
            sender=(f"{sender.first_name} {sender.last_name}".strip()
                    or sender.username),
            preview=_preview(message, recipient_id),
        ))
    except Exception:       # noqa: BLE001 — the message stands whatever happens here
        logger.warning('could not notify about chat message %s',
                       getattr(message, 'pk', None), exc_info=True)


def _preview(message, recipient_id) -> str:
    """What the lock screen quotes: the text, or a media message's caption, or — for a
    photo, voice note or video sent without one — what it is, in the RECIPIENT's
    language (the context is interpolated as it is stored, so it is translated here).
    Never a link: the media itself is only ever served inside the thread.
    """
    from django.utils.translation import gettext as _

    from training_platform.i18n import LanguageContext

    from .models import KIND_IMAGE, KIND_VIDEO, KIND_VOICE

    if message.body.strip() or message.kind not in (KIND_IMAGE, KIND_VOICE, KIND_VIDEO):
        return message.body[:PREVIEW_CHARS]
    with LanguageContext.for_user_id(recipient_id):
        return str({KIND_IMAGE: _("Photo"), KIND_VOICE: _("Voice note"),
                    KIND_VIDEO: _("Video")}[message.kind])


def _claim_nudge(conversation, recipient_id) -> bool:
    """Take the right to push `recipient_id` about this thread; True if it was ours.

    One UPDATE on the ROW, never a read of this in-memory conversation: a consumer
    holds one object for the whole life of a socket, and the recipient may have opened
    the thread on their phone since it was loaded. The WHERE is re-checked under the
    row lock, so a concurrent claim that committed first makes this one match nothing.
    """
    from django.db.models import F, Q
    from django.utils import timezone

    from .models import Conversation

    side = 'client' if recipient_id == conversation.client_id else 'trainer'
    notified, read = f'{side}_notified_at', f'{side}_last_read_at'
    return bool(Conversation.objects
                .filter(pk=conversation.pk)
                .filter(Q(**{f'{notified}__isnull': True})
                        | Q(**{f'{notified}__lt': F(read)}))
                .update(**{notified: timezone.now()}))


def forget_preview(message):
    """Blank the preview of `message` wherever the recipient's inbox quotes it.

    The push stores the first words of the message in the Notification row's
    `context`, and `GET /api/social/notifications/` renders from it — so a message its
    sender took back stayed readable in the other person's inbox for ever, although
    `MessageDeleteView` promises no endpoint serves the text again (SIDE_BUGS S-094).
    The row is kept (it still says who wrote, and when); only the quoted text goes.

    Never raises: the tombstone stands whatever happens here.
    """
    try:
        from notifications.services import blank_previews

        recipient_id = message.conversation.other_id(message.sender_id)
        if recipient_id is None:
            return
        blank_previews(recipient_id=recipient_id, event_type='chat_message',
                       metadata__data__message_id=str(message.pk))
    except Exception:       # noqa: BLE001
        logger.warning('could not blank the preview of chat message %s',
                       getattr(message, 'pk', None), exc_info=True)
