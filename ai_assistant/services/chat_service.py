"""
chat_service.py — Core GPT orchestration with function calling.

Manages the full chat loop: context compilation → GPT call → tool execution →
streaming response. Designed for async usage from the WebSocket consumer.

The model is reached through the OpenAI Chat Completions protocol, and WHICH provider
answers is configuration: `AI_API_KEY`, `AI_BASE_URL`, `AI_ASSISTANT_MODEL` and
`AI_EXTRA_BODY` (`settings_base.py`, "AI Integration"). With no key the coach is off:
every turn answers `config_error`. `manage.py ai_coach_eval` runs a fixed set of
Arabic questions through this service to compare one provider or model with another.
"""

import asyncio
import json
import logging
import time
from decimal import Decimal

import openai
from channels.db import database_sync_to_async
from django.conf import settings
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from ai_assistant.tool_registry import ToolRegistry, display_name
from ai_assistant.services.context_compiler import ContextCompiler
from ai_assistant.services.memory_service import MemoryService
from ai_assistant.services.security import InputSanitizer
from ai_assistant.services.data_collector import DataCollector
from ai_assistant.services.cost_tracker import CostTracker
# The one consent predicate. `moderation.services` imports no models at module level,
# precisely so a service like this one can import it without pulling half the app in.
from moderation.services import has_ai_consent
# What the coach may say about money on this build, and the disclaimer every reply
# carries (spec §9 B4, §10). Neither imports a model at module level.
from ai_assistant.store_safe import (
    is_payment_question, payment_talk_blocked, store_safe_reply)
from training_platform.disclaimers import ai_coach_disclaimer

logger = logging.getLogger(__name__)

#: What the client is told while a turn is still working, one frame per pass of the
#: tool-calling loop: a stable `code` and the sentence that goes with it.
#:
#: These were five bare English literals. Everything else this socket sends is
#: translated, so an Arabic reader got their answer in Arabic with "Thinking..." and
#: "Almost done..." in front of it. Both halves are sent, and the `code` is the half
#: that matters: the rest of this platform answers a screen with codes and numbers and
#: lets the app write the words (`home/sections.py`, `routine/strength.py headline`),
#: because a status line the app owns can be phrased for its own layout, shortened, or
#: animated, and it never depends on the server's idea of the reader's language. The
#: `content` stays, translated, because a build already in a pocket reads that key and
#: knows nothing about `code`.
#:
#: Lazy, not `gettext`: this table is built at import time and rendered per frame,
#: inside the `LanguageContext` the consumer opens for the reader.
ITERATION_STATUSES = (
    ("thinking", _lazy("Thinking...")),
    ("checking_data", _lazy("Checking your data...")),
    ("analyzing", _lazy("Analyzing details...")),
    ("looking_deeper", _lazy("Looking deeper...")),
    ("almost_done", _lazy("Almost done...")),
)


class ChatService:
    """
    Orchestrates GPT chat with function calling and streaming.

    Usage (from async consumer):
        service = ChatService()
        async for event in service.chat_stream(user, "How is my squat?", session_id):
            await websocket.send(json.dumps(event))
    """

    def __init__(self, build=None):
        config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
        #: The build this service answers for, `(channel, version)` as
        #: `policy.channels` reads them. The socket sets it from its handshake
        #: (`ai_assistant.store_safe.build_of_scope`); None is a caller that declared
        #: nothing, which policy answers with its strictest flags — so a new caller
        #: that forgets to say which build it serves gets the store-safe coach.
        self.build = build
        self.model = config.get('MODEL', 'gpt-4o-mini')
        self.max_response_tokens = config.get('MAX_RESPONSE_TOKENS', 2000)
        self.temperature = config.get('TEMPERATURE', 0.7)
        self.max_tool_calls = config.get('MAX_TOOL_CALLS_PER_TURN', 5)
        self.openai_timeout = 30

        # Which provider answers is configuration (`settings_base.py`, "AI Integration"):
        # a key, an address and a model name. `AI_API_KEY` falls back to the older
        # `OPENAI_API_KEY`, here as well as in settings, so a test or a caller that
        # overrides only the old name still decides the key.
        self.api_key = (getattr(settings, 'AI_API_KEY', '')
                        or getattr(settings, 'OPENAI_API_KEY', ''))
        self.base_url = getattr(settings, 'AI_BASE_URL', '') or None
        #: Sent with every completion, for switches the protocol has no field for.
        self.extra_body = config.get('EXTRA_BODY') or None
        #: None when no provider is configured — the coach is then off, and
        #: `chat_stream` says so with `config_error` instead of calling anything.
        self.client = self._build_client()

        self.context_compiler = ContextCompiler()
        self.memory_service = MemoryService()
        self.sanitizer = InputSanitizer()
        self.data_collector = DataCollector()
        self.cost_tracker = CostTracker()

    def _build_client(self):
        """The provider client, or None when there are no credentials to build one.

        The pinned client library refuses to be constructed without a key
        (`OpenAIError: Missing credentials`). This service is built in the socket
        consumer's `__init__`, so that refusal used to escape there: with no key in the
        environment — a deploy with the AI coach deliberately off, or a test machine —
        every connection to the coach's socket died on an unhandled exception instead
        of answering. No key is a state the settings promise to survive ("a missing key
        disables the feature"), so it is answered here: no client, and every turn gets
        the `config_error` frame the app already knows.

        The library, not this method, decides what counts as credentials (it also
        reads its own environment variables), so the constructor is always called and
        only its refusal is caught.
        """
        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        try:
            return openai.AsyncOpenAI(**kwargs)
        except openai.OpenAIError as exc:
            logger.warning("AI coach is off: no provider client could be built (%s)", exc)
            return None

    async def chat_stream(self, user, message: str, session_id=None):
        """
        Async generator that yields WebSocket events for a chat turn.

        Events yielded:
          {"type": "status", "content": "...", "code": "..."}
          {"type": "tool_used", "name": "...", "display": "..."}
          {"type": "token", "content": "partial text"}
          {"type": "complete", "session_id": "...", "message_id": ..., "tools_used": [...],
           "medical_disclaimer": "..."}
          {"type": "error", "content": "...", "code": "..."}

        What differs by build (`self.build`, and nothing else): on a build that may not
        sell the store-billed goods (`store_safe.payment_talk_blocked` — `play`, `ios`,
        or no build declared), a payment / price / purchase question
        (`store_safe.is_payment_question`) is answered with the fixed
        `store_safe_reply()` — one `token` frame and a `complete`, saved like any reply
        — and the model is never called; every other question reaches the model with
        the store rule in its system prompt. On `apk` and `web` nothing changes. The
        model's own reply is streamed and saved as written on every build.
        """
        start_time = time.monotonic()

        # 1. Sanitize input
        content, flagged = self.sanitizer.sanitize(message)
        if not content:
            yield {"type": "error", "content": str(_("Message cannot be empty.")), "code": "invalid_input"}
            return

        # Consent gate — the AI coach reads this person's training, food, body
        # metrics and injuries to answer (see `services/context_compiler.py`), and a
        # store review asks that they agreed to that first. Here rather than in the
        # consumer because THIS is the single place the assistant answers: a REST or
        # Celery caller added later gets the same refusal without being told to.
        # Before the session row is created and before any completion is requested, so
        # a refused turn leaves nothing behind and costs nothing. Nothing caches it, so
        # revoking takes effect on the very next message.
        if not await database_sync_to_async(has_ai_consent)(user):
            yield {
                "type": "error",
                "content": str(_("Allow the AI coach to use your data before asking "
                                 "it anything.")),
                "code": "ai_consent_required",
            }
            return

        # The build decides what the coach may say about money (spec §3, §10: no store
        # build answers "how do I pay" with instructions). Read per turn, like the REST
        # flags, so an operator's change reaches an open socket on its next message. A
        # recognised payment question gets the fixed reply without the model — before
        # the budget gate, because it costs nothing — and is kept like any other turn.
        channel, version = self.build or (None, None)
        store_safe = await database_sync_to_async(payment_talk_blocked)(channel, version)
        if store_safe and is_payment_question(content):
            async for event in self._store_safe_turn(user, content, session_id):
                yield event
            return

        # 2. Budget gate — checked BEFORE any completion is requested. Cost used to be
        # recorded only afterwards, and the hourly task merely logged CRITICAL, so there
        # was nothing anywhere that could actually stop spending.
        if await database_sync_to_async(self.cost_tracker.is_over_daily_limit)():
            logger.critical("AI daily cost limit reached — refusing further completions")
            yield {
                "type": "error",
                "content": str(_("The AI assistant is temporarily unavailable. Please try again later.")),
                "code": "budget_exceeded",
            }
            return

        # No provider configured (`_build_client`): the coach is off. Said here — after
        # the gates that cost nothing, before the insights are computed and the session
        # row is written — so a refused turn leaves nothing behind, and with the same
        # frame an invalid key gets, which the app already handles.
        if self.client is None:
            logger.warning("AI coach turn refused: no AI provider is configured")
            yield {
                "type": "error",
                "content": str(_("AI service is temporarily unavailable. Please contact support.")),
                "code": "config_error",
            }
            return

        # 3. Ensure insights exist (cold start)
        yield {"type": "status", "content": str(_("Getting ready...")),
               "code": "getting_ready"}
        await self._ensure_insights(user)

        # 3. Get or create session
        session = await self._get_or_create_session(user, session_id)

        # 4. Build system prompt
        system_prompt = await database_sync_to_async(
            self.context_compiler.compile
        )(user, flagged_input=flagged, store_safe=store_safe)
        
        # Inject language instruction based on user preference (prepend for higher LLM weight)
        lang = getattr(user, 'preferred_language', 'en')
        if lang == 'ar':
            system_prompt = (
                "CRITICAL INSTRUCTION: You MUST respond ONLY in Arabic (Modern Standard Arabic / العربية الفصحى). "
                "Do NOT respond in English under any circumstances.\n\n"
            ) + system_prompt

        # 5. Load token-aware history
        history = await database_sync_to_async(
            self.memory_service.get_truncated_history
        )(session)

        # 6. Setup
        registry = ToolRegistry(user)
        schemas = await database_sync_to_async(registry.get_schemas)()
        messages = [{"role": "system", "content": system_prompt}] + history
        messages.append({"role": "user", "content": content})

        tool_calls_made = []
        full_response = ""

        # 7. Function calling loop
        for iteration in range(self.max_tool_calls + 1):
            try:
                yield self._iteration_status(iteration)

                request = {
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": self.max_response_tokens,
                    "temperature": self.temperature,
                    "stream": True,
                }
                # The tools are left out of the last pass so the model has to answer.
                # Left OUT, not sent as null: OpenAI reads the two alike, but a
                # compatible server is free to reject `"tools": null` as a malformed
                # list, and an absent field means the same thing to all of them.
                if schemas and iteration < self.max_tool_calls:
                    request["tools"] = schemas
                if self.extra_body:
                    request["extra_body"] = self.extra_body
                response = await asyncio.wait_for(
                    self.client.chat.completions.create(**request),
                    timeout=self.openai_timeout,
                )

                # Collect streaming response
                tool_calls_buffer = []
                async for chunk in response:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if not delta:
                        continue

                    # Text content
                    if delta.content:
                        full_response += delta.content
                        yield {"type": "token", "content": delta.content}

                    # Tool calls (accumulated across chunks)
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            while len(tool_calls_buffer) <= tc_delta.index:
                                tool_calls_buffer.append({
                                    "id": "", "function": {"name": "", "arguments": ""},
                                })
                            tc_buf = tool_calls_buffer[tc_delta.index]
                            if tc_delta.id:
                                tc_buf["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tc_buf["function"]["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tc_buf["function"]["arguments"] += tc_delta.function.arguments

                # Check if GPT wants to call tools
                if tool_calls_buffer:
                    # Process each tool call
                    for tc in tool_calls_buffer:
                        func_name = tc["function"]["name"]
                        # Translated per frame, in the reader's language: see
                        # `tool_registry.TOOL_DISPLAY_NAMES`. Branch on `name`.
                        display = display_name(func_name)
                        yield {"type": "tool_used", "name": func_name, "display": display}

                        try:
                            args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {}

                        # Execute tool with timeout
                        result = await self._execute_tool_safely(registry, func_name, args)

                        # Truncate large results
                        truncated_result = await database_sync_to_async(
                            self.memory_service.truncate_tool_result
                        )(result)

                        tool_calls_made.append({
                            "name": func_name,
                            "arguments": args,
                            "result": truncated_result,
                        })

                        # Add to messages for next loop iteration
                        messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": func_name,
                                    "arguments": json.dumps(args),
                                },
                            }],
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(truncated_result, default=str),
                        })

                    # Reset full_response for next iteration (tool calls don't produce text)
                    full_response = ""
                    continue

                # No tool calls — we have the final response
                break

            except asyncio.TimeoutError:
                yield {
                    "type": "error",
                    "content": str(_("I'm taking longer than usual. Please try again in a moment.")),
                    "code": "timeout",
                }
                return
            except openai.RateLimitError:
                yield {
                    "type": "error",
                    "content": str(_("I'm experiencing high demand. Please try again in a few seconds.")),
                    "code": "rate_limit_openai",
                }
                return
            except openai.AuthenticationError:
                logger.critical("The AI provider's API key is invalid or expired")
                yield {
                    "type": "error",
                    "content": str(_("AI service is temporarily unavailable. Please contact support.")),
                    "code": "config_error",
                }
                return
            except openai.APIConnectionError:
                yield {
                    "type": "error",
                    "content": str(_("I'm temporarily unavailable. Please try again shortly.")),
                    "code": "connection",
                }
                return
            except Exception as e:
                logger.exception(f"AI provider API error: {e}")
                yield {
                    "type": "error",
                    "content": str(_("Something went wrong. Please try again.")),
                    "code": "server",
                }
                return

        # 8. Calculate metrics
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        response_tokens = self.memory_service.count_tokens(full_response)

        # 9. Save messages to DB
        msg_id = await self._save_messages(
            session, content, full_response, tool_calls_made,
        )

        # 10. Update session
        await self._update_session(session, content)

        # 11. Track cost
        estimated_cost = self.cost_tracker.estimate_cost(
            input_tokens=self.memory_service.count_tokens(
                system_prompt + content + json.dumps(tool_calls_made, default=str)
            ),
            output_tokens=response_tokens,
        )
        await database_sync_to_async(self.cost_tracker.record_usage)(
            user, response_tokens, estimated_cost,
        )
        await database_sync_to_async(self.cost_tracker.update_session_cost)(
            session, estimated_cost, response_tokens,
        )

        # 12. Log training data (non-blocking)
        try:
            await database_sync_to_async(self.data_collector.log_interaction)(
                user=user,
                session=session,
                # The FK the feedback path filters on. `log_interaction` grew this
                # parameter specifically to replace a fragile text-prefix join, and no
                # caller ever passed it — so every row had `message = NULL` and every
                # thumbs-up matched nothing. `_save_messages` returns the id.
                message_id=msg_id,
                user_message=content,
                ai_response=full_response,
                tools_called=[tc["name"] for tc in tool_calls_made],
                tool_results=[tc["result"] for tc in tool_calls_made],
                response_tokens=response_tokens,
                response_latency_ms=elapsed_ms,
            )
        except Exception as e:
            logger.error(f"Training data logging failed: {e}")

        # 13. Emit completion event
        tools_display = [
            {"name": tc["name"], "display": display_name(tc["name"])}
            for tc in tool_calls_made
        ]
        yield {
            "type": "complete",
            "session_id": str(session.session_id),
            "message_id": msg_id,
            "tools_used": tools_display,
            # Spec §9 B4: every AI coach reply carries the medical disclaimer, in the
            # reader's language. Additive.
            "medical_disclaimer": ai_coach_disclaimer(),
        }

    async def _store_safe_turn(self, user, content: str, session_id=None):
        """Answer a payment question on a store-safe build: the fixed reply, no model.

        Kept like any other turn — the question and the reply are saved to the chat
        (so the history, the title and 👍/👎 work), and the turn ends on `complete`, so
        the socket charges it like any answered message. Nothing is sent to the model,
        nothing is counted as cost, and no training copy is kept: the sentence is ours,
        not the model's. The same frames for every user of the build.
        """
        reply = store_safe_reply()
        session = await self._get_or_create_session(user, session_id)
        msg_id = await self._save_messages(session, content, reply, [])
        await self._update_session(session, content)
        yield {"type": "token", "content": reply}
        yield {
            "type": "complete",
            "session_id": str(session.session_id),
            "message_id": msg_id,
            "tools_used": [],
            "medical_disclaimer": ai_coach_disclaimer(),
        }

    def _iteration_status(self, iteration: int) -> dict:
        """The `status` frame for one pass of the tool-calling loop.

        Returns the whole frame, not a string: the `code` is what the app branches on
        and the translated `content` is what an older build displays. See
        `ITERATION_STATUSES` for why both are sent. Past the last entry the frame
        repeats, so a longer loop does not run off the end.
        """
        code, text = ITERATION_STATUSES[min(iteration, len(ITERATION_STATUSES) - 1)]
        return {"type": "status", "content": str(text), "code": code}

    async def _execute_tool_safely(self, registry, func_name: str, args: dict) -> dict:
        """Execute a tool with timeout, return error dict on failure."""
        try:
            result = await asyncio.wait_for(
                database_sync_to_async(registry.execute)(func_name, args),
                timeout=5.0,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Tool {func_name} timed out")
            return {"error": str(_("This data took too long to fetch."))}
        except Exception as e:
            logger.exception(f"Tool {func_name} failed: {e}")
            return {"error": str(_("Could not fetch this data right now."))}

    async def _get_or_create_session(self, user, session_id=None):
        """Get existing session by ID or create a new one."""
        from ai_assistant.models import ChatSession
        import uuid

        if session_id:
            try:
                session_uuid = uuid.UUID(session_id)
                session = await database_sync_to_async(
                    ChatSession.objects.get
                )(session_id=session_uuid, user=user, is_active=True)
                return session
            except (ChatSession.DoesNotExist, ValueError):
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

        # Create new session
        session = await database_sync_to_async(ChatSession.objects.create)(
            user=user,
        )
        return session

    async def _update_session(self, session, user_message: str):
        """Update session title and message count."""

        def _update():
            if not session.title:
                session.title = user_message[:100]
            session.total_messages += 1
            session.save(update_fields=['title', 'total_messages', 'updated_at'])

        await database_sync_to_async(_update)()

    async def _save_messages(self, session, user_message, ai_response, tool_calls):
        """Save user and assistant messages to the database."""
        from ai_assistant.models import ChatMessage

        # Save user message
        await database_sync_to_async(ChatMessage.objects.create)(
            session=session,
            role='user',
            content=user_message,
        )

        # Save assistant message
        tools_names = [tc["name"] for tc in tool_calls]
        tools_results = [tc.get("result", {}) for tc in tool_calls]

        msg = await database_sync_to_async(ChatMessage.objects.create)(
            session=session,
            role='assistant',
            content=ai_response,
            tool_calls=tools_names,
            tool_results=tools_results,
            tokens_used=self.memory_service.count_tokens(ai_response),
        )
        return msg.id

    async def _ensure_insights(self, user):
        """Cold start: run analyzers synchronously if no insights exist."""
        from ai_assistant.models import UserInsight

        has_insights = await database_sync_to_async(
            UserInsight.objects.filter(
                user=user,
                insight_type='training_pattern',
            ).exists
        )()

        if not has_insights:
            try:
                await self._compute_insights_for_user(user)
            except Exception as e:
                logger.warning(f"Cold start insight computation failed: {e}")

    async def _compute_insights_for_user(self, user):
        """Run all analyzers and cache results."""
        from ai_assistant.models import UserInsight
        from ai_assistant.analyzers.training_analyzer import TrainingAnalyzer
        from ai_assistant.analyzers.diet_analyzer import DietAnalyzer
        from ai_assistant.analyzers.behavior_profiler import BehaviorProfiler
        from datetime import timedelta
        from django.utils import timezone

        expires = timezone.now() + timedelta(hours=24)

        analyzers = [
            ('training_pattern', TrainingAnalyzer()),
            ('diet_pattern', DietAnalyzer()),
            ('behavior_profile', BehaviorProfiler()),
        ]

        for insight_type, analyzer in analyzers:
            try:
                result = await database_sync_to_async(analyzer.analyze)(user)
                await database_sync_to_async(UserInsight.objects.update_or_create)(
                    user=user,
                    insight_type=insight_type,
                    defaults={
                        'content': result,
                        'confidence': 0.7,
                        'expires_at': expires,
                    },
                )
            except Exception as e:
                logger.warning(f"Analyzer {insight_type} failed: {e}")
