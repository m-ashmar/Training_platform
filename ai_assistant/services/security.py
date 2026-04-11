"""
security.py — Input sanitization and prompt injection defense.

Validates and cleans user input before it reaches the LLM.
"""

import re
import logging

logger = logging.getLogger(__name__)


class InputSanitizer:
    """Detect and neutralize prompt injection attempts in user messages."""

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

    def sanitize(self, text: str, max_length: int = 2000) -> tuple:
        """
        Sanitize user input.

        Returns:
            (cleaned_text, is_flagged) tuple.
            If flagged, the message should still be sent to GPT but with
            a warning in the system prompt so GPT can refuse gracefully.
        """
        # Enforce length limit
        text = text[:max_length].strip()

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
