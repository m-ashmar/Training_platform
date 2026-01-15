"""
Custom exception hierarchy for the Diet app and AI services.
Provides meaningful error classes for API, parsing, persistence, and constraint failures
so that callers can differentiate transient issues from permanent ones and log accordingly.
"""

from __future__ import annotations


class DietError(Exception):
    """Base class for all Diet-related errors."""


class OpenAIError(DietError):
    """Failures when calling or handling responses from OpenAI or LLM providers."""


class DietParsingError(DietError):
    """Failures when parsing AI output into structured models."""


class PersistenceError(DietError):
    """General database persistence failures when saving diet plans/meals/components."""


class ConstraintViolationError(PersistenceError):
    """Raised for DB constraint violations (unique, not-null, FK integrity)."""


class HTTPTransientError(DietError):
    """HTTP errors that are likely transient and should be retried (e.g., 429, 5xx, timeouts)."""


class HTTPPermanentError(DietError):
    """HTTP errors that are permanent (e.g., 4xx other than 429) and should not be retried."""


