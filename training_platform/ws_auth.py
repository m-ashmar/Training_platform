"""JWT authentication for WebSocket connections.

One implementation for every consumer. There were two copies of this logic, and the
second carried a comment describing the first as "the proven pattern from
SocialConsumer" — the pattern was wrong, and copying it doubled the hole rather than
proving anything.

What was wrong: both used `UntypedToken`, whose whole purpose is to accept a token
WITHOUT checking what kind it is. So a refresh token opened a socket, and because
`UntypedToken` does not inherit `BlacklistMixin` it never consulted the blacklist
either. Logging out therefore closed nothing: the refresh token the logout endpoint
had just blacklisted still opened both sockets, including the AI one that spends
money per message.

`AccessToken` is the right class. It verifies the signature, enforces `exp`, and
enforces `token_type == "access"`, so a refresh token is rejected as the wrong
credential before the blacklist is even relevant.

Access tokens are revoked by a password change (the owner, 2026-11-24,
`training_platform/jwt_sessions.py`): a token made under an older password is refused
here as on every route. What is NOT acceptable either is a 7-day refresh token, already
revoked, authenticating a live connection.

An OPEN socket ends at the change too, not at its next frame or at its token's expiry:
every consumer joins its account's session group (`session_group`), and
`users/utils.py end_every_session` sends `session.ended` to it when the change commits
(`end_open_sockets`). Each socket then re-checks its own credential
(`authenticate_scope`, one query) and closes if it no longer holds, so a socket opened
with the fresh pair a password change returns stays open.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)


def token_from_scope(scope) -> str | None:
    """Pull the bearer token out of an ASGI scope.

    Prefers the Authorization header when a client can set one, and falls back to
    `?token=` which is what browser WebSocket clients are limited to.
    """
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            raw = value.decode("latin-1", "ignore").strip()
            if raw.lower().startswith("bearer "):
                return raw[7:].strip() or None

    query_string = (scope.get("query_string") or b"").decode("latin-1", "ignore")
    token = parse_qs(query_string).get("token", [None])[0]
    return (token or "").strip() or None


def token_expires_at(scope) -> float | None:
    """When the access token this socket connected with expires (epoch seconds), or None
    when there is no usable token.

    No database: only the `exp` claim of the token `authenticate_scope` accepted. For a
    socket that only LISTENS — it sends no frame, so nothing re-runs the per-frame check —
    this is the one thing that can still end it (the batch A review, 2026-11-23).
    """
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.tokens import AccessToken

    token = token_from_scope(scope)
    if not token:
        return None
    try:
        return float(AccessToken(token)["exp"])
    except (TokenError, KeyError, TypeError, ValueError):
        return None


def still_authenticated(scope) -> bool:
    """Is the credential this socket connected with STILL good?

    A WebSocket presents its token exactly once. `ACCESS_TOKEN_LIFETIME` is 60 minutes,
    but nothing re-reads it, so on this surface it is effectively unbounded: measured
    with a 5-second token, the socket kept answering seven seconds later, after logging
    out (which blacklists the refresh token), and after the account was disabled — a
    banned user kept spending model tokens until they chose to close the app.

    `_check_premium` is already re-run per message, which shows the shape was understood;
    only entitlement was wired up, not identity. This is the identity half: it re-parses
    the token (so expiry bites) and re-reads `is_active` (so a suspension bites), which
    is everything simplejwt can tell us about an access token.

    Runs database queries, so consumers must call it through
    `channels.db.database_sync_to_async`.
    """
    return authenticate_scope(scope) is not None


def authenticate_scope(scope):
    """Return the authenticated user for this connection, or None.

    Runs database queries, so consumers must call it through
    `channels.db.database_sync_to_async`.
    """
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.tokens import AccessToken

    from training_platform.jwt_sessions import SessionJWTAuthentication

    token = token_from_scope(scope)
    if not token:
        logger.warning("websocket auth: no token supplied")
        return None

    try:
        # AccessToken, never UntypedToken: this is what rejects a refresh token. The
        # REST routes' authentication, so a password change refuses it here too.
        validated = AccessToken(token)
        return SessionJWTAuthentication().get_user(validated)
    except TokenError as exc:
        logger.warning("websocket auth: rejected token (%s)", exc)
        return None
    except Exception as exc:  # user lookup failed, user inactive, malformed claim
        logger.warning("websocket auth: could not resolve user (%s)", exc)
        return None

    # No close_old_connections() here. channels.db.database_sync_to_async, which every
    # caller goes through, already closes stale connections either side of the call.
    # Doing it again inside the function closed the connection on the rejection path
    # too, which breaks any caller still holding it — a test running inside a
    # transaction sees "connection already closed" on its very next query.


# ------------------------------------------------------------- the session's end
def session_group(user_id) -> str:
    """The channel group every socket of one account joins (`SessionEndsMixin`)."""
    return f"session_{user_id}"


def end_open_sockets(user_id) -> None:
    """Tell every open socket of the account to re-check its credential now
    (`SessionEndsMixin.session_ended`). Best effort: a channel layer that cannot be
    reached is logged, and those sockets end at their next frame or their token's
    expiry, as before. Called on commit by `users/utils.py end_every_session`."""
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    try:
        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(session_group(user_id),
                                            {"type": "session.ended"})
    except Exception:
        logger.warning("could not tell user %s's open sockets that the session ended",
                       user_id, exc_info=True)


class SessionEndsMixin:
    """For an `AsyncWebsocketConsumer`: close the socket when its account's session
    ends (a password change, a reset, a deactivation), not at its next frame.

    Call `join_session_group()` once the user is known, before `accept()`, and
    `leave_session_group()` in `disconnect`. `session_ended` re-checks this socket's
    own token, so a socket opened with a fresh pair after the change stays open; when
    the token no longer holds, `close_for_ended_session()` runs (a consumer overrides it
    to say why first)."""

    _session_group = None

    async def join_session_group(self):
        self._session_group = session_group(self.user.pk)
        await self.channel_layer.group_add(self._session_group, self.channel_name)

    async def leave_session_group(self):
        if self._session_group:
            await self.channel_layer.group_discard(self._session_group, self.channel_name)

    async def session_ended(self, event):
        from channels.db import database_sync_to_async

        user = await database_sync_to_async(authenticate_scope)(self.scope)
        if user is None or user.pk != getattr(self.user, "pk", None):
            await self.close_for_ended_session()

    async def close_for_ended_session(self):
        await self.close(code=4001)
