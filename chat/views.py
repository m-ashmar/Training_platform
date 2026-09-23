"""`/api/v1/chat/` — text messages between a trainer and their client.

Every route here is authenticated (`401 not_authenticated` without a token) and nothing
on it differs by build: a message is not a product, so `X-App-Channel` and
`X-App-Version` change nothing. No route moves money, so none of them consults
`policy.services`.

A conversation the caller is not in is a **404**, never a 403: a 403 would confirm that
conversation 812 exists and belongs to someone, which is how ids get walked.

THE RULE FOR A COACHING THAT IS NOT PAID (the owner, 2026-09-19): frozen, unpaid and
ended keep the thread READABLE and refuse SENDING with that code, because an unpaid
relationship is never deleted and cannot be used. Reading is membership; writing asks
`chat.permissions.may_message`.
"""
import logging

from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import Conversation, Message
from .serializers import MessageSerializer, conversation_cards, conversation_queryset

logger = logging.getLogger(__name__)


def _mine_or_404(user, conversation_id):
    """The caller's conversation, or 404. Not a 403 — see the module docstring."""
    conversation = (Conversation.objects.select_related('trainer', 'client')
                    .filter(pk=conversation_id).first())
    if conversation is None or conversation.other_id(user.pk) is None:
        raise NotFound(_('Conversation not found.'), code='not_found')
    return conversation


class ConversationListView(APIView):
    """GET /api/v1/chat/conversations/ — the caller's threads, newest activity first.

    Auth: any signed-in trainer or client (an admin or agent has no conversations and
    gets an empty page). Query: `page`, `page_size` (max 100).

    200 `{count, next, previous, results: [...]}` where a result is::

        {"id": 7,
         "other": {"id", "first_name", "last_name", "profile_picture"},   absolute URL
         "last_message": {"id", "sender", "body" (200 chars), "deleted", "created_at"}
                         | null,
         "last_message_at": datetime,        what the list is ordered by
         "unread_count": 3,                  messages from the other person since the
                                             caller last opened the thread
         "can_send": false,                  may the caller type here right now
         "refusal": "coaching_frozen",       why not, when `can_send` is false:
                                             no_relation | blocked | coaching_frozen |
                                             coaching_unpaid | coaching_ended
         "created_at": datetime}

    `refusal` is null when `can_send` is true. Branch on it, never on the sentence the
    send endpoint would answer with.

    The page costs a fixed number of queries whatever its size: the last message of
    every row is read in one query and the refusals in one bulk pass.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from routine.views import StandardResultsSetPagination

        user = request.user
        paginator = StandardResultsSetPagination()
        # Nobody but a trainer or a client can be on either side of a conversation
        # (chat/models.py), so the honest answer for anyone else is an empty page.
        rows = (conversation_queryset(user) if (user.is_trainer or user.is_client)
                else Conversation.objects.none())
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(
            conversation_cards(page, user=user, request=request))


class ConversationStartView(APIView):
    """POST /api/v1/chat/conversations/start/ — the pair's thread, opened if it is new.

    Body: `{"client_id": 12}` from a trainer, `{"trainer_id": 4}` from a client. Exactly
    one of the two; the caller supplies the other side.

    200 `{"conversation": {...the list row...}}` — the same shape the list returns, so
    the app can push the thread screen with it.

    400 `invalid_body` when neither id is given, or both.
    404 `not_found` when the other person is not an APPROVED trainer or client of the
    caller and never had a paid coaching month with them, or when either of the two
    has blocked the other — the same answer as for a user id that does not exist, so
    this cannot be used to find out who is on the platform or that a block exists.

    Opening a thread is allowed while the coaching is frozen, unpaid or ended: the
    conversation is readable for ever, and the row says `can_send: false` with the
    reason. Only SENDING is refused. An ENDED coaching deletes the relation, so "ended"
    is read from the pair's paid months (`CoachingPeriod`): such a pair opens its thread
    and reads `refusal: "coaching_ended"`.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from rest_framework.exceptions import ValidationError

        from .permissions import may_open_thread

        data = request.data if hasattr(request.data, 'get') else {}
        client_id, trainer_id = data.get('client_id'), data.get('trainer_id')
        if (client_id is None) == (trainer_id is None):
            raise ValidationError(
                _('Name exactly one of client_id or trainer_id.'), code='invalid_body')

        from users.models import CustomUser
        try:
            other = CustomUser.objects.filter(
                pk=int(client_id if client_id is not None else trainer_id)).first()
        except (TypeError, ValueError):
            other = None
        pair = may_open_thread(request.user, other)
        # One answer for "no such person", "not your client", "wrong kind of account"
        # and "blocked": an id the caller may not chat with tells them nothing else.
        if pair is None:
            raise NotFound(_('Conversation not found.'), code='not_found')

        conversation = services.open_conversation(*pair)
        row = conversation_queryset(request.user).filter(pk=conversation.pk)
        cards = conversation_cards(row, user=request.user, request=request)
        return Response({'conversation': cards[0]})


class ConversationMessagesView(APIView):
    """GET, POST /api/v1/chat/conversations/{id}/messages/ — read and write one thread.

    404 `not_found` for a conversation that is not the caller's (never 403).

    **GET** — a page of history, newest first, cursored by id.
    Query: `before` (a message id; returns what is older than it), `limit` (default 30,
    max 100). 400 `invalid_cursor` / `invalid_limit`.
    200 `{"results": [message, ...], "next_before": 91 | null}`, where `next_before` is
    the cursor for the next page and is null when the thread has been read to its
    start. A message is::

        {"id", "conversation", "sender", "body", "client_uuid",
         "created_at", "read_at", "deleted_at", "deleted"}

    A message the sender took back is still in the list with `deleted: true` and an
    empty `body`, so the thread does not renumber under a scroll.

    **POST** — send. Body `{"body": "...", "client_uuid": "..."}` (`client_uuid`
    optional). `body` is capped at 4000 characters and is stored exactly as typed.
    201 with the message. A repeat of a `client_uuid` already used answers **200** with
    the FIRST message and writes nothing, so a retry after a lost answer is safe.
    400 `validation_error` (an empty or whitespace-only body: `blank_message`),
    `invalid_client_uuid`.
    409 `idempotency_conflict` — that id is already the other person's message here.
    403 `no_relation` — the two are not an approved trainer and client, and never had a
    paid coaching month.
    403 `blocked` — either of the two has blocked the other. The thread stays readable.
    403 `coaching_frozen` / `coaching_unpaid` / `coaching_ended` (with `trainer_id`) —
    the coaching cannot be used; the thread stays readable. Same codes, same shape, as
    every training write. `coaching_ended` is also the answer once the relation is gone
    for a pair that paid for coaching.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        conversation = _mine_or_404(request.user, conversation_id)
        rows, next_before = services.history(
            conversation, before=request.query_params.get('before'),
            limit=request.query_params.get('limit'))
        return Response({'results': MessageSerializer(rows, many=True).data,
                         'next_before': next_before})

    def post(self, request, conversation_id):
        conversation = _mine_or_404(request.user, conversation_id)
        message, created = services.send_message(conversation, request.user,
                                                 request.data)
        return Response(MessageSerializer(message).data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ConversationReadView(APIView):
    """POST /api/v1/chat/conversations/{id}/read/ — the caller opened the thread.

    No body. Marks everything written up to now as read: the caller's own read stamp
    (what their unread count is measured from) and `read_at` on each message they had
    not read (what the other side's ticks are drawn from). The other side is told over
    the socket with `message.read`.

    200 `{"unread_count": 0, "read_at": datetime}` — `unread_count` is what is STILL
    unread, which is normally 0 and is only non-zero if a message arrived during the
    call. Calling it twice is not an error.
    404 `not_found` for a conversation that is not the caller's.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, conversation_id):
        conversation = _mine_or_404(request.user, conversation_id)
        remaining, read_at = services.mark_read(conversation, request.user)
        return Response({'unread_count': remaining, 'read_at': read_at})


class MessageDeleteView(APIView):
    """DELETE /api/v1/chat/messages/{id}/ — the sender takes their message back.

    The row is NOT removed: it keeps its place in the thread with `deleted: true` and an
    empty `body`, because the other person's history is not the sender's to renumber.
    The text stays in the column and no endpoint ever serves it again — including the
    recipient's notification inbox: the `chat_message` push quoting it loses its preview
    (`chat.events.forget_preview`). A push already on the phone cannot be taken back.

    200 with the tombstoned message (not 204: the app replaces the row it is showing).
    Deleting a message that is already deleted answers the same thing.
    403 `not_your_message` — it is the other person's message. They can see it, so
    hiding its existence would be theatre.
    404 `not_found` — no such message, or it is in a conversation that is not theirs.
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, message_id):
        message = (Message.objects.select_related('conversation')
                   .filter(pk=message_id).first())
        if message is None or message.conversation.other_id(request.user.pk) is None:
            raise NotFound(_('Message not found.'), code='not_found')
        services.tombstone(message, request.user)
        return Response(MessageSerializer(message).data)
