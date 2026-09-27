"""Answer the platform's machine health check before anything reads the Host header.

THE FAILURE. fly.toml's `[[http_service.checks]]` polls `GET /api/auth/health/` on each
web machine. Fly's checker talks to the machine directly, and what it sends as Host is
not something this repository controls — commonly the machine's private address and
port. Django's CommonMiddleware calls `request.get_host()` on every request, which
raises DisallowedHost for any name outside ALLOWED_HOSTS, and production refuses a
wildcard there (`settings_production.enforce_production_safety`). Every probe would
then be a 400, the machine never healthy, and the first deploy failed. The audit could
not confirm which Host the checker sends (deploy finding 5); this makes the answer not
matter.

THE CHOICE — a narrow exemption here, not a Host header in fly.toml. The alternative,
`headers = { Host = "<api domain>" }` on the check, needs the API's domain written into
fly.toml, and the owner has not chosen domains yet; it would also have to be kept equal
to DJANGO_ALLOWED_HOSTS by hand for ever, and the day they drift every deploy fails its
health check with a message about hosts. This exemption depends on nothing:

* exactly the path `/api/auth/health/` (no prefix match, not `/api/auth/health`, not
  `/api/auth/health/background/`), and only GET and HEAD. Every other request still
  meets the host check unchanged.
* The answer uses nothing the Host header could poison: no absolute URL, no redirect,
  no cookie, no template. `HealthCheckView` renders JSON only (the browsable renderer
  builds URLs from the host) and reports two booleans.

It sits after SecurityMiddleware — whose SSL redirect already exempts this path, and
which reads no host for it — and after SecurityHeadersMiddleware, so the probe's answer
still carries the security headers; and before CommonMiddleware, the first thing that
reads the host. Rate limiting never applied to this path either
(`RateLimitMiddleware._should_skip`).
"""

HEALTH_PATH = "/api/auth/health/"


class HealthProbeMiddleware:
    """Serves `GET|HEAD /api/auth/health/` itself; passes everything else through."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._view = None

    def __call__(self, request):
        if request.path == HEALTH_PATH and request.method in ("GET", "HEAD"):
            if self._view is None:
                from users.health_views import HealthCheckView

                self._view = HealthCheckView.as_view()
            response = self._view(request)
            # A response returned by a middleware is not rendered by the handler, which
            # renders only what the URL resolver's view returned.
            response.render()
            return response
        return self.get_response(request)
