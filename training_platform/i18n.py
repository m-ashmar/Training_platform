"""
training_platform/i18n.py — Language as a first-class system dimension.

Provides:
  - LanguageContext: unified language activation for all process boundaries
  - CACHE_VERSION: global cache key version for invalidation on schema changes

Usage:
    # HTTP views (delivery-time, user object available):
    with LanguageContext.for_user(user):
        title = str(_("Some title"))

    # Celery tasks (delivery-time, user_id only):
    with LanguageContext.for_user_id(user_id):
        send_notification(...)

    # Cache keys:
    key = LanguageContext.cache_key("trainer", user_id, "clients")
    # → "trainer:42:clients:ar:v1"

    # RTL direction:
    dir_attr = LanguageContext.direction()  # 'rtl' or 'ltr'
"""

import logging

from django.conf import settings
from django.utils import translation
from django.utils.translation import get_language_bidi

logger = logging.getLogger(__name__)

# Bump this when serializer structure or translated fields change.
# All language-partitioned cache keys include this version.
CACHE_VERSION = "v1"

# Pre-compute valid language codes once at module load
_VALID_LANGUAGES = None


def _get_valid_languages():
    """Lazily load valid language codes from settings.LANGUAGES."""
    global _VALID_LANGUAGES
    if _VALID_LANGUAGES is None:
        _VALID_LANGUAGES = frozenset(
            code for code, _name in getattr(settings, 'LANGUAGES', [('en', 'English')])
        )
    return _VALID_LANGUAGES


def _validate_language(lang, *, user_id=None):
    """
    Validate a language code against settings.LANGUAGES.

    Returns the validated language or settings.LANGUAGE_CODE on failure.
    Logs a structured warning on invalid values.
    """
    if not lang:
        return settings.LANGUAGE_CODE

    valid = _get_valid_languages()
    if lang in valid:
        return lang

    logger.warning(
        "i18n.language.invalid",
        extra={
            "user_id": user_id,
            "invalid_language_value": lang,
            "fallback_language": settings.LANGUAGE_CODE,
            "valid_languages": list(valid),
        },
    )

    # Emit metric if available
    try:
        from notifications.metrics import language_fallback_total
        language_fallback_total.labels(
            invalid_value=str(lang)[:10],
        ).inc()
    except Exception:
        pass

    return settings.LANGUAGE_CODE


class LanguageContext:
    """
    Centralised language activation for every process boundary.

    Replaces scattered translation.override() / translation.activate()
    calls with a single, documented abstraction.
    """

    @staticmethod
    def for_user(user):
        """
        Context manager that activates the user's preferred language.

        Safe for both sync and async (context-manager scoped, not
        threadlocal-persistent).

        Validates language against settings.LANGUAGES. Invalid or missing
        values fall back to settings.LANGUAGE_CODE with a structured warning.

        Usage:
            with LanguageContext.for_user(request.user):
                ...
        """
        raw_lang = getattr(user, 'preferred_language', None)
        lang = _validate_language(
            raw_lang,
            user_id=getattr(user, 'id', None),
        )
        return translation.override(lang)

    @staticmethod
    def for_user_id(user_id):
        """
        Delivery-time language resolution.

        Fetches the user's *current* preferred_language from the DB,
        ensuring that language changes between event emission and
        delivery are respected.

        Validates the stored value against settings.LANGUAGES.

        Usage (inside Celery worker / FCM channel):
            with LanguageContext.for_user_id(recipient.id):
                title = str(template.title)
        """
        from django.contrib.auth import get_user_model
        User = get_user_model()
        raw_lang = (
            User.objects
            .filter(id=user_id)
            .values_list('preferred_language', flat=True)
            .first()
        )
        lang = _validate_language(raw_lang, user_id=user_id)
        return translation.override(lang)

    @staticmethod
    def direction():
        """
        Returns 'rtl' or 'ltr' based on the currently active language.

        Uses Django's built-in bidi detection — future-proof for
        Hebrew, Persian, Urdu, etc.
        """
        return 'rtl' if get_language_bidi() else 'ltr'

    @staticmethod
    def cache_key(*parts):
        """
        Build a language-partitioned, versioned cache key.

        Example:
            LanguageContext.cache_key("trainer", 42, "clients")
            → "trainer:42:clients:ar:v1"
        """
        lang = translation.get_language() or settings.LANGUAGE_CODE
        segments = [str(p) for p in parts]
        segments.extend([lang, CACHE_VERSION])
        return ":".join(segments)


class LanguageAwareAPIView:
    """
    DRF mixin that activates the authenticated user's preferred language
    after DRF authentication but before serialization.

    Guarantees that django-modeltranslation fields resolve in the
    correct language for API responses.

    Deactivates language in finalize_response() to prevent leaking
    to the next request on the same thread.

    Usage:
        class MyView(LanguageAwareAPIView, APIView):
            ...

        class MyViewSet(LanguageAwareAPIView, ModelViewSet):
            ...
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        user = getattr(request, 'user', None)
        if user and getattr(user, 'is_authenticated', False):
            raw_lang = getattr(user, 'preferred_language', None)
            lang = _validate_language(
                raw_lang,
                user_id=getattr(user, 'id', None),
            )
            if lang:
                translation.activate(lang)

    def finalize_response(self, request, response, *args, **kwargs):
        """Deactivate language to prevent threadlocal leaking between requests."""
        response = super().finalize_response(request, response, *args, **kwargs)
        translation.deactivate()
        return response
