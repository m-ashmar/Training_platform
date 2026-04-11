"""
memory_service.py — Token-aware conversation history management.

Manages loading, truncating, and summarizing conversation history
to stay within the model's context window budget.
"""

import json
import logging
import tiktoken
from django.conf import settings

logger = logging.getLogger(__name__)


class MemoryService:
    """Manages conversation memory with token-aware truncation."""

    def __init__(self):
        config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
        self.model = config.get('MODEL', 'gpt-4o-mini')
        self.history_budget = config.get('HISTORY_BUDGET', 3000)
        self.tool_result_budget = config.get('TOOL_RESULTS_BUDGET', 2000)
        self.tool_result_max_per_call = 500
        try:
            self._enc = tiktoken.encoding_for_model(self.model)
        except KeyError:
            self._enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, text: str) -> int:
        """Count tokens in a string."""
        return len(self._enc.encode(text))

    def get_truncated_history(self, session) -> list:
        """
        Load messages from a session, newest-first, until the
        history token budget is exhausted.

        Returns list of OpenAI-format messages [{"role": ..., "content": ...}]
        ordered oldest-first.
        """
        from ai_assistant.models import ChatMessage

        messages_qs = ChatMessage.objects.filter(
            session=session,
        ).order_by('-created_at')

        result = []
        tokens_used = 0

        for msg in messages_qs:
            msg_tokens = self.count_tokens(msg.content)
            if tokens_used + msg_tokens > self.history_budget:
                break
            result.append({
                "role": msg.role,
                "content": msg.content,
            })
            tokens_used += msg_tokens

        # Reverse to get chronological order
        result.reverse()
        return result

    def truncate_tool_result(self, result: dict) -> dict:
        """
        Truncate a tool result to fit within the per-call token budget.
        """
        result_str = json.dumps(result, default=str)
        tokens = self._enc.encode(result_str)

        if len(tokens) <= self.tool_result_max_per_call:
            return result

        # Truncate and try to return valid JSON
        truncated_tokens = tokens[:self.tool_result_max_per_call]
        truncated_str = self._enc.decode(truncated_tokens)

        # Try to make it valid JSON by wrapping
        return {"data": truncated_str, "_truncated": True}

    def generate_summary(self, session) -> str:
        """
        Generate a compressed summary of a session's conversation.
        Used when session times out to create long-term memory.
        """
        from ai_assistant.models import ChatMessage

        messages = ChatMessage.objects.filter(
            session=session, role__in=['user', 'assistant'],
        ).order_by('created_at')

        if not messages.exists():
            return ""

        # Simple extractive summary: first and last user messages + topics
        user_msgs = [m.content for m in messages if m.role == 'user']
        assistant_msgs = [m.content for m in messages if m.role == 'assistant']

        summary_parts = []
        if user_msgs:
            summary_parts.append(f"User asked about: {user_msgs[0][:100]}")
            if len(user_msgs) > 1:
                summary_parts.append(f"Also discussed: {user_msgs[-1][:100]}")
        summary_parts.append(f"Total exchanges: {len(user_msgs)}")

        # Extract tool references from assistant messages for topic detection
        tools_mentioned = set()
        for msg in messages:
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    if isinstance(tc, dict) and 'name' in tc:
                        tools_mentioned.add(tc['name'])
        if tools_mentioned:
            summary_parts.append(f"Data accessed: {', '.join(tools_mentioned)}")

        return ". ".join(summary_parts)
