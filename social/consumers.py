import logging
from django.utils.translation import gettext as _
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from training_platform.ws_auth import authenticate_scope

logger = logging.getLogger(__name__)


class SocialConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = await self.get_user_from_token()
        if self.user is None or not self.user.is_authenticated:
            await self.close()
            return
            
        logger.warning(f"User {self.user.id} connected to legacy WebSocket. This endpoint is deprecated.")
            
        self.user_group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(
            self.user_group_name, self.channel_name
        )
        await self.accept()
        # Optionally send a welcome message
        await self.send(
            text_data=json.dumps({
                "type": "connection",
                "message": _("WebSocket connected. DEPRECATED: Please migrate to Firebase Cloud Messaging.")
            })
        )

    async def disconnect(self, close_code):
        if hasattr(self, "user_group_name"):
            await self.channel_layer.group_discard(
                self.user_group_name, self.channel_name
            )

    async def receive(self, text_data=None, bytes_data=None):
        # TODO: Handle incoming messages (e.g., client pings, custom actions)
        await self.send(
            text_data=json.dumps({"type": "echo", "data": text_data})
        )

    async def send_notification(self, event):
        # Example: send notification to client
        await self.send(text_data=json.dumps(event["data"]))

    @database_sync_to_async
    def get_user_from_token(self):
        """Authenticate from ?token= (or an Authorization header).

        Shared with AIChatConsumer. See training_platform.ws_auth for why this must
        be an AccessToken and not an UntypedToken.
        """
        return authenticate_scope(self.scope) 