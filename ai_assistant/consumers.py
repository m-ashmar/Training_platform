"""
consumers.py — WebSocket consumer for AI chat.

Handles WebSocket connections at ws://host/ws/ai/chat/?token=<JWT>.
Authenticates via JWT query string (proven pattern from SocialConsumer),
checks premium subscription, enforces rate limiting, and streams
GPT responses to the client.

LANGUAGE SAFETY: Uses LanguageContext.for_user() per-handler, NOT
translation.activate() in connect(), to avoid ASGI threadlocal leaks.
"""

import json
import logging
from datetime import date, datetime, timedelta

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext as _
from urllib.parse import parse_qs

from ai_assistant.services.chat_service import ChatService
from ai_assistant.services.security import InputSanitizer
from training_platform.i18n import LanguageContext

logger = logging.getLogger(__name__)


class AIChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket endpoint: ws://host/ws/ai/chat/?token=<JWT>

    Protocol (JSON messages):

    Client → Server:
      {"type": "message", "content": "...", "session_id": "uuid|null"}
      {"type": "feedback", "message_id": 123, "feedback": "positive|negative"}

    Server → Client:
      {"type": "status", "content": "..."}
      {"type": "token", "content": "partial text"}
      {"type": "tool_used", "name": "...", "display": "..."}
      {"type": "complete", "session_id": "uuid", "message_id": 123, "tools_used": [...]}
      {"type": "error", "content": "...", "code": "rate_limit|auth|server"}
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.chat_service = ChatService()
        self.sanitizer = InputSanitizer()

    async def connect(self):
        """Authenticate and check premium status on WebSocket connect."""
        self.user = await self._authenticate()
        if not self.user:
            await self.close(code=4001)
            return

        if not await self._check_premium():
            await self.close(code=4003)
            return

        await self.accept()

        # Log behavior event
        await self._log_chat_opened()

        # Send welcome — wrapped in LanguageContext per-message (ASGI safe)
        with LanguageContext.for_user(self.user):
            await self.send(text_data=json.dumps({
                "type": "status",
                "content": str(_("Connected to AI Assistant. How can I help you today?")),
            }))

    async def disconnect(self, close_code):
        """Handle WebSocket disconnect."""
        logger.info(
            f"AI chat disconnected: user={getattr(self.user, 'id', '?')}, "
            f"code={close_code}"
        )

    async def receive(self, text_data=None, bytes_data=None):
        """Handle incoming WebSocket messages."""
        if not self.user:
            # No LanguageContext needed — user is unknown
            await self.send(text_data=json.dumps({
                "type": "error",
                "content": "Not authenticated.",
                "code": "auth",
            }))
            return

        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            with LanguageContext.for_user(self.user):
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Invalid message format.")),
                    "code": "invalid_input",
                }))
            return

        msg_type = data.get("type")

        if msg_type == "message":
            await self._handle_message(data)
        elif msg_type == "feedback":
            await self._handle_feedback(data)
        else:
            with LanguageContext.for_user(self.user):
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Unknown message type: %(type)s") % {"type": msg_type}),
                    "code": "invalid_input",
                }))

    async def _handle_message(self, data):
        """Process a user chat message."""
        content = data.get("content", "").strip()
        session_id = data.get("session_id")

        # All user-facing responses inside LanguageContext
        with LanguageContext.for_user(self.user):
            if not content:
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Message cannot be empty.")),
                    "code": "invalid_input",
                }))
                return

            # Unbounded prompt size = unbounded token spend. A 200 KB frame was
            # previously accepted and forwarded to the model verbatim.
            # Must match InputSanitizer's own limit, otherwise a message passes here and
            # is then silently truncated to half its length before reaching the model.
            from ai_assistant.services.security import InputSanitizer
            max_chars = getattr(settings, 'AI_ASSISTANT_CONFIG', {}).get(
                'MAX_MESSAGE_CHARS', InputSanitizer.MAX_LENGTH)
            if len(content) > max_chars:
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Message is too long (max %(n)d characters).") % {"n": max_chars}),
                    "code": "message_too_long",
                }))
                return

            # Entitlement is re-checked per message. Checking only at connect meant a
            # cancelled or lapsed subscription kept working for as long as the socket
            # stayed open — and an active client never lets it idle out.
            if not await self._check_premium():
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Your AI assistant subscription is no longer active.")),
                    "code": "subscription_inactive",
                }))
                await self.close(code=4003)
                return

            # Check rate limit
            allowed, remaining, resets_at = await self._check_rate_limit()
            if not allowed:
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Daily message limit reached. Resets at %(time)s.") % {"time": resets_at}),
                    "code": "rate_limit",
                    "remaining": 0,
                    "resets_at": resets_at,
                }))
                return

            # Stream GPT response
            try:
                async for event in self.chat_service.chat_stream(
                    self.user, content, session_id,
                ):
                    await self.send(text_data=json.dumps(event, default=str))

                # Decrement rate limit after successful response
                await self._increment_rate_limit()

                # Send remaining count
                await self.send(text_data=json.dumps({
                    "type": "rate_limit",
                    "remaining": remaining - 1,
                    "resets_at": resets_at,
                }))

            except Exception as e:
                logger.exception(f"Chat stream error: {e}")
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Something went wrong. Please try again.")),
                    "code": "server",
                }))

    async def _handle_feedback(self, data):
        """Process user feedback (👍/👎) for a message."""
        message_id = data.get("message_id")
        feedback = data.get("feedback")

        with LanguageContext.for_user(self.user):
            if feedback not in ("positive", "negative"):
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Feedback must be 'positive' or 'negative'.")),
                    "code": "invalid_input",
                }))
                return

            if not message_id:
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("message_id is required.")),
                    "code": "invalid_input",
                }))
                return

            success = await self._save_feedback(message_id, feedback)
            await self.send(text_data=json.dumps({
                "type": "feedback_saved" if success else "error",
                "content": str(_("Thanks for the feedback!")) if success else str(_("Could not save feedback.")),
            }))

    # --- Authentication ---

    @database_sync_to_async
    def _authenticate(self):
        """
        Authenticate via JWT in query string.
        Reuses the proven pattern from SocialConsumer.
        """
        try:
            from rest_framework_simplejwt.tokens import UntypedToken
            from rest_framework_simplejwt.authentication import JWTAuthentication
            from django.db import close_old_connections

            query_string = self.scope.get("query_string", b"").decode()
            token = parse_qs(query_string).get("token", [None])[0]
            if not token:
                logger.warning("AI chat: no token provided")
                return None

            validated_token = UntypedToken(token)
            jwt_auth = JWTAuthentication()
            user = jwt_auth.get_user(validated_token)
            close_old_connections()
            return user
        except Exception as e:
            logger.warning(f"AI chat auth failed: {e}")
            return None

    @database_sync_to_async
    def _check_premium(self):
        """Verify the user has an active subscription with AI advice access."""
        from subscription.models import Subscription
        return Subscription.objects.filter(
            user=self.user,
            status='active',
            has_ai_advice=True,
        ).exists()

    # --- Rate Limiting ---

    async def _check_rate_limit(self):
        """Check if user has messages remaining today (increment-first to avoid race)."""
        config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
        max_messages = config.get('MAX_MESSAGES_PER_DAY', 50)
        from training_platform.cache import ratelimit_cache

        cache_key = f"ai_chat_limit:{self.user.id}:{timezone.localdate().isoformat()}"
        rl = ratelimit_cache()

        # Calculate reset time (midnight)
        tomorrow = datetime.combine(
            timezone.localdate() + timedelta(days=1), datetime.min.time(),
        )
        seconds_until_midnight = int((tomorrow - datetime.now()).total_seconds())
        resets_at = tomorrow.isoformat()

        # Increment first, then check (prevents concurrent bypass)
        try:
            current = rl.incr(cache_key)
        except ValueError:
            rl.set(cache_key, 1, seconds_until_midnight)
            current = 1

        if current > max_messages:
            # Roll back the over-limit increment so counter stays accurate
            try:
                rl.decr(cache_key)
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)
            return False, 0, resets_at

        remaining = max_messages - current
        return True, remaining, resets_at

    async def _increment_rate_limit(self):
        """No-op: increment already done in _check_rate_limit."""
        pass

    # --- Helpers ---

    @database_sync_to_async
    def _save_feedback(self, message_id, feedback):
        """Save user feedback to AITrainingData."""
        from ai_assistant.models import AITrainingData, ChatMessage
        try:
            msg = ChatMessage.objects.get(
                id=message_id,
                session__user=self.user,
                role='assistant',
            )
            # Update training data
            AITrainingData.objects.filter(
                user=self.user,
                session=msg.session,
                ai_response=msg.content[:100],  # partial match
            ).update(user_feedback=feedback)
            return True
        except ChatMessage.DoesNotExist:
            return False

    @database_sync_to_async
    def _log_chat_opened(self):
        """Log a chat_opened behavior event."""
        from ai_assistant.models import UserBehaviorEvent
        UserBehaviorEvent.objects.create(
            user=self.user,
            event_type='chat_opened',
        )
