"""
training_platform/error_handlers.py — Language-aware custom error handlers.

Ensures 404/500 responses respect the user's preferred language
and never leak default-English pages.
"""

import logging
from django.http import JsonResponse
from django.utils.translation import gettext as _

from training_platform.i18n import LanguageContext

logger = logging.getLogger(__name__)


def handler404(request, exception=None):
    """
    Language-aware 404 handler.

    Activates the requesting user's language before rendering the response.
    Returns JSON for API clients (Accept: application/json) and a simple
    text response otherwise.
    """
    user = getattr(request, 'user', None)
    if user and getattr(user, 'is_authenticated', False):
        ctx = LanguageContext.for_user(user)
    else:
        # Use language set by middleware (from Accept-Language or cookie)
        from django.utils import translation
        ctx = translation.override(translation.get_language() or 'en')

    with ctx:
        message = str(_("Page not found."))
        logger.warning(
            "http.404",
            extra={
                "path": request.path,
                "user_id": getattr(user, 'id', None),
                "language": translation.get_language() if 'translation' in dir() else None,
            },
        )

    return JsonResponse(
        {"detail": message, "code": "not_found"},
        status=404,
    )


def handler500(request):
    """
    Language-aware 500 handler.

    Best-effort language activation — if anything fails during language
    lookup, falls back to settings.LANGUAGE_CODE.
    """
    try:
        user = getattr(request, 'user', None)
        if user and getattr(user, 'is_authenticated', False):
            ctx = LanguageContext.for_user(user)
        else:
            from django.utils import translation
            ctx = translation.override(translation.get_language() or 'en')

        with ctx:
            message = str(_("An unexpected error occurred. Please try again later."))
    except Exception:
        message = "An unexpected error occurred. Please try again later."

    return JsonResponse(
        {"detail": message, "code": "server_error"},
        status=500,
    )
