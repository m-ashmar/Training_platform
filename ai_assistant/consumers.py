"""
consumers.py — WebSocket consumer for AI chat.

Handles WebSocket connections at ws://host/ws/ai/chat/?token=<JWT>.
Authentication is delegated to training_platform.ws_auth, shared with
SocialConsumer. It checks premium entitlement, enforces rate limiting, and
streams GPT responses to the client.

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

from ai_assistant.services.chat_service import ChatService
from ai_assistant.services.security import InputSanitizer
from ai_assistant.store_safe import build_of_scope
from training_platform.disclaimers import ai_coach_disclaimer
from training_platform.i18n import LanguageContext
from training_platform.ws_auth import authenticate_scope

logger = logging.getLogger(__name__)


def _daily_window(now=None):
    """Today's date, the aware instant the day ends, and the seconds until then.

    In `TIME_ZONE`, aware. This combined `localdate()` into a NAIVE midnight and
    subtracted a naive `datetime.now()`, so `resets_at` reached the app with no offset
    at all ("2026-09-24T00:00:00"), and the TTL was right only because Django happens
    to export `TIME_ZONE` into the process's `TZ` (SIDE_BUGS S-050).
    """
    now = now or timezone.now()
    zone = timezone.get_current_timezone()
    today = timezone.localdate(now, zone)
    midnight = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=zone)
    return today, midnight, max(1, int((midnight - now).total_seconds()))


def daily_limit_key(user_id, today=None):
    """The one key both the reservation and the refund use for a user's day."""
    today = today or timezone.localdate()
    return f"ai_chat_limit:{user_id}:{today.isoformat()}"


def reserve_daily_slot(user_id, max_messages, *, store=None, now=None):
    """Take one of `user_id`'s messages for today. Returns (allowed, remaining,
    resets_at), `resets_at` an ISO 8601 instant WITH its offset.

    The first message of the day was not atomic (SIDE_BUGS S-089): `incr` on a missing
    key raises, and every socket that saw it raise then `set` the counter to 1, so N
    sockets sending their first message together were all allowed and the counter
    ended at 1. `add` is SET-if-absent, so exactly one of them creates the key at 0
    and INCR alone decides: every caller gets a distinct number.
    """
    from training_platform.cache import ratelimit_cache

    rl = store or ratelimit_cache()
    today, midnight, ttl = _daily_window(now)
    key = daily_limit_key(user_id, today)
    resets_at = midnight.isoformat()

    rl.add(key, 0, ttl)
    try:
        current = rl.incr(key)
    except ValueError:
        # Gone between the two calls: the day ended in that instant. A fresh day's
        # counter, created the same atomic way.
        rl.add(key, 0, ttl)
        current = rl.incr(key)

    if current > max_messages:
        # Roll back the over-limit increment so counter stays accurate
        try:
            rl.decr(key)
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
        return False, 0, resets_at

    return True, max_messages - current, resets_at


class AIChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket endpoint: ws://host/ws/ai/chat/?token=<JWT>

    **The build.** Declare it on the handshake the way every REST call does, as
    `X-App-Channel: apk | play | ios | web` and `X-App-Version: 1.4.0` headers; a
    client that cannot set headers on a WebSocket sends them as query parameters
    instead, `?token=<JWT>&app_channel=ios&app_version=1.4.0`. A header that is present
    wins over the parameter. Read once, at connect (a build's channel never changes at
    runtime), by `ai_assistant.store_safe.build_of_scope` with policy's own parser:
    exact lower-case channels only. A socket that declares no channel, or one policy
    does not know, is treated as REST treats such a caller — policy's STRICTEST flags,
    which sell nothing — so it gets the store build's coach, never a refusal.

    What differs by build: on a build that may not sell plans or routine products
    (`store_safe.payment_talk_blocked` — today `play`, `ios` and no declaration), a
    question about paying, prices, subscribing or buying is answered with one fixed
    sentence that names no price, no place and no link, without calling the model;
    every other question is answered by the model with the store rule in its
    instructions. On `apk` and `web` the coach answers everything. The same for every
    user of a build. The turn's frames are the ordinary ones (`token`, `complete`,
    `rate_limit`), so the app needs no branch for it.

    Protocol (JSON messages):

    Client → Server:
      {"type": "message", "content": "...", "session_id": "uuid|null"}
      {"type": "feedback", "message_id": 123, "feedback": "positive|negative|null"}
                            (`null`, sent explicitly, clears the rating)

    Server → Client:
      {"type": "status", "content": "...", "code": "..."}
                            (`connected` also carries `medical_disclaimer`)
      {"type": "token", "content": "partial text"}
      {"type": "tool_used", "name": "...", "display": "..."}
      {"type": "complete", "session_id": "uuid", "message_id": 123, "tools_used": [...],
       "medical_disclaimer": "..."}
      {"type": "feedback_saved", "content": "..."}
      {"type": "rate_limit", "remaining": 49, "resets_at": "..."}
      {"type": "error", "content": "...", "code": "..."}

    Branch on `code`, never on `content` — `content` is translated. Every `code` this
    socket can answer with, and nothing else:

      auth                  no usable token, or the credential died mid-session (closes 4001)
      invalid_input         not JSON, not an object, unknown `type`, bad `content`,
                            bad `feedback`, missing `message_id`, empty message
      message_too_long      over `AI_ASSISTANT_CONFIG['MAX_MESSAGE_CHARS']`
      subscription_inactive entitlement lapsed since connect (closes 4003)
      rate_limit            today's allowance is spent; carries `remaining` and `resets_at`
      message_not_found     no assistant message of yours by that id (feedback); the
                            same code `POST /api/ai/feedback/` answers 404 with
      server                unhandled fault in this consumer

    and, from `services/chat_service.py` while a turn runs: `ai_consent_required`,
    `budget_exceeded`, `timeout`, `rate_limit_openai`, `config_error`, `connection`,
    `server`.

    A `status` frame carries a `code` too, and the app writes its own words for it —
    the sentence in `content` is translated and is there for a build that predates the
    code. The full set: `connected` on connect, then `getting_ready` and, once per pass
    of the tool-calling loop, `thinking`, `checking_data`, `analyzing`,
    `looking_deeper`, `almost_done` (`services/chat_service.py ITERATION_STATUSES`).

    `medical_disclaimer` (spec §9 B4) is one sentence in the reader's language — the AI
    coach can be wrong and is not a substitute for a trainer or a doctor — sent on the
    `connected` status (so the chat screen can show it before the first reply) and on
    every `complete`. The same on every build. Additive: a build that ignores it loses
    nothing.

    A turn that ends on an `error` is NOT charged against the daily allowance
    (`_reserve_rate_limit`), and the `rate_limit` frame that closes every turn carries
    what is actually left. `resets_at` is the next midnight in the server's
    `TIME_ZONE`, as ISO 8601 with its offset (`2026-09-24T00:00:00+03:00`); it used to
    carry no offset at all.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        #: `(channel, version)` from the handshake; `(None, None)` until connect reads
        #: it, which is policy's strictest answer.
        self.build = (None, None)
        self.chat_service = ChatService()
        self.sanitizer = InputSanitizer()

    async def connect(self):
        """Authenticate and check premium status on WebSocket connect."""
        # The build this socket speaks for, read once: a build's channel is a
        # compiled-in constant and never changes while it runs.
        self.build = build_of_scope(self.scope)
        self.chat_service.build = self.build
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
                "code": "connected",
                "medical_disclaimer": ai_coach_disclaimer(),
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

        # A well-formed JSON frame is not a well-formed MESSAGE. `json.loads` returns
        # lists and ints quite legitimately, and `data.get` on either is an
        # AttributeError — which killed the socket with close code 1011 and no reason,
        # while the protocol at the top of this class documents an `invalid_input`
        # error. Seven shapes did it: a list body, an int body, `content` as a number, a
        # list or null, and `message_id` as a string or a list. Each also wrote a fresh
        # traceback into the server log, so it was a cheap log flood too.
        if not isinstance(data, dict):
            with LanguageContext.for_user(self.user):
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Expected a JSON object.")),
                    "code": "invalid_input",
                }))
            return

        # The credential is re-checked per message, not only at connect. A socket
        # presents its token once and then outlives it: logging out did not end the AI
        # session, and neither did disabling the account.
        if not await self._still_authenticated():
            with LanguageContext.for_user(self.user):
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Your session has expired. Please sign in again.")),
                    "code": "auth",
                }))
            await self.close(code=4001)
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
        # `data.get("content", "").strip()` assumed a string. A JSON number, list or null
        # is a legitimate value and an AttributeError here, which closed the socket.
        raw_content = data.get("content")
        if raw_content is not None and not isinstance(raw_content, str):
            with LanguageContext.for_user(self.user):
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("`content` must be text.")),
                    "code": "invalid_input",
                }))
            return
        content = (raw_content or "").strip()
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

            # Take the slot BEFORE the turn runs — see `_reserve_rate_limit` for why
            # the reservation cannot wait until there is an answer to charge for.
            allowed, remaining, resets_at = await self._reserve_rate_limit()
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
            answered = False
            try:
                async for event in self.chat_service.chat_stream(
                    self.user, content, session_id,
                ):
                    # `complete` is the last event of a turn that produced a reply and
                    # saved it; a turn that refuses or fails ends on an `error` event
                    # instead, and that one is not the user's to pay for.
                    if isinstance(event, dict) and event.get("type") == "complete":
                        answered = True
                    await self.send(text_data=json.dumps(event, default=str))

            except Exception as e:
                logger.exception(f"Chat stream error: {e}")
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Something went wrong. Please try again.")),
                    "code": "server",
                }))

            if not answered:
                remaining = await self._refund_rate_limit(remaining)

            # `remaining` already excludes this turn: `_reserve_rate_limit` counts the
            # reservation in. Sending `remaining - 1` here under-reported the user's
            # allowance by one all the way to zero.
            await self.send(text_data=json.dumps({
                "type": "rate_limit",
                "remaining": remaining,
                "resets_at": resets_at,
            }))

    async def _handle_feedback(self, data):
        """Process user feedback (👍/👎) for a message."""
        message_id = data.get("message_id")
        feedback = data.get("feedback")

        with LanguageContext.for_user(self.user):
            # An explicit `null` clears the rating (as on `POST /api/ai/feedback/`); a
            # frame that leaves the key out is still refused, never read as a clear.
            clearing = "feedback" in data and feedback is None
            if feedback not in ("positive", "negative") and not clearing:
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("Feedback must be 'positive' or 'negative'.")),
                    "code": "invalid_input",
                }))
                return

            # A whole number, and only that. `_save_feedback` handed this straight to
            # `ChatMessage.objects.get(id=...)`, which raises ValueError for a string
            # and TypeError for a list — neither caught, so the socket died with 1011.
            if isinstance(message_id, bool) or not isinstance(message_id, int):
                try:
                    message_id = int(str(message_id).strip())
                except (TypeError, ValueError):
                    message_id = None
            if not message_id or message_id <= 0:
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "content": str(_("message_id is required.")),
                    "code": "invalid_input",
                }))
                return

            success = await self._save_feedback(message_id, feedback)
            if success:
                await self.send(text_data=json.dumps({
                    "type": "feedback_saved",
                    "content": str(_("Thanks for the feedback!")),
                }))
                return
            # `message_not_found` is the same code the REST twin answers with
            # (`ai_assistant/views.py` `FeedbackView`), because it is the same
            # condition: no assistant message of yours by that id. This was the one
            # error frame in the consumer with no `code` at all, so the app had
            # nothing to branch on and fell back to reading the sentence.
            await self.send(text_data=json.dumps({
                "type": "error",
                "content": str(_("Could not save feedback.")),
                "code": "message_not_found",
            }))

    # --- Authentication ---

    @database_sync_to_async
    def _still_authenticated(self):
        """Re-check the credential this socket connected with, per message.

        A WebSocket presents its token exactly once, so the 60-minute access-token
        lifetime never bit here: measured with a 5-second token, the socket kept
        answering after it expired, after logout blacklisted the refresh token, and after
        the account was disabled.
        """
        from training_platform.ws_auth import still_authenticated

        return still_authenticated(self.scope)

    @database_sync_to_async
    def _authenticate(self):
        """Authenticate via JWT in the query string (or an Authorization header).

        Shared with SocialConsumer. The previous copy of this method described itself
        as reusing "the proven pattern" from that consumer; the pattern accepted
        refresh tokens, including revoked ones, so copying it doubled the hole.
        """
        return authenticate_scope(self.scope)

    @database_sync_to_async
    def _check_premium(self):
        """Verify the user holds a live subscription that includes AI advice.

        `Subscription.is_active` is the single definition of "live" and it checks
        end_date and any trial window, not just the status column. This used to be a
        bare `filter(status='active', has_ai_advice=True)`, which asks a different and
        weaker question: nothing sweeps lapsed rows out of 'active', so a subscription
        that ended a year ago still matched and kept spending model tokens. The HTTP
        routes guarding the same feature go through HasAIAdviceAccess -> is_active and
        correctly answered 403 for exactly that user, so the two surfaces disagreed.
        """
        from subscription.models import Subscription
        subscription = (
            Subscription.objects
            .select_related('plan')
            .filter(user=self.user)
            .first()
        )
        return bool(subscription and subscription.is_active and subscription.has_ai_advice)

    # --- Rate Limiting ---

    async def _reserve_rate_limit(self):
        """Take one of today's slots, atomically. Returns (allowed, remaining, resets_at).

        Reserve-then-refund, not check-then-charge. The counter is a single Redis
        integer and `incr` is the only operation on it that two sockets cannot both
        win: reading it, deciding, and writing it back lets two concurrent turns each
        see the same "one left" and both go through — which is what
        `tests/security/p9_lifecycle.py` probes with two sockets and a quota of one.

        So the slot is taken up front and GIVEN BACK by `_refund_rate_limit` when the
        turn produced no answer. Previously there was no refund, so a turn that only
        ever yielded an error — a model timeout, a refused consent, the budget gate —
        still spent the user's daily allowance: they paid for the assistant's failures.

        `remaining` counts the reservation as already spent, so it is what the user has
        left AFTER this turn. The work is `reserve_daily_slot`, synchronous, below.
        """
        config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
        return reserve_daily_slot(self.user.id, config.get('MAX_MESSAGES_PER_DAY', 50))

    async def _refund_rate_limit(self, remaining):
        """Give back the slot a failed turn reserved. Returns the corrected `remaining`.

        `decr` for the same reason `incr` takes it: the refund races the next
        reservation on the same account, and only the atomic operation keeps both
        correct. A key that has already expired at midnight is not recreated — the day
        it belonged to is over and the counter has restarted at zero.
        """
        from training_platform.cache import ratelimit_cache

        cache_key = daily_limit_key(self.user.id)
        try:
            ratelimit_cache().decr(cache_key)
        except Exception:
            # The key expired, or Redis is unavailable. Refusing the user a refund is
            # the safe direction to fail in, and it must not turn a failed turn into a
            # closed socket.
            logger.debug('could not refund an AI rate-limit slot', exc_info=True)
            return remaining
        return remaining + 1

    # --- Helpers ---

    @database_sync_to_async
    def _save_feedback(self, message_id, feedback):
        """Save the user's feedback on one of their assistant replies."""
        from ai_assistant.models import ChatMessage
        try:
            msg = ChatMessage.objects.get(
                id=message_id,
                session__user=self.user,
                role='assistant',
            )
            # By the reply's keyed pseudonym (`AITrainingData.reply_key`; the copy has
            # had no FK since the owner's decision of 2026-09-24), not on a text prefix.
            # History: `AITrainingData.message` was added to
            # replace exactly this join — the model comment says the old key was "first
            # 100 characters of ai_response, and replies routinely share an opening
            # line, so a single thumbs-up mislabelled several unrelated rows" — but
            # neither end was migrated: `chat_service` never passed `message=`, so the
            # FK was always NULL, and this filter matched nothing at all because
            # `ai_response` stores the FULL text. `.update()`'s return value was ignored
            # and `True` was returned unconditionally, so the user tapped 👍, saw
            # "Thanks for the feedback!", and nothing was recorded. The quality signal
            # for the entire assistant was silently zero.
            # Stored on the message, and mirrored onto the training row when the user
            # consented and it is already written; a row written later copies it from
            # the message. Shared with `POST /api/ai/feedback/`.
            msg.record_feedback(self.user, feedback)
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
