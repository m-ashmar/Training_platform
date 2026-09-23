"""Everything that CHANGES a conversation, in one module.

The REST routes and the WebSocket consumer are two doors into these four functions.
Neither one implements a rule: the pair check, the idempotent replay, the fan-out and
the push all live here, so a message sent over a socket and a message sent over HTTP are
the same event with the same side effects.
"""
import logging
import uuid as _uuid

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework.exceptions import PermissionDenied, ValidationError

from .events import broadcast, forget_preview, notify_new_message
from .exceptions import IdempotencyConflict, MessagingRefused
from .models import Conversation, Message
from .permissions import may_message, pair_of
from .serializers import MessageSerializer

logger = logging.getLogger(__name__)

#: How much history one page may ask for.
PAGE_DEFAULT, PAGE_MAX = 30, 100

#: What `may_message` answers that is a coaching state, and is raised as the same 403
#: the training endpoints raise — one vocabulary for the app, not two.
COACHING_REFUSALS = ('coaching_frozen', 'coaching_unpaid', 'coaching_ended')


def require_may_message(sender, recipient):
    """Raise the 403 `chat.permissions.may_message` finds; nothing when it allows."""
    from users.coaching import raise_refusal

    code = may_message(sender, recipient)
    if code is None:
        return
    if code in COACHING_REFUSALS:
        pair = pair_of(sender, recipient)
        raise_refusal(code, pair[0].pk if pair else None)
    raise MessagingRefused(code)


def open_conversation(trainer, client) -> Conversation:
    """The pair's one conversation, created if this is the first time.

    Race-safe by the unique constraint rather than by get_or_create's read: two phones
    tapping "message" at once produce one row, and the loser reads it back.
    """
    conversation, _created = Conversation.objects.get_or_create(
        trainer=trainer, client=client)
    return conversation


def replay(conversation, sender, client_uuid):
    """The message this id already wrote, or None.

    A retry whose first attempt DID land (the answer was lost) must not post twice. An
    id already used by the OTHER person in this thread is a 409 and says nothing about
    their message — the same shape as the routine set-log replay.
    """
    if client_uuid is None:
        return None
    found = Message.objects.filter(conversation=conversation,
                                   client_uuid=client_uuid).first()
    if found is None:
        return None
    if found.sender_id != sender.pk:
        raise IdempotencyConflict()
    return found


def parse_client_uuid(raw):
    """The app's id as DRF's UUIDField reads it, or None. A malformed one is a 400."""
    if raw in (None, ''):
        return None
    try:
        return (_uuid.UUID(int=raw) if isinstance(raw, int) and not isinstance(raw, bool)
                else _uuid.UUID(str(raw)))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError({'client_uuid': [_('Not a valid UUID.')]},
                              code='invalid_client_uuid')


def send_message(conversation, sender, data):
    """Write one message and tell everyone. Returns `(message, created)`.

    `data` is the raw request body: `{body, client_uuid?}`. Validation, the pair check
    and the replay all happen here, in that order — an idempotent retry of a message
    that landed while the coaching was still paid is ANSWERED, not refused, exactly as a
    replayed set log is.
    """
    recipient_id = conversation.other_id(sender.pk)
    if recipient_id is None:
        # Not a member. Callers 404 before this; belt and braces for the socket.
        raise PermissionDenied(_('This conversation is not yours.'), code='not_found')

    client_uuid = parse_client_uuid(
        data.get('client_uuid') if hasattr(data, 'get') else None)
    replayed = replay(conversation, sender, client_uuid)
    if replayed is not None:
        return replayed, False

    serializer = MessageSerializer(data=data)
    serializer.is_valid(raise_exception=True)

    recipient = (conversation.client if sender.pk == conversation.trainer_id
                 else conversation.trainer)
    require_may_message(sender, recipient)

    try:
        with transaction.atomic():
            message = Message.objects.create(
                conversation=conversation, sender=sender,
                body=serializer.validated_data['body'], client_uuid=client_uuid)
            # The list is ordered by this, so it moves with every message, including
            # one that is later taken back: the thread DID have activity.
            Conversation.objects.filter(pk=conversation.pk).update(
                last_message_at=message.created_at)
            conversation.last_message_at = message.created_at
    except IntegrityError:
        # Two attempts raced and the partial unique index let one through.
        first = replay(conversation, sender, client_uuid)
        if first is None:
            raise
        return first, False

    payload = dict(MessageSerializer(message).data)
    broadcast(conversation.pk, 'message.new', {'message': payload})
    notify_new_message(message)
    return message, True


def mark_read(conversation, reader):
    """Mark everything up to now read, and answer with what is left.

    Stamps the reader's own `..._last_read_at` (which is what the unread count is
    measured from) AND `read_at` on each message they had not read, which is what the
    other side's ticks are drawn from.
    """
    field = conversation.read_field_for(reader.pk)
    if field is None:
        raise PermissionDenied(_('This conversation is not yours.'), code='not_found')
    now = timezone.now()
    with transaction.atomic():
        Conversation.objects.filter(pk=conversation.pk).update(**{field: now})
        setattr(conversation, field, now)
        (Message.objects.filter(conversation=conversation, read_at__isnull=True)
         .exclude(sender=reader).update(read_at=now))
        # `.update()` fires no signal, and the home card carries this unread count.
        from training_platform.signals import bust_home_for_a_read
        bust_home_for_a_read(conversation, reader.pk)
    broadcast(conversation.pk, 'message.read',
              {'conversation': conversation.pk, 'reader': reader.pk,
               'read_at': now.isoformat()})
    # Anything that arrived while the update ran is genuinely still unread.
    remaining = (Message.objects
                 .filter(conversation=conversation, deleted_at__isnull=True,
                         created_at__gt=now)
                 .exclude(sender=reader).count())
    return remaining, now


def tombstone(message, actor):
    """The sender takes their message back. The row stays; the text stops being served.

    Only the sender (403 `not_your_message`), and taking back a message that is already
    gone changes nothing and answers the same tombstone — a retry of a delete is not an
    error. The recipient's inbox stops quoting it too (`forget_preview`); the listener
    that writes the inbox row checks the tombstone as well, for a push still queued.
    """
    if message.sender_id != actor.pk:
        raise PermissionDenied(_('You can only delete your own messages.'),
                               code='not_your_message')
    if message.deleted_at is None:
        message.deleted_at = timezone.now()
        message.save(update_fields=['deleted_at'])
        broadcast(message.conversation_id, 'message.deleted',
                  {'conversation': message.conversation_id, 'message': message.pk,
                   'deleted_at': message.deleted_at.isoformat()})
        forget_preview(message)
    return message


def history(conversation, *, before=None, limit=None):
    """A page of the thread, newest first. Returns `(rows, next_before)`.

    Cursored by id and not by time: two messages can share a timestamp to the
    microsecond, and a cursor on a non-unique column repeats and hides rows. Tombstones
    are RETURNED (with no text) so the app keeps a stable list rather than watching a
    gap appear under its scroll.

    `next_before` is the cursor for the next page, and None once the thread has been
    read back to its first message. One row beyond the page is fetched to decide that,
    so the app never asks for a page that turns out to be empty.
    """
    try:
        size = int(limit) if limit not in (None, '') else PAGE_DEFAULT
    except (TypeError, ValueError):
        raise ValidationError({'limit': [_('Not a number.')]}, code='invalid_limit')
    if size < 1 or size > PAGE_MAX:
        raise ValidationError({'limit': [_('Out of range.')]}, code='invalid_limit')

    rows = Message.objects.filter(conversation=conversation)
    if before not in (None, ''):
        try:
            rows = rows.filter(pk__lt=int(before))
        except (TypeError, ValueError):
            raise ValidationError({'before': [_('Not a message id.')]},
                                  code='invalid_cursor')
    page = list(rows.order_by('-id')[:size + 1])
    return page[:size], (page[size - 1].pk if len(page) > size else None)
