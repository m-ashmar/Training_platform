"""The public pages' addresses, as the capabilities answer hands them to every build.

Spec B7: the privacy policy and the terms must be reachable from the app and from the
store listing in both languages, and their URLs travel through the capabilities endpoint
so they can move without a release. The account-deletion page rides along, because the
Play listing needs it and the app's own deletion screen may point at it.

The base is `PUBLIC_PAGES_BASE_URL` (the hub domain, e.g. `https://yallagym.com`, which
serves or forwards `/legal/...`) and, when that is unset, the host the request came to.
Nothing else enters: the same links for every channel, every version and every caller.
"""
from django.conf import settings
from django.urls import reverse

from .models import LANGUAGES

#: The key the app reads -> the route that serves it.
PAGES = {
    "privacy_policy": "legal:privacy",
    "terms_of_use": "legal:terms",
    "account_deletion": "legal:delete-account",
}


def _absolute(path: str, request) -> str:
    base = (getattr(settings, "PUBLIC_PAGES_BASE_URL", "") or "").strip().rstrip("/")
    if base:
        return base + path
    return request.build_absolute_uri(path)


def legal_links(request) -> dict:
    """`{"privacy_policy": {"ar": url, "en": url}, "terms_of_use": ..., ...}`."""
    return {
        key: {language.value: _absolute(f"{reverse(route)}?lang={language.value}", request)
              for language in LANGUAGES}
        for key, route in PAGES.items()
    }
