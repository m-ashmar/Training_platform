"""Size limits for WebSocket frames.

Django's `MIDDLEWARE` does not run for WebSocket connections — `ProtocolTypeRouter`
sends them to `AuthMiddlewareStack`, not through the HTTP stack — so
`RequestSizeLimitMiddleware`, the project's only body-size cap, never saw a frame. Daphne
does not set autobahn's payload limits either, and autobahn's defaults are `0`, meaning
unlimited. Nothing anywhere bounded an inbound frame.

Measured against real Daphne with one authenticated client and one frame each:

    baseline RSS                                36 MB
    one 64 MB text frame   -> echoed back,     403 MB peak
    one 128 MB text frame  -> echoed back,     710 MB peak
    one 200 MB text frame  ->                  958 MB peak

`fly.toml` gives the web process 512 MB, and one process group serves all HTTP and all
WebSockets, so a single frame from any authenticated account takes the whole API down.
`SocialConsumer` echoes the frame back, which doubles the peak: the attacker uploads
once, the server pays twice.

The cap is applied as a CLASS attribute on Daphne's protocol, which is autobahn's own
documented override point: `WebSocketProtocol._connectionMade` copies each config
attribute from the factory only `if not hasattr(self, configAttr)`, so a value on the
protocol class wins over the factory's unlimited default. autobahn then fails the
connection while the frame is still arriving, before the bytes are assembled — which is
the only place this can be fixed, since the ASGI application is not handed the message
until assembly is already complete.

256 KB is roughly a hundred times the largest thing a client legitimately sends: the AI
message cap is 2,000 characters (`InputSanitizer.MAX_LENGTH`) and every other frame on
either consumer is a small JSON object.
"""

#: Bytes. Both the whole message and any single frame of it.
MAX_WS_MESSAGE_BYTES = 256 * 1024


def apply_websocket_limits() -> bool:
    """Cap inbound frame size on Daphne's protocol. True if it was applied."""
    try:
        from daphne.ws_protocol import WebSocketProtocol
    except Exception:  # pragma: no cover - Daphne absent (tests, wsgi-only runs)
        return False

    WebSocketProtocol.maxMessagePayloadSize = MAX_WS_MESSAGE_BYTES
    WebSocketProtocol.maxFramePayloadSize = MAX_WS_MESSAGE_BYTES
    return True


class SameSiteOriginValidator:
    """Reject a WebSocket handshake from a browser page that is not ours.

    `AuthMiddlewareStack` bundles cookie, session and auth middleware and deliberately
    does NOT check `Origin`; Daphne additionally runs with `allowNullOrigin=True`. So any
    web page could open a socket to this API, and browsers attach cookies to a cross-site
    WebSocket handshake with no CORS preflight to stop them. Nothing reads
    `scope["user"]` today, which is the only reason this is not already exploitable —
    the day a consumer trusts the user that `AuthMiddlewareStack` populates from that
    cookie, it becomes cross-site WebSocket hijacking.

    Channels' own `AllowedHostsOriginValidator` cannot be used as-is: it rejects a
    handshake with NO `Origin` header unless `ALLOWED_HOSTS` contains `*`, and the
    Flutter client is not a browser and sends none. Wrapping the router in it would take
    the mobile app offline.

    So: a handshake with no `Origin` is allowed (a native client, which cannot be made to
    forge one from a hostile page), and a handshake that DOES carry one must match
    `ALLOWED_HOSTS`. That is the rule an API serving both browsers and native apps needs.
    """

    def __init__(self, application):
        self.application = application

    @staticmethod
    def origin_of(scope):
        """The handshake's `Origin` header, or None when it sent none."""
        for name, value in scope.get("headers") or []:
            if name == b"origin":
                value = (value or b"").strip()
                return value or None
        return None

    @classmethod
    def should_validate(cls, scope) -> bool:
        """Only a handshake that CARRIES an Origin is checked against ALLOWED_HOSTS.

        A native client sends none and cannot be made to forge one from a hostile page,
        so there is nothing to validate; a browser always sends one, and that is the
        vector this exists to close.
        """
        return cls.origin_of(scope) is not None

    async def __call__(self, scope, receive, send):
        from channels.security.websocket import AllowedHostsOriginValidator

        if not self.should_validate(scope):
            return await self.application(scope, receive, send)
        return await AllowedHostsOriginValidator(self.application)(scope, receive, send)
