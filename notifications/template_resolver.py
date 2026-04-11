"""
notifications/template_resolver.py — Context-validated notification template resolution.

Resolves NotificationTemplate objects into translated title/body strings
inside a LanguageContext. Provides structured logging and prometheus metrics
for missing templates or invalid context.

This is the ONLY place where lazy translation strings are evaluated for
notifications. Never call str() on a notification template outside this module.
"""

import logging

from django.utils import translation
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


class NotificationTemplateError(Exception):
    """Raised when a notification event has no template bound."""
    pass


class NotificationContextError(Exception):
    """Raised when template context is missing required keys."""
    pass


def _inc_metric(counter_name, **labels):
    """Safely increment a prometheus counter (no-op if unavailable)."""
    try:
        from notifications import metrics
        getattr(metrics, counter_name).labels(**labels).inc()
    except Exception:
        pass


class NotificationTemplateResolver:
    """
    Resolves event templates into translated strings with context validation.

    Usage (inside a LanguageContext block):
        with LanguageContext.for_user_id(recipient_id):
            title, body = NotificationTemplateResolver.render(
                event_type="post_liked",
                template=PostLikedEvent.template,
                context={"actor": "Ahmed"},
                recipient_id=recipient_id,
            )
    """

    @staticmethod
    def render(event_type: str, template, context: dict, recipient_id=None) -> tuple:
        """
        Render a NotificationTemplate with the given context.

        Args:
            event_type: The event type string (for logging).
            template: A NotificationTemplate instance (with lazy title/body).
            context: Dict of interpolation values for %(key)s placeholders.
            recipient_id: Optional recipient ID for structured logging.

        Returns:
            (title: str, body: str)

        On failure, returns a safe user-facing fallback — never exposes
        raw template placeholders like %(actor)s.
        """
        lang = translation.get_language()
        safe_fallback_body = str(_("You have a new notification."))

        if not template:
            logger.error(
                "notification.template.missing",
                extra={
                    "event_type": event_type,
                    "recipient_id": recipient_id,
                    "language": lang,
                },
            )
            _inc_metric(
                "notification_template_missing_total",
                event_type=event_type,
            )
            # Graceful degradation — don't crash the worker
            return str(_("Notification")), safe_fallback_body

        try:
            title = str(template.title) % context if context else str(template.title)
            body = str(template.body) % context if context else str(template.body)
            return title, body
        except KeyError as e:
            logger.error(
                "notification.context.invalid",
                extra={
                    "event_type": event_type,
                    "missing_key": str(e),
                    "provided_context_keys": list(context.keys()) if context else [],
                    "recipient_id": recipient_id,
                    "language": lang,
                },
            )
            _inc_metric(
                "notification_context_error_total",
                event_type=event_type,
                error_kind="missing_key",
            )
            # Safe fallback — never expose %(actor)s to user
            try:
                return str(template.title), safe_fallback_body
            except Exception:
                return str(_("Notification")), safe_fallback_body
        except TypeError as e:
            logger.error(
                "notification.context.type_error",
                extra={
                    "event_type": event_type,
                    "error": str(e),
                    "recipient_id": recipient_id,
                    "language": lang,
                },
            )
            _inc_metric(
                "notification_context_error_total",
                event_type=event_type,
                error_kind="type_error",
            )
            return str(_("Notification")), safe_fallback_body
