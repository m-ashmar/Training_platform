"""The REST face of the AI-consent guard.

A permission class rather than a check inside each view, so the guard is declared once
(`ai_assistant/views.py` `AI_PERMISSIONS`) and cannot be forgotten on a view added
later. `code` and `message` are what DRF puts in the 403 body — the same mechanism
`subscription/permissions.py` uses for `no_entitlement`.
"""
from rest_framework import permissions

from .exceptions import AIConsentRequired
from .services import has_ai_consent


class HasAIConsent(permissions.BasePermission):
    """Refuse the AI coach's surface until the person has allowed it to read their data.

    Ordered AFTER the entitlement permission wherever both apply: a lapsed subscriber
    who has also never consented must still be told `no_entitlement`, because that is
    the one they can act on.
    """

    code = AIConsentRequired.default_code
    message = AIConsentRequired.default_detail

    def has_permission(self, request, view):
        return has_ai_consent(request.user)
