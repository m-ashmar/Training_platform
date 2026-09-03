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

Access tokens themselves are not revocable in simplejwt; they expire on their own
(ACCESS_TOKEN_LIFETIME, 60 minutes here). That is the standard bearer-token tradeoff
and is deliberate. What is NOT acceptable is a 7-day refresh token, already revoked,
authenticating a live connection.
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


def authenticate_scope(scope):
    """Return the authenticated user for this connection, or None.

    Runs database queries, so consumers must call it through
    `channels.db.database_sync_to_async`.
    """
    from rest_framework_simplejwt.authentication import JWTAuthentication
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.tokens import AccessToken

    token = token_from_scope(scope)
    if not token:
        logger.warning("websocket auth: no token supplied")
        return None

    try:
        # AccessToken, never UntypedToken: this is what rejects a refresh token.
        validated = AccessToken(token)
        return JWTAuthentication().get_user(validated)
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
