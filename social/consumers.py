import json
from channels.generic.websocket import AsyncWebsocketConsumer
from urllib.parse import parse_qs
from channels.db import database_sync_to_async


class SocialConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = await self.get_user_from_token()
        if self.user is None or not self.user.is_authenticated:
            await self.close()
            return
        self.user_group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(
            self.user_group_name, self.channel_name
        )
        await self.accept()
        # Optionally send a welcome message
        await self.send(
            text_data=json.dumps({
                "type": "connection",
                "message": "WebSocket connected."
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
        try:
            from rest_framework_simplejwt.tokens import UntypedToken
            from django.db import close_old_connections
            from rest_framework_simplejwt.authentication import JWTAuthentication
            # JWT in querystring: ws://.../ws/social/?token=...
            query_string = self.scope.get("query_string", b"").decode()
            token = parse_qs(query_string).get("token", [None])[0]
            if not token:
                return None
            validated_token = UntypedToken(token)
            jwt_auth = JWTAuthentication()
            user = jwt_auth.get_user(validated_token)
            close_old_connections()
            return user
        except Exception:
            return None 