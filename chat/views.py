"""`/api/v1/chat/` — messages between a trainer and their client: text, and (B-091) a
photo, a voice note or a video with an optional caption.

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

NOT FILTERED FOR BANNED TERMS, deliberately (`moderation/ugc_filter.py`, 2026-09-27).
Spec B1 asks for a banned-terms filter on post and comment creation: text written for
everyone to read. A chat is private, between one trainer and one client who chose each
other, and its safety tools are the ones B1 names for it — reporting a message
(`coach_message` on `POST /api/v1/reports/`) and blocking, which closes the thread for
writing both ways. A message body is stored exactly as typed.
"""
import logging

from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, MultiPartParser
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
         "last_message": {"id", "sender", "kind", "body" (200 chars), "deleted",
                          "created_at"}
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
        # Django's ValidationError, not DRF's: only Django's carries its `code` to the
        # response's top-level `code` (training_platform/exception_handler.py). DRF's
        # answered `validation_error`, so the documented `invalid_body` never reached
        # the app (the owner, 2026-09-24: every refusal names its own code).
        from django.core.exceptions import ValidationError

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

        {"id", "conversation", "sender", "kind", "body", "media", "exercise",
         "client_uuid", "created_at", "read_at", "deleted_at", "deleted"}

    `kind` is `text`, `image`, `voice` or `video`; `media` is null on a text message
    and `{url, duration_seconds, width, height}` on the others, where `url` is signed
    and expires (use it now, never store it); `exercise` is `{id, name}` on a video
    that names its move, else null (see `MediaMessageView` and `MessageSerializer`).
    A message the sender took back is still in the list with `deleted: true`, an
    empty `body` and `media: null`, so the thread does not renumber under a scroll.

    Photos, voice notes and videos are SENT through `POST /api/v1/chat/media/`; this
    POST takes text only.

    **POST** — send. Body `{"body": "...", "client_uuid": "..."}` (`client_uuid`
    optional). `body` is capped at 4000 characters and is stored exactly as typed;
    it is not checked against the banned-terms list (see the module docstring).
    201 with the message. A repeat of a `client_uuid` already used answers **200** with
    the FIRST message and writes nothing, so a retry after a lost answer is safe.
    400 `validation_error` (an empty or whitespace-only body: `blank_message` on
    `field_errors.body`). 400 `invalid_client_uuid` — a `client_uuid` that is not a
    UUID; that is the top-level `code`, and `field_errors.client_uuid` carries it too.
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
        return Response({'results': MessageSerializer(
                            rows, many=True, context={'request': request}).data,
                         'next_before': next_before})

    def post(self, request, conversation_id):
        conversation = _mine_or_404(request.user, conversation_id)
        message, created = services.send_message(conversation, request.user,
                                                 request.data)
        return Response(MessageSerializer(message, context={'request': request}).data,
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class MediaMessageView(APIView):
    """POST /api/v1/chat/media/ — send a photo, a voice note or a video.

    `multipart/form-data`:

        conversation   the thread's id (required)
        kind           `image` | `voice` | `video` (required)
        file           the bytes (required)
        body           the caption, optional, capped at 4000 characters, kept as typed
        exercise_id    a video only: the move it shows. A CLIENT's video with one is a
                       FORM CHECK — it waits in the trainer's attention queue
                       (`GET /api/v1/trainer/attention/`) until the trainer writes in
                       this thread after it
        client_uuid    optional, the app's own id: a retry answers the first message

    A route of its own, not a second body type on `.../messages/`: a video is bigger
    than the API's 15 MB body cap, and the ceiling that lets it through
    (`settings.LARGE_UPLOAD_PATHS`) is granted to exact paths only. Answers on both
    spellings, with and without the trailing slash — the size check runs before
    `CommonMiddleware` could redirect a POST, and a redirected POST loses its body.

    Each kind goes through the platform's existing guard (`chat/media.py`): a photo is
    capped at 5 MB (checked first), content-checked and RE-ENCODED (no EXIF, no GPS
    reaches the other person), and a REFUSED photo is not kept anywhere — no sample of
    it is quarantined; a video is identified from its own bytes and capped at
    `MEDIA_VIDEO_MAX_BYTES` (64 MB) and `MEDIA_VIDEO_MAX_SECONDS` (90 s) — the media
    kit's numbers; a voice note the same way, under `CHAT_VOICE_MAX_BYTES` (10 MB) and
    the same 90 s. The stored file is private: its URL is signed, expires, and appears
    only in answers the two people in this conversation receive.

    A DAILY ALLOWANCE per sender: `CHAT_MEDIA_DAILY_COUNT` files (60) and
    `CHAT_MEDIA_DAILY_BYTES` (512 MB) over any rolling 24 hours, counting every photo,
    voice note and video the sender sent in that window — taken-back ones too, since
    their files are kept — and the file being sent. Past either, 429
    `media_quota_exceeded` with `retry_after` and `retry_after_seconds` (and a
    `Retry-After` header): the seconds until the OLDEST of those files leaves the
    window, the earliest moment the answer can change. It is decided from the upload's
    size, before its bytes are read. Text messages never count and are never refused
    by it. (Numbers proposed, not yet confirmed by the owner.)

    201 with the message (the `MessageSerializer` shape: `kind`, `body`, `media`,
    `exercise`, ...). A repeat of a `client_uuid` answers **200** with the FIRST message
    and stores nothing — it is answered even past the daily allowance. The other side
    hears `message.new` on the socket and gets the `chat_message` push (the caption, or
    "Photo" / "Voice note" / "Video" in their language — never a link).

    Every refusal, and its `code`, in the order it is decided — this is every code the
    route can answer:

    | status | `code` | when |
    |---|---|---|
    | 411 | `length_required` | a chunked body with no `Content-Length` (every POST of the API) |
    | 413 | `payload_too_large` | the body is over the route's ceiling, `MEDIA_VIDEO_MAX_BYTES` + 1 MB |
    | 429 | `rate_limited` | the hourly allowance every route shares (`retry_after`) |
    | 401 | `not_authenticated`, `token_not_valid` | no token; a token that is expired, forged or not an access token |
    | 415 | `unsupported_media_type` | the body is not `multipart/form-data` (or a urlencoded form, which carries no file) |
    | 400 | `parse_error` | a multipart body that cannot be parsed (no boundary, say) |
    | 404 | `not_found` | no such conversation, or not the caller's (never 403) |
    | 400 | `invalid_client_uuid` | `client_uuid` is not a UUID — the top-level `code`, and `field_errors.client_uuid` |
    | 409 | `idempotency_conflict` | `client_uuid` is the other person's message here |
    | 400 | `validation_error` | a form field is wrong (`field_errors` names it: a `kind` that is not `image`/`voice`/`video`, a caption over 4000 characters, a non-numeric `exercise_id`) |
    | 400 | `exercise_only_on_video` | `exercise_id` on a photo or a voice note |
    | 400 | `unknown_exercise` | an exercise the sender may not see, or none by that id |
    | 403 | `no_relation`, `blocked` | the pair rule, exactly as a text send |
    | 403 | `coaching_frozen`, `coaching_unpaid`, `coaching_ended` | the coaching cannot be used (with `trainer_id`); the thread stays readable |
    | 429 | `media_quota_exceeded` | the sender's daily allowance is spent (above) |
    | 400 | `media_file_required` | no `file` part |
    | 400 | `media_not_a_video`, `media_video_too_large`, `media_video_too_long`, `empty_file` | a video the guard refused: not MP4/MOV/WebM by its bytes, over 64 MB, declaring over 90 s, or empty |
    | 400 | `media_not_audio`, `media_audio_too_large`, `media_audio_too_long`, `empty_file` | a voice note the guard refused: not M4A/Ogg/WebM by its bytes, over 10 MB, declaring over 90 s, or empty |
    | 400 | `file_too_large`, `empty_file`, `dangerous_extension`, `invalid_mime_type`, `corrupt_image`, `invalid_aspect_ratio`, `unreadable_image`, `unsupported_format`, `image_dimensions_too_large`, `image_too_many_pixels` | a photo the platform's image guard refused, respectively: over 5 MB; empty; a file name ending in an executable's extension; not JPEG/PNG/GIF/WebP by its bytes (a HEIC is refused here); broken at its header; longer than 10:1 on one side; broken past its header (a truncated or half-downloaded photo); a format the re-encoder does not write; over 10,000 px a side; over 40 MP (or an animation over its frame budget) |
    | 400 | `mime_detection_failed`, `file_validation_error` | the image guard itself failed on the file |
    | 503 | `upload_busy` | a photo, while the server is already decoding as many images as its memory holds (`training_platform/image_budget.py`): nothing was decoded, stored or counted against the allowance; send it again after `Retry-After` seconds (`retry_after_seconds` in the body) |

    Nothing here differs by build: a message is not a product.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        from django.core.exceptions import ValidationError as DjangoValidationError

        try:
            message, created = services.send_media_message(
                request.user, request.data, request.FILES.get('file'))
        except DjangoValidationError as refused:
            return Response({'error': refused.messages[0],
                             'code': getattr(refused, 'code', None) or 'invalid_media'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(MessageSerializer(message, context={'request': request}).data,
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
