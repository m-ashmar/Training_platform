"""The computer's half of signing in to the console from the phone (BACKEND_BUGS B-120).

`/dj-admin/qr-login/<secret>/` is the link the phone shows. GET is a page with one button
and changes nothing; POST (the button, CSRF-protected) claims the link for this browser
and shows the number to type on the phone; the page then asks
`/dj-admin/qr-login/<secret>/status/` every two seconds until the phone approved it,
and the answer that finds it approved signs this browser in. `console_login.py` has the
flow and why each step is there.

Operator pages, so plain English like the rest of the console (no `LocaleMiddleware`
serves it). Every answer is `no-store` and sends no referrer: the secret is in the path.
While `OPS_QR_LOGIN_ENABLED` is off, every route here is a 404 that says
`console_login_disabled`.
"""
import hashlib
import logging

from django.conf import settings
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import login
from django.contrib.contenttypes.models import ContentType
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from training_platform.middleware import get_trusted_client_ip

from . import console_login
from .models import ConsoleLogin

logger = logging.getLogger(__name__)

TEMPLATE = "admin_dashboard/qr_login.html"


def _private(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _page(request, state, status=200, **context):
    return _private(render(request, TEMPLATE, {"state": state, **context}, status=status))


def _closed_page(request):
    return _page(request, "closed", status=410)


@require_http_methods(["GET", "POST"])
def qr_login_page(request, secret):
    if not console_login.enabled():
        return _page(request, "disabled", status=404)
    if request.method == "GET":
        ticket = console_login.find(secret)
        if ticket is None:
            return _page(request, "not_found", status=404)
        if console_login.state_of(ticket) != ConsoleLogin.State.ISSUED:
            return _closed_page(request)
        return _page(request, "start")

    ip = get_trusted_client_ip(request)
    try:
        console_login.throttle(
            f"rl:console_login:claim:{hashlib.sha256(str(ip).encode()).hexdigest()[:32]}",
            console_login.CLAIM_LIMIT, console_login.CLAIM_WINDOW,
            "Too many sign-in links opened from this address. Wait a few minutes.")
        ticket, browser_secret = console_login.claim(
            secret, ip, request.META.get("HTTP_USER_AGENT", ""))
    except console_login.Refusal as refusal:
        if refusal.status == 404:
            return _page(request, "not_found", status=404)
        if refusal.status == 429:
            return _page(request, "throttled", status=429)
        return _closed_page(request)
    response = _page(request, "claimed", match_code=ticket.match_code,
                     status_url=reverse("ops_qr_login_status", args=[secret]),
                     seconds=console_login.seconds_left(ticket))
    response.set_cookie(
        console_login.COOKIE, browser_secret, max_age=console_login.LIFETIME_SECONDS,
        path=console_login.COOKIE_PATH, httponly=True, samesite="Strict",
        secure=request.is_secure() or getattr(settings, "SESSION_COOKIE_SECURE", False))
    return response


@require_POST
def qr_login_status(request, secret):
    """JSON `{state}` — `waiting`, `signed_in` (with `next`), `expired`, `cancelled`,
    `used`. 403 `console_login_wrong_browser` to any browser but the one that opened
    the link; 404 `console_login_not_found`, or `console_login_disabled` while off."""
    if not console_login.enabled():
        return _private(JsonResponse(
            {"error": "Not found.", "detail": "Not found.",
             "code": "console_login_disabled"}, status=404))
    try:
        answer, ticket = console_login.complete(
            secret, request.COOKIES.get(console_login.COOKIE, ""))
    except console_login.Refusal as refusal:
        return _private(JsonResponse(
            {"error": refusal.message, "detail": refusal.message, "code": refusal.code},
            status=refusal.status))
    if answer != "signed_in":
        return _private(JsonResponse({"state": answer}))

    user = ticket.user
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    try:
        LogEntry.objects.log_action(
            user_id=user.pk,
            content_type_id=ContentType.objects.get_for_model(ConsoleLogin).pk,
            object_id=str(ticket.pk), object_repr=str(ticket), action_flag=CHANGE,
            change_message="Signed in to the console on a computer with a link approved "
                           "on the phone")
    except Exception:
        logger.warning("console login %s: could not write the admin log", ticket.pk,
                       exc_info=True)
    response = JsonResponse({"state": "signed_in",
                             "next": reverse("training_admin:index")})
    response.delete_cookie(console_login.COOKIE, path=console_login.COOKIE_PATH,
                           samesite="Strict")
    return _private(response)
