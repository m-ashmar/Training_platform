"""The live half of the chat: `ws://host/ws/chat/<conversation_id>/?token=<JWT>`.

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
      {"type": "message.read"}

    Server -> Client
      {"type": "message.new", "message": {...}}          the same object REST returns
      {"type": "message.read", "conversation", "reader", "read_at"}
      {"type": "message.deleted", "conversation", "message", "deleted_at"}
      {"type": "error", "code": "...", "detail": "..."}   branch on `code`

Close codes: 4001 no usable token, 4004 not this caller's conversation.
"""
import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils.translation import gettext as _
from rest_framework.exceptions import APIException

from training_platform.i18n import LanguageContext
from training_platform.ws_auth import authenticate_scope

from . import services
from .events import group_name
from .models import Conversation

logger = logging.getLogger(__name__)


class ChatConsumer(AsyncWebsocketConsumer):
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
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if self.group:
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if self.conversation is None:
            return
        try:
            frame = json.loads(text_data or '{}')
            if not isinstance(frame, dict):
                raise ValueError('not an object')
        except (TypeError, ValueError):
            await self._error('invalid_frame', _('Send a JSON object.'))
            return

        kind = frame.get('type')
        if kind == 'message.send':
            await self._send_message(frame)
        elif kind == 'message.read':
            await self._mark_read()
        else:
            await self._error('unknown_type', _('Unknown message type.'))

    # --- what the client asks for -------------------------------------------------

    async def _send_message(self, frame):
        """The same call the REST route makes; its refusals become `error` frames.

        The fan-out is not done here: `send_message` broadcasts to the group, and this
        socket is IN that group, so the sender receives their own `message.new` exactly
        as the other side does and both ends render one code path.
        """
        try:
            await database_sync_to_async(services.send_message)(
                self.conversation, self.user, frame)
        except APIException as exc:
            await self._error(_code_of(exc), _detail_of(exc))
        except Exception:       # noqa: BLE001 — one bad frame must not close the socket
            logger.error('chat socket send failed on conversation %s',
                         self.conversation.pk, exc_info=True)
            await self._error('server_error', _('Could not send that message.'))

    async def _mark_read(self):
        try:
            await database_sync_to_async(services.mark_read)(
                self.conversation, self.user)
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

    # --- plumbing -----------------------------------------------------------------

    async def _out(self, kind, event):
        payload = {k: v for k, v in event.items() if k != 'type'}
        await self.send(text_data=json.dumps({'type': kind, **payload}, default=str))

    async def _error(self, code, detail):
        with LanguageContext.for_user(self.user):
            await self.send(text_data=json.dumps(
                {'type': 'error', 'code': code, 'detail': str(detail)}))

    @database_sync_to_async
    def _load(self, conversation_id):
        conversation = (Conversation.objects.select_related('trainer', 'client')
                        .filter(pk=conversation_id).first())
        if conversation is None or conversation.other_id(self.user.pk) is None:
            return None
        return conversation


def _code_of(exc):
    """The `code` the app branches on, whatever shape the exception carries."""
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
