"""
security.py — Input sanitization and prompt injection defense.

Validates and cleans user input before it reaches the LLM.
"""

import re
import logging

logger = logging.getLogger(__name__)


class InputSanitizer:
    """Detect and neutralize prompt injection attempts in user messages."""

    # Single source of truth for message length. The consumer used its own larger cap,
    # so a 3000-character message was accepted and then quietly cut to 2000 here.
    MAX_LENGTH = 2000

    # Patterns that indicate prompt injection or jailbreak attempts
    BLOCKED_PATTERNS = [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"you\s+are\s+now\s+DAN",
        r"system\s*prompt",
        r"reveal\s+(your|the)\s+(instructions|prompt|rules)",
        r"act\s+as\s+if",
        r"pretend\s+(you|to\s+be)",
        r"disregard\s+(all|your|previous)",
        r"override\s+(your|system)",
        r"new\s+instructions?\s*:",
        r"from\s+now\s+on\s+(you|ignore|act)",
    ]

    _compiled = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]

    def sanitize(self, text: str, max_length: int | None = None) -> tuple:
        """
        Sanitize user input.

        Returns:
            (cleaned_text, is_flagged) tuple.
            If flagged, the message should still be sent to GPT but with
            a warning in the system prompt so GPT can refuse gracefully.
        """
        # Enforce length limit
        text = text[:max_length or self.MAX_LENGTH].strip()

        if not text:
            return text, False

        # Check for injection patterns
        flagged = False
        for pattern in self._compiled:
            if pattern.search(text):
                flagged = True
                logger.warning(f"Prompt injection attempt detected: {pattern.pattern}")
                break

        return text, flagged


    def sanitize_context_value(self, value, max_length: int = 300) -> tuple:
        """Neutralise user-controlled profile text destined for the SYSTEM prompt.

        `sanitize()` guards the chat message, but ContextCompiler interpolates profile
        fields — name, goals, injury — straight into the system prompt, and those are
        writable via /api/auth/user/update/. Text in the system role carries far more
        weight with the model than anything in a user turn, so the same payload the
        sanitizer blocks in a message was landing in the most trusted position.

        Returns (cleaned, was_flagged).
        """
        if value is None:
            return "", False
        text = str(value)[:max_length]
        flagged = any(p.search(text) for p in self._compiled)
        # Strip the structural characters used to fake prompt turns or headings.
        text = re.sub(r"[\r\n]+", " ", text)
        text = re.sub(r"(?i)\b(system|assistant|user)\s*:", r"\1-", text)
        text = text.replace("```", "").replace("#", "").strip()
        if flagged:
            logger.warning("Injection markers in profile-supplied context; neutralised.")
            return "[content removed: disallowed instructions]", True
        return text, flagged
