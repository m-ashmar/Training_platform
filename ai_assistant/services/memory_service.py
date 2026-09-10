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

#: The most messages the history budget can possibly consume, so the query
#: reads a bounded window instead of the whole session.
HISTORY_WINDOW = 200


class MemoryService:
    """Manages conversation memory with token-aware truncation."""

    def __init__(self):
        config = getattr(settings, 'AI_ASSISTANT_CONFIG', {})
        self.model = config.get('MODEL', 'gpt-4o-mini')
        self.history_budget = config.get('HISTORY_BUDGET', 3000)
        self.tool_result_budget = config.get('TOOL_RESULTS_BUDGET', 2000)
        self.tool_result_max_per_call = 500
        # NOT here. `tiktoken.encoding_for_model` downloads a 2.5 MB BPE vocabulary over
        # HTTPS when it is not already on disk, and Channels constructs a consumer on the
        # EVENT LOOP thread — so the first AI connection after a cold start blocked the
        # whole worker for 11.5 seconds, every other socket on it included, and the
        # health check has a 10 second timeout. tiktoken caches under
        # `tempfile.gettempdir()`, which is the ephemeral container filesystem, and
        # `auto_stop_machines` with `min_machines_running = 0` means that cost is paid on
        # every scale-from-zero, not once. If the vocabulary host is unreachable the
        # constructor raised and AI chat was 100% down while everything else looked
        # healthy.
        #
        # Deferred to first use, and falling back to a length estimate rather than
        # failing: a token count is a budgeting aid, not a correctness requirement.
        self._enc = None
        self._enc_failed = False

    @property
    def encoder(self):
        """The tokeniser, loaded on first use. None if it cannot be loaded."""
        if self._enc is None and not self._enc_failed:
            try:
                self._enc = tiktoken.encoding_for_model(self.model)
            except KeyError:
                try:
                    self._enc = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    self._enc_failed = True
            except Exception:
                # No egress to the vocabulary host, or a read-only cache dir.
                logger.warning("tiktoken unavailable; counting tokens by estimate",
                               exc_info=True)
                self._enc_failed = True
        return self._enc

    def count_tokens(self, text: str) -> int:
        """Count tokens in a string, or estimate when the tokeniser is unavailable."""
        encoder = self.encoder
        if encoder is None:
            # ~4 characters per token for English and Arabic alike. Wrong by a few
            # percent; a budget that is a few percent off is not a broken product, and a
            # chat that will not open is.
            return max(1, len(text or "") // 4)
        return len(encoder.encode(text))

    def get_truncated_history(self, session) -> list:
        """
        Load messages from a session, newest-first, until the
        history token budget is exhausted.

        Returns list of OpenAI-format messages [{"role": ..., "content": ...}]
        ordered oldest-first.
        """
        from ai_assistant.models import ChatMessage

        # Bounded. This had no LIMIT, so every message in the session was fetched to
        # keep about ninety of them: a 2,000-message session read 2,000 rows on every
        # turn. Real sessions stay near a hundred because `close_idle_sessions` runs at
        # 30 minutes and the daily cap is 50 turns, so the window below is comfortably
        # more than the budget can ever use.
        messages_qs = ChatMessage.objects.filter(
            session=session,
        ).order_by('-created_at')[:HISTORY_WINDOW]

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
