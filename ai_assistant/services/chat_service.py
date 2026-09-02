"""
chat_service.py — Core GPT orchestration with function calling.

Manages the full chat loop: context compilation → GPT call → tool execution →
streaming response. Designed for async usage from the WebSocket consumer.
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

from ai_assistant.tool_registry import ToolRegistry, TOOL_DISPLAY_NAMES
from ai_assistant.services.context_compiler import ContextCompiler
from ai_assistant.services.memory_service import MemoryService
from ai_assistant.services.security import InputSanitizer
from ai_assistant.services.data_collector import DataCollector
from ai_assistant.services.cost_tracker import CostTracker

logger = logging.getLogger(__name__)


class ChatService:
    """
    Orchestrates GPT chat with function calling and streaming.

    Usage (from async consumer):
        service = ChatService()
        async for event in service.chat_stream(user, "How is my squat?", session_id):
            await websocket.send(json.dumps(event))
    """

    def __init__(self):
        config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
        self.model = config.get('MODEL', 'gpt-4o-mini')
        self.max_response_tokens = config.get('MAX_RESPONSE_TOKENS', 2000)
        self.temperature = config.get('TEMPERATURE', 0.7)
        self.max_tool_calls = config.get('MAX_TOOL_CALLS_PER_TURN', 5)
        self.openai_timeout = 30

        self.api_key = getattr(settings, 'OPENAI_API_KEY', '')
        self.client = openai.AsyncOpenAI(api_key=self.api_key)

        self.context_compiler = ContextCompiler()
        self.memory_service = MemoryService()
        self.sanitizer = InputSanitizer()
        self.data_collector = DataCollector()
        self.cost_tracker = CostTracker()

    async def chat_stream(self, user, message: str, session_id=None):
        """
        Async generator that yields WebSocket events for a chat turn.

        Events yielded:
          {"type": "status", "content": "..."}
          {"type": "tool_used", "name": "...", "display": "..."}
          {"type": "token", "content": "partial text"}
          {"type": "complete", "session_id": "...", "message_id": ..., "tools_used": [...]}
          {"type": "error", "content": "...", "code": "..."}
        """
        start_time = time.monotonic()

        # 1. Sanitize input
        content, flagged = self.sanitizer.sanitize(message)
        if not content:
            yield {"type": "error", "content": str(_("Message cannot be empty.")), "code": "invalid_input"}
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

        # 3. Ensure insights exist (cold start)
        yield {"type": "status", "content": str(_("Getting ready..."))}
        await self._ensure_insights(user)

        # 3. Get or create session
        session = await self._get_or_create_session(user, session_id)

        # 4. Build system prompt
        system_prompt = await database_sync_to_async(
            self.context_compiler.compile
        )(user, flagged_input=flagged)
        
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
                yield {"type": "status", "content": self._iteration_status(iteration)}

                response = await asyncio.wait_for(
                    self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        tools=schemas if iteration < self.max_tool_calls else None,
                        max_tokens=self.max_response_tokens,
                        temperature=self.temperature,
                        stream=True,
                    ),
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
                        display = TOOL_DISPLAY_NAMES.get(func_name, func_name)
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
                    "content": "I'm taking longer than usual. Please try again in a moment.",
                    "code": "timeout",
                }
                return
            except openai.RateLimitError:
                yield {
                    "type": "error",
                    "content": "I'm experiencing high demand. Please try again in a few seconds.",
                    "code": "rate_limit_openai",
                }
                return
            except openai.AuthenticationError:
                logger.critical("OpenAI API key is invalid or expired")
                yield {
                    "type": "error",
                    "content": "AI service is temporarily unavailable. Please contact support.",
                    "code": "config_error",
                }
                return
            except openai.APIConnectionError:
                yield {
                    "type": "error",
                    "content": "I'm temporarily unavailable. Please try again shortly.",
                    "code": "connection",
                }
                return
            except Exception as e:
                logger.exception(f"OpenAI API error: {e}")
                yield {
                    "type": "error",
                    "content": "Something went wrong. Please try again.",
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
            {"name": tc["name"], "display": TOOL_DISPLAY_NAMES.get(tc["name"], tc["name"])}
            for tc in tool_calls_made
        ]
        yield {
            "type": "complete",
            "session_id": str(session.session_id),
            "message_id": msg_id,
            "tools_used": tools_display,
        }

    def _iteration_status(self, iteration: int) -> str:
        """Status messages for each iteration of the tool calling loop."""
        statuses = [
            "Thinking...",
            "Checking your data...",
            "Analyzing details...",
            "Looking deeper...",
            "Almost done...",
        ]
        return statuses[min(iteration, len(statuses) - 1)]

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
