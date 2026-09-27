"""The public pages: the privacy policy, the terms of use and the account-deletion page.

HTML, not the JSON API: they are opened in a browser, from the app, from the store
listing and from the hub site. No authentication runs and nothing on them depends on
who is looking — a visitor and a signed-in user get the same page.

**`GET /legal/privacy/`, `GET /legal/terms/`** — `?lang=ar|en`, Arabic when absent or
unknown (Arabic is the primary language), right-to-left in Arabic. The newest published
version of the document in that language (`LegalDocument.current`); when that language
has none yet, the other language's, with a line saying so; when neither has one, 404.
Cacheable by anyone for five minutes (`Cache-Control: public, max-age=300`), no cookie,
no validators — see `_cacheable` for why.

**`GET, POST /legal/delete-account/`** — how to delete an account in the app, and a form
for someone who cannot open it. A POST (`email`, optional `reason`, `lang`) queues an
`AccountDeletionRequest` for an operator and redirects (303) to `?lang=..&sent=1`, which
thanks the requester. It deletes nothing, and it answers exactly the same whether or not
an account uses the address, whether the address already has an open request, and
whether the request was dropped as a bot's (the hidden `website` field). Refusals are
the page again, with a sentence: 400 for an address that is not one or a reason over
1000 characters, 429 (with `Retry-After`) past `REQUESTS_PER_HOUR` posts from one
network in an hour. Never cached: the form carries a CSRF token.
"""
import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from training_platform.cache import ratelimit_cache, seconds_left
from training_platform.middleware import get_trusted_client_ip

from .models import (
    LANGUAGES, REASON_MAX_LENGTH, AccountDeletionRequest, DeletionRequestStatus,
    DocumentKind, LegalDocument,
)
from .rendering import render_body

logger = logging.getLogger(__name__)

#: How long anyone may keep a legal page. A new version reaches everyone within this.
DOCUMENT_CACHE_SECONDS = 300
#: Deletion requests one network may post in `REQUEST_WINDOW_SECONDS`.
REQUESTS_PER_HOUR = 5
REQUEST_WINDOW_SECONDS = 3600
EMAIL_MAX_LENGTH = 254

_CODES = tuple(language.value for language in LANGUAGES)


def _language(request) -> str:
    raw = (request.POST.get("lang") if request.method == "POST" else None) \
        or request.GET.get("lang") or ""
    raw = raw.strip().lower()
    return raw if raw in _CODES else LANGUAGES[0].value


def _direction(language: str) -> str:
    return "rtl" if language == "ar" else "ltr"


def _speak(language: str) -> None:
    """The page's language for the rest of this request — activated, not overridden, so
    `LanguageResolutionMiddleware` answers it as `Content-Language` too. It re-activates
    per request, so nothing carries over to the next one."""
    translation.activate(language)


def _chrome(request, language: str, page: str) -> dict:
    """What every page shares: language, direction, the links between the pages."""
    other = "en" if language == "ar" else "ar"
    return {
        "lang": language,
        "dir": _direction(language),
        "page": page,
        "other_lang": other,
        "switch_url": f"{request.path}?lang={other}",
        "privacy_url": f"{reverse('legal:privacy')}?lang={language}",
        "terms_url": f"{reverse('legal:terms')}?lang={language}",
        "delete_url": f"{reverse('legal:delete-account')}?lang={language}",
        "platform_name": getattr(settings, "PLATFORM_NAME", "") or _("Yalla Gym"),
        "contact_email": getattr(settings, "PLATFORM_CONTACT_EMAIL", ""),
    }


def _cacheable(response, seconds: int):
    """Public, for `seconds`, and nothing a shared cache could hand to the wrong person.

    No ETag and no Last-Modified, on purpose. The page's inline style carries the
    request's CSP nonce, and a 304 lets a browser merge the NEW response's headers —
    a new nonce — into the stored page, whose style would then be blocked. Without a
    validator the page is simply fetched whole once it is stale.
    """
    response["Cache-Control"] = f"public, max-age={seconds}"
    return response


# ------------------------------------------------------------------ legal documents
def _document_page(request, kind: str):
    language = _language(request)
    document = LegalDocument.current(kind, language)
    fallback = False
    if document is None:
        other = "en" if language == "ar" else "ar"
        document = LegalDocument.current(kind, other)
        fallback = document is not None

    _speak(language)
    context = _chrome(request, language, kind)
    context["heading"] = (_("Privacy policy") if kind == DocumentKind.PRIVACY
                          else _("Terms of use"))
    if document is None:
        return _cacheable(render(request, "legal/missing.html", context, status=404), 60)
    context.update({
        "document": document,
        "body": render_body(document.body),
        "document_lang": document.language,
        "document_dir": _direction(document.language),
        "fallback": fallback,
        "updated": (document.published_at or document.created_at).date().isoformat(),
    })
    return _cacheable(render(request, "legal/document.html", context),
                      DOCUMENT_CACHE_SECONDS)


@require_safe
def privacy_page(request):
    return _document_page(request, DocumentKind.PRIVACY)


@require_safe
def terms_page(request):
    return _document_page(request, DocumentKind.TERMS)


# ------------------------------------------------------------- account deletion
def _deletion_page(request, language, *, status=200, sent=False, error="", email="",
                   reason="", headers=None):
    _speak(language)
    context = _chrome(request, language, "delete")
    context.update({"heading": _("Delete your account"), "sent": sent,
                    "error": error, "email": email, "reason": reason,
                    "reason_max": REASON_MAX_LENGTH})
    response = render(request, "legal/delete_account.html", context, status=status)
    for name, value in (headers or {}).items():
        response[name] = value
    return response


def _throttled(request) -> int:
    """Count this post against its network; the seconds to wait when over, else 0."""
    key = f"rl:account_deletion:ip:{get_trusted_client_ip(request)}"
    cache = ratelimit_cache()
    try:
        if (cache.get(key) or 0) >= REQUESTS_PER_HOUR:
            return max(1, seconds_left(cache, key, REQUEST_WINDOW_SECONDS))
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, REQUEST_WINDOW_SECONDS)
    except Exception:
        # Fail open, as the platform's own limiter does: a cache outage must not take
        # the one page a store requires down with it.
        logger.warning("account deletion: rate limit unavailable", exc_info=True)
    return 0


def _queue(email: str, reason: str) -> None:
    """One open request per address: a second one while the first waits adds nothing."""
    if AccountDeletionRequest.objects.filter(
            email=email, status=DeletionRequestStatus.OPEN).exists():
        return
    try:
        with transaction.atomic():
            row = AccountDeletionRequest.objects.create(email=email, reason=reason)
    except IntegrityError:
        # Two posts for one address at the same instant: the database kept one.
        return
    logger.info("account deletion request %s queued", row.pk)


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
def delete_account_page(request):
    language = _language(request)
    _speak(language)
    if request.method != "POST":
        return _deletion_page(request, language, sent=request.GET.get("sent") == "1")

    wait = _throttled(request)
    if wait:
        message = _("Too many requests from this network. Please try again in an hour.")
        return _deletion_page(request, language, status=429, error=message,
                              headers={"Retry-After": str(wait)})

    done = HttpResponseRedirect(f"{reverse('legal:delete-account')}?lang={language}&sent=1")
    done.status_code = 303
    if (request.POST.get("website") or "").strip():
        # The field people never see. Thanked like anyone else, and not queued.
        return done

    email = (request.POST.get("email") or "").strip().lower()
    reason = (request.POST.get("reason") or "").strip()
    error = ""
    try:
        if not email or len(email) > EMAIL_MAX_LENGTH:
            raise ValidationError("bad address")
        EmailValidator()(email)
    except ValidationError:
        error = _("Please enter a valid email address.")
    if not error and len(reason) > REASON_MAX_LENGTH:
        error = _("The reason is too long. Keep it under 1000 characters.")
    if error:
        return _deletion_page(request, language, status=400, error=error,
                              email=email[:EMAIL_MAX_LENGTH], reason=reason[:REASON_MAX_LENGTH])

    _queue(email, reason)
    return done
