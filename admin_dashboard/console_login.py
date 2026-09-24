"""Signing in to the web console from the operator's phone (BACKEND_BUGS B-120).

**Off unless the owner turns it on.** A new way into the console — the page that
approves transfers, pays trainers and reads identity documents — is behind
`settings.OPS_QR_LOGIN_ENABLED`, False by default. While it is off every route of it
answers `console_login_disabled` and nothing is written.

**What the design draws.** `16-agent-operator/OperatorPhone` shows the QR ON THE PHONE
("افتح على الحاسوب — امسح لتسجيل الدخول بلا كلمة مرور. الرمز صالح دقيقتين.") and a
"send the link to my email" button: the phone hands the computer a link. A link that
signs whoever opens it in is a bearer password for two minutes — forwarded mail, a
photographed screen, a link scanner in the mail client, a browser history synced to
another device — and CLAUDE.md forbids a way round the sign-in. So the link is one
half. The other half is the phone that made it:

1. The PHONE — signed in, a console operator (`operators.is_console_operator`), its
   address verified by the OTP flow — asks for a link (`issue`): a 256-bit secret, kept
   here only as its sha256, two minutes, one per operator (a new one cancels the old).
2. A COMPUTER opens it. The page changes nothing until "Continue" is pressed (a mail
   scanner that follows links must not spend it); Continue CLAIMS the link (`claim`):
   it is bound to THAT browser — a second secret, in an HttpOnly, SameSite=Strict cookie
   scoped to the sign-in page, its sha256 on the row — and the page shows a three-digit
   number. No other computer can open the link any more.
3. The PHONE, which watches its link (`GET …/console-login/{id}/`), sees it was opened
   and the operator TYPES the number the computer shows (`approve`). Typing, not
   tapping "yes": a person who never sees the attacker's screen cannot type its number.
   Three wrong numbers cancel the link.
4. The COMPUTER's page asks (`complete`) with its cookie: once approved, before the two
   minutes are out, and while the phone's account is still a console operator, the row
   becomes `used` and the view signs that browser in (`django.contrib.auth.login`),
   once. A line goes into the console's own admin log.

So the link alone opens nothing; the phone alone opens nothing (it never sees the
computer's secret); and the session is created only for the browser that opened the
link, only after the phone that made it approved the number that browser shows.

Rate limits: `ISSUE_LIMIT` links per operator per `ISSUE_WINDOW`, `CLAIM_LIMIT` claims
per address per `CLAIM_WINDOW`, `MAX_WRONG_CODES` numbers per link, besides the global
limiter. Nothing here logs an address, an email or a secret: ids only.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext_lazy as _

from .models import ConsoleLogin
from .operators import is_console_operator

logger = logging.getLogger(__name__)

LIFETIME_SECONDS = 120
MAX_WRONG_CODES = 3
ISSUE_LIMIT, ISSUE_WINDOW = 5, 600
CLAIM_LIMIT, CLAIM_WINDOW = 10, 600
#: The cookie the computer that opened the link holds; scoped to the sign-in pages.
COOKIE = "ops_qr_login"
COOKIE_PATH = "/dj-admin/qr-login/"

State = ConsoleLogin.State


class Refusal(Exception):
    """A step refused, with the `code` and status the API answers."""

    def __init__(self, code, status, message, **extra):
        super().__init__(code)
        self.code, self.status, self.message, self.extra = code, status, message, extra


def enabled() -> bool:
    return bool(getattr(settings, "OPS_QR_LOGIN_ENABLED", False))


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def may_sign_in(user) -> bool:
    """A console operator whose address the OTP flow verified — asked at every step
    that could end in a session, not only when the link was made."""
    return is_console_operator(user) and bool(getattr(user, "email_verified", False))


def state_of(ticket, now=None) -> str:
    """The stored state, or `expired` once a live link's two minutes are out."""
    now = now or timezone.now()
    if ticket.state in ConsoleLogin.LIVE and now >= ticket.expires_at:
        return "expired"
    return ticket.state


def seconds_left(ticket, now=None) -> int:
    now = now or timezone.now()
    if state_of(ticket, now) not in ConsoleLogin.LIVE:
        return 0
    return max(0, int((ticket.expires_at - now).total_seconds()))


def _cancel(ticket, reason, now):
    ticket.state = State.CANCELLED
    ticket.cancel_reason = reason
    ticket.cancelled_at = now
    ticket.save(update_fields=["state", "cancel_reason", "cancelled_at"])
    logger.info("console login %s cancelled (%s)", ticket.pk, reason)


def throttle(key, limit, window, message):
    """Count one attempt against `key`; refuse with 429 `throttled` past `limit`."""
    from training_platform.cache import ratelimit_cache
    from training_platform.cache import seconds_left as cache_seconds_left

    rl = ratelimit_cache()
    if (rl.get(key) or 0) >= limit:
        wait = cache_seconds_left(rl, key, window)
        raise Refusal("throttled", 429, message, retry_after_seconds=wait)
    try:
        rl.incr(key)
    except ValueError:
        rl.set(key, 1, window)


# ------------------------------------------------------------------ the phone's steps
def issue(user):
    """A new link for `user`: `(ticket, secret)`. The secret goes into the link and is
    never stored. Every earlier live link of theirs is cancelled."""
    from users.models import CustomUser

    throttle(f"rl:console_login:issue:{user.pk}", ISSUE_LIMIT, ISSUE_WINDOW,
              _("Too many sign-in links. Wait a few minutes and try again."))
    secret = secrets.token_urlsafe(32)
    with transaction.atomic():
        # One operator's links are made one at a time, so "cancel the old, make the
        # new" cannot interleave with another request's (the partial unique index
        # would refuse the second anyway).
        CustomUser.objects.select_for_update().filter(pk=user.pk).first()
        now = timezone.now()
        live = ConsoleLogin.objects.filter(user=user, state__in=ConsoleLogin.LIVE)
        live.filter(expires_at__lte=now).update(
            state=State.CANCELLED, cancel_reason="expired", cancelled_at=now)
        live.update(state=State.CANCELLED, cancel_reason="replaced", cancelled_at=now)
        ticket = ConsoleLogin.objects.create(
            user=user, token_digest=digest(secret),
            expires_at=now + timedelta(seconds=LIFETIME_SECONDS))
    logger.info("console login %s issued for user %s", ticket.pk, user.pk)
    return ticket, secret


def _own(ticket_id, user, lock=True):
    qs = ConsoleLogin.objects.filter(pk=ticket_id, user=user)
    ticket = (qs.select_for_update() if lock else qs).first()
    if ticket is None:
        raise Refusal("console_login_not_found", 404,
                      _("This sign-in link is not yours or does not exist."))
    return ticket


def read(ticket_id, user):
    return _own(ticket_id, user, lock=False)


def approve(ticket_id, user, code):
    """The phone types the number the computer shows."""
    refusal = None
    with transaction.atomic():
        ticket = _own(ticket_id, user)
        now = timezone.now()
        state = state_of(ticket, now)
        if state == "expired":
            raise Refusal("console_login_expired", 410,
                          _("This sign-in link has expired. Make a new one."))
        if state == State.ISSUED:
            raise Refusal("console_login_not_claimed", 409,
                          _("Open the link on your computer first."))
        if state != State.CLAIMED:
            raise Refusal("console_login_closed", 409,
                          _("This sign-in link can no longer be used."), state=state)
        typed = str(code if code is not None else "").strip()
        if not typed:
            raise Refusal("console_login_code_required", 400,
                          _("Type the number your computer shows."), field="code")
        if constant_time_compare(typed, ticket.match_code):
            ticket.state = State.APPROVED
            ticket.approved_at = now
            ticket.save(update_fields=["state", "approved_at"])
            logger.info("console login %s approved on the phone", ticket.pk)
            return ticket
        # A wrong number is remembered even though the request is refused: the count
        # must survive the refusal, so it is saved here and raised after the commit.
        ticket.wrong_codes += 1
        left = max(0, MAX_WRONG_CODES - ticket.wrong_codes)
        ticket.save(update_fields=["wrong_codes"])
        if left == 0:
            _cancel(ticket, "wrong_codes", now)
        refusal = Refusal("console_login_code_mismatch", 400,
                          _("That is not the number your computer shows."),
                          attempts_left=left, state=state_of(ticket, now), field="code")
    raise refusal


def cancel(ticket_id, user):
    """The phone says no, or leaves the screen. Idempotent."""
    with transaction.atomic():
        ticket = _own(ticket_id, user)
        if ticket.state == State.USED:
            raise Refusal("console_login_closed", 409,
                          _("This sign-in link can no longer be used."), state="used")
        if ticket.state in ConsoleLogin.LIVE:
            _cancel(ticket, "phone", timezone.now())
    return ticket


# --------------------------------------------------------------- the computer's steps
def find(secret):
    """The link a secret opens, or None. Never locks: the landing page only reads."""
    if not secret or len(secret) > 100:
        return None
    return ConsoleLogin.objects.filter(token_digest=digest(secret)).first()


def claim(secret, ip, user_agent):
    """The computer pressed Continue: `(ticket, browser_secret)`."""
    if not secret or len(secret) > 100:
        raise Refusal("console_login_not_found", 404, "No such sign-in link.")
    with transaction.atomic():
        ticket = (ConsoleLogin.objects.select_for_update().select_related("user")
                  .filter(token_digest=digest(secret)).first())
        if ticket is None:
            raise Refusal("console_login_not_found", 404, "No such sign-in link.")
        now = timezone.now()
        state = state_of(ticket, now)
        if state != State.ISSUED:
            raise Refusal("console_login_closed", 410,
                          "This link has expired or was already opened.", state=state)
        if not may_sign_in(ticket.user):
            # Cancelled, and refused after the commit: raising in here would undo it.
            _cancel(ticket, "not_operator", now)
            browser_secret = None
        else:
            browser_secret = secrets.token_urlsafe(32)
            ticket.browser_digest = digest(browser_secret)
            ticket.match_code = f"{secrets.randbelow(900) + 100}"
            ticket.state = State.CLAIMED
            ticket.claimed_at = now
            ticket.claimed_ip = ip or None
            ticket.claimed_user_agent = (user_agent or "")[:200]
            ticket.save(update_fields=["browser_digest", "match_code", "state",
                                       "claimed_at", "claimed_ip", "claimed_user_agent"])
    if browser_secret is None:
        raise Refusal("console_login_closed", 410, "This link can no longer be used.",
                      state="cancelled")
    logger.info("console login %s opened on a computer", ticket.pk)
    return ticket, browser_secret


def complete(secret, browser_secret):
    """The computer's page asks whether it may come in: `(answer, ticket)`, where
    `answer` is `signed_in` (the caller logs the browser in NOW), `waiting`, `expired`,
    `cancelled` or `used`. Only the browser that claimed the link gets an answer."""
    if not secret or len(secret) > 100:
        raise Refusal("console_login_not_found", 404, "No such sign-in link.")
    with transaction.atomic():
        ticket = (ConsoleLogin.objects.select_for_update().select_related("user")
                  .filter(token_digest=digest(secret)).first())
        if ticket is None:
            raise Refusal("console_login_not_found", 404, "No such sign-in link.")
        if not (ticket.browser_digest and browser_secret
                and constant_time_compare(digest(browser_secret), ticket.browser_digest)):
            raise Refusal("console_login_wrong_browser", 403,
                          "This link was opened in another browser.")
        now = timezone.now()
        state = state_of(ticket, now)
        if state == State.APPROVED:
            if not may_sign_in(ticket.user):
                _cancel(ticket, "not_operator", now)
                return "cancelled", ticket
            ticket.state = State.USED
            ticket.used_at = now
            ticket.save(update_fields=["state", "used_at"])
            logger.info("console login %s used: user %s signed in", ticket.pk,
                        ticket.user_id)
            return "signed_in", ticket
        return {State.ISSUED: "waiting", State.CLAIMED: "waiting"}.get(state, state), ticket
