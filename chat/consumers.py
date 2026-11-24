"""The live half of the chat: `wss://<host>/ws/chat/<conversation_id>/?token=<JWT>`.

(TLS ends at Caddy in production; a local server, which has no TLS, takes the same path
over the plain ws scheme.)

Authentication is `training_platform.ws_auth.authenticate_scope`, the one
implementation both other consumers use: an **access** token, never an `UntypedToken`,
so a refresh token that logging out revoked cannot open a socket.

This consumer implements NO rule of its own. A send over the socket goes through
`chat.services.send_message` — the same validation, the same `may_message` check, the
same idempotent replay and the same serializer as `POST .../messages/` — so a thread
cannot be written to over a socket in a state where HTTP would refuse it.

Protocol (JSON, one object per frame):

    Client -> Server
      {"type": "message.send", "body": "...", "client_uuid": "..."}   client_uuid optional
      {"type": "message.send", "kind": "program", "routine_id": 41 | "template_id": 7,
       "day": 2, "body": "...", "client_uuid": "..."}      a trainer's program card
      {"type": "message.read"}

    `message.send` carries text, or (the trainer only, 2026-09-27) a program card: the
    body the REST send takes, the same `services.send_message` behind it, the same
    refusals as `error` frames (`program_trainer_only`, `program_target_required`,
    `unknown_program`, `program_not_for_this_client`, `day_out_of_range`,
    `invalid_kind`). A photo, voice note or video is sent through
    `POST /api/v1/chat/media/` (a multipart upload does not belong in a JSON frame), and
    both ends hear it here as `message.new` like any other message.

    Server -> Client
      {"type": "message.new", "message": {...}}          the same object REST returns,
                                                         `kind`, `media`, `exercise` and
                                                         `program` included (on the
                                                         filesystem backend `media.url`
                                                         is relative to the API host)
      {"type": "message.updated", "message": {...}}      a message already sent now
                                                         reads differently: today, a
                                                         video's `media.poster_url`, or
                                                         a voice note's or video's
                                                         `media.duration_seconds`, was
                                                         filled by a worker. Replace
                                                         the row you hold by `id`
      {"type": "message.read", "conversation", "reader", "read_at"}
      {"type": "message.deleted", "conversation", "message", "deleted_at"}
      {"type": "error", "code": "...", "detail": "..."}   branch on `code`

An `error` frame's `code` is one of the codes `POST .../messages/` and `.../read/`
answer with (`blank_message`, `invalid_client_uuid`, `idempotency_conflict`,
`no_relation`, `blocked`, `coaching_frozen` / `coaching_unpaid` / `coaching_ended`
with `trainer_id` ...), or one of this socket's own:

      invalid_frame   not JSON, or not a JSON object
      unknown_type    a `type` this protocol does not have
      auth            the credential died since connect: the access token expired,
                      or the account was suspended. The socket closes with 4001 right
                      after; reconnect with a fresh token. Expiry is also checked
                      before every frame the server sends, so a socket that only
                      listens closes too, at the first event after its token expired.
      rate_limited    the same hourly allowance the REST routes share
                      (`training_platform.middleware.RateLimitMiddleware`, 1500 an hour
                      for a client and for a trainer): every `message.send` and
                      `message.read` frame counts against it exactly as one HTTP
                      request would. Carries
                      `retry_after` (seconds). Nothing was written.
      server_error    an unhandled fault; the socket stays open

`detail` is in the reader's language (`preferred_language`), as the REST answer is.

Close codes: 4001 no usable token (at connect, or after an `auth` frame), 4004 not this
caller's conversation.
"""
import json
import logging
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils.translation import gettext as _
from rest_framework.exceptions import APIException

from training_platform.i18n import LanguageContext
from training_platform.ws_auth import (SessionEndsMixin, authenticate_scope,
                                       token_expires_at)

from . import services
from .events import group_name
from .models import Conversation

logger = logging.getLogger(__name__)


class ChatConsumer(SessionEndsMixin, AsyncWebsocketConsumer):
    """One socket, one conversation, one group.

    Group per conversation rather than per user: the two people in a thread are the
    only ones who may ever see its events, so membership is checked once at connect and
    the fan-out needs no filtering afterwards.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.conversation = None
        self.group = None
        #: When the token this socket connected with expires (see `_out`).
        self.expires_at = None

    async def connect(self):
        self.user = await database_sync_to_async(authenticate_scope)(self.scope)
        if self.user is None or not self.user.is_authenticated:
            await self.close(code=4001)
            return
        conversation_id = self.scope['url_route']['kwargs']['conversation_id']
        self.conversation = await self._load(conversation_id)
        if self.conversation is None:
            # The same answer as the REST 404: a socket must not confirm that a
            # conversation exists for someone who is not in it.
            await self.close(code=4004)
            return
        self.group = group_name(self.conversation.pk)
        self.expires_at = token_expires_at(self.scope)
        await self.channel_layer.group_add(self.group, self.channel_name)
        # A password change, a reset or a deactivation closes it at once
        # (`training_platform.ws_auth.SessionEndsMixin`; the owner, 2026-11-24).
        if not await self.join_session_group():
            await self.close(code=4001)
            return
        await self.accept()

    async def disconnect(self, close_code):
        if self.group:
            await self.channel_layer.group_discard(self.group, self.channel_name)
        await self.leave_session_group()

    async def close_for_ended_session(self):
        """The account's session ended while this socket was open: the same `auth`
        error and 4001 as a sending socket whose credential no longer holds."""
        await self._error('auth', _('Your session has expired. Please sign in again.'))
        await self.close(code=4001)

    async def receive(self, text_data=None, bytes_data=None):
        if self.conversation is None:
            return
        # The credential is re-checked per frame, not only at connect (SIDE_BUGS
        # S-062). A socket presents its token once and then outlives it: a suspended
        # account, or one whose access token expired, kept writing to the thread — and
        # pushing to the other person's phone — while the REST route refused it. Same
        # function the AI socket calls (`training_platform.ws_auth`). The row it reads
        # also refreshes `self.user`, so a language changed since connect is honoured.
        user = await database_sync_to_async(authenticate_scope)(self.scope)
        if user is None or user.pk != self.user.pk:
            with LanguageContext.for_user(self.user):
                await self._error('auth',
                                  _('Your session has expired. Please sign in again.'))
            await self.close(code=4001)
            return
        self.user = user

        # Every sentence below is translated INSIDE the reader's language (S-093).
        # `_()` runs where it is called, and DRF renders an exception's text when it is
        # raised, so wrapping only the final `send` — as this consumer did — handed an
        # Arabic reader English for every refusal.
        with LanguageContext.for_user(self.user):
            try:
                frame = json.loads(text_data or '{}')
                if not isinstance(frame, dict):
                    raise ValueError('not an object')
            except (TypeError, ValueError):
                await self._error('invalid_frame', _('Send a JSON object.'))
                return

            kind = frame.get('type')
            if kind not in ('message.send', 'message.read'):
                await self._error('unknown_type', _('Unknown message type.'))
                return
            # WebSockets never pass through the HTTP middleware, so the owner's hourly
            # allowance was not enforced here at all (S-092): one client could post
            # without limit, each message a row, a broadcast and a queued push.
            wait = await database_sync_to_async(_rate_limited)(self.user)
            if wait is not None:
                await self._error('rate_limited',
                                  _('Too many requests. Please try again later.'),
                                  retry_after=wait)
                return
            if kind == 'message.send':
                await self._send_message(frame)
            else:
                await self._mark_read()

    # --- what the client asks for -------------------------------------------------

    async def _send_message(self, frame):
        """The same call the REST route makes; its refusals become `error` frames.

        The fan-out is not done here: `send_message` broadcasts to the group, and this
        socket is IN that group, so the sender receives their own `message.new` exactly
        as the other side does and both ends render one code path.
        """
        try:
            await database_sync_to_async(_in_language)(
                self.user, services.send_message, self.conversation, self.user, frame)
        except APIException as exc:
            await self._error(_code_of(exc), _detail_of(exc))
        except Exception:       # noqa: BLE001 — one bad frame must not close the socket
            logger.error('chat socket send failed on conversation %s',
                         self.conversation.pk, exc_info=True)
            await self._error('server_error', _('Could not send that message.'))

    async def _mark_read(self):
        try:
            await database_sync_to_async(_in_language)(
                self.user, services.mark_read, self.conversation, self.user)
        except APIException as exc:
            await self._error(_code_of(exc), _detail_of(exc))
        except Exception:       # noqa: BLE001
            logger.error('chat socket read failed on conversation %s',
                         self.conversation.pk, exc_info=True)
            await self._error('server_error', _('Could not mark that as read.'))

    # --- what the group sends here ------------------------------------------------
    # Channels turns the event's `type` into the handler name, so `message.new` on the
    # wire and `message_new` here are the same string by construction.

    async def message_new(self, event):
        await self._out('message.new', event)

    async def message_read(self, event):
        await self._out('message.read', event)

    async def message_deleted(self, event):
        await self._out('message.deleted', event)

    async def message_updated(self, event):
        await self._out('message.updated', event)

    # --- plumbing -----------------------------------------------------------------

    async def _out(self, kind, event):
        # A socket that only LISTENS never sends a frame, so the per-frame check in
        # `receive` never ran for it: it went on receiving the thread after its access
        # token expired — so after a logout and after a password change, which end the
        # session only as far as the access token's own expiry (the batch A review,
        # 2026-11-23). Every frame OUT now checks that expiry first, with no database
        # read: past it, the same `auth` error and 4001 a sending socket gets.
        if self.expires_at is not None and time.time() >= self.expires_at:
            await self._error('auth', _('Your session has expired. Please sign in again.'))
            await self.close(code=4001)
            return
        payload = {k: v for k, v in event.items() if k != 'type'}
        await self.send(text_data=json.dumps({'type': kind, **payload}, default=str))

    async def _error(self, code, detail, **extra):
        with LanguageContext.for_user(self.user):
            await self.send(text_data=json.dumps(
                {'type': 'error', 'code': code, 'detail': str(detail), **extra}))

    @database_sync_to_async
    def _load(self, conversation_id):
        conversation = (Conversation.objects.select_related('trainer', 'client')
                        .filter(pk=conversation_id).first())
        if conversation is None or conversation.other_id(self.user.pk) is None:
            return None
        return conversation


def _in_language(user, function, *args):
    """Run `function` with the reader's language active, in the thread it runs in.

    The service raises DRF exceptions whose text is rendered at construction, in
    whatever language that worker thread has active — which is not the reader's. So
    the language is switched on HERE, beside the call, rather than trusted to travel
    in from the event loop.
    """
    with LanguageContext.for_user(user):
        return function(*args)


def _rate_limited(user):
    """Seconds left in the caller's window when this frame is over the allowance, else
    None — and the frame is COUNTED either way.

    The HTTP limiter's own bucket and numbers, not a copy of them: the key is
    `rate_limit:user_<id>` in the rate-limit cache, the tier is the account's type, and
    the allowance is whatever `RateLimitMiddleware` holds for it (1500 an hour for a
    client and for a trainer, the owner's number). A socket and the REST routes
    therefore spend ONE allowance, so opening a socket is not a way around it. Fails
    open on a cache outage, as the middleware does.
    """
    from training_platform.middleware import RateLimitMiddleware

    try:
        limiter = RateLimitMiddleware(lambda request: None)
        tier = getattr(user, 'user_type', None)
        if tier not in limiter.rate_limits or tier == 'anonymous':
            tier = 'client'
        client_id = f"user_{user.pk}"
        if limiter._is_rate_limited(client_id, tier):
            return limiter._seconds_left(client_id, tier)
    except Exception:       # noqa: BLE001 — a cache outage must not close the chat
        logger.warning('chat socket rate limit unavailable', exc_info=True)
    return None


def _code_of(exc):
    """The `code` the app branches on, whatever shape the exception carries.

    A `service_code` first, as the REST exception handler reads it, so a refusal names
    the same code on both doors (`invalid_client_uuid`, `chat.services`)."""
    service_code = getattr(exc, 'service_code', None)
    if isinstance(service_code, str) and service_code:
        return service_code
    codes = exc.get_codes()
    while isinstance(codes, (list, tuple)) and codes:
        codes = codes[0]
    if isinstance(codes, dict):
        first = next(iter(codes.values()), 'error')
        return _code_of_value(first)
    return str(codes)


def _code_of_value(value):
    while isinstance(value, (list, tuple)) and value:
        value = value[0]
    return str(value)


def _detail_of(exc):
    detail = exc.detail
    if isinstance(detail, dict):
        detail = next(iter(detail.values()), '')
    if isinstance(detail, (list, tuple)) and detail:
        detail = detail[0]
    return str(detail)
