"""
users/health_views.py — liveness/readiness probe.

Mounted at /api/auth/health/ because fly.toml's [[http_service.checks]] polls that
exact path every 30s. Four things must all hold or the check fails and Fly marks the
machine unhealthy (failing the deploy):

  1. The route exists and returns 200.
  2. It is exempt from SECURE_SSL_REDIRECT — Fly's internal check hits the container
     over plain HTTP without X-Forwarded-Proto, so a redirect would return 301.
  3. It is exempt from RateLimitMiddleware — 30s polling is 120 req/hour against an
     anonymous limit of 100/hour, which would start returning 429.
  4. It is answered whatever Host header the probe sends. The checker may address the
     machine by its private IP, and ALLOWED_HOSTS refuses wildcards in production, so
     CommonMiddleware answered every probe 400 DisallowedHost.
     `training_platform/health_probe.py` answers exactly this path, GET and HEAD, before
     anything reads the Host header — see its docstring for why that and not a Host
     header in fly.toml.

Semantics:
  * DB unreachable      -> 503 (the app genuinely cannot serve requests)
  * Cache unreachable   -> 200 with status "degraded" (JWT auth is stateless and the
                           public/edamam caches ignore Redis errors, so we can serve)

It never looks at the background side (Celery worker and beat). A failing machine check
restarts the machine, and a late beat must not restart the web machines serving users.
The background has its own route, `BackgroundHealthView` below, for an external monitor.
"""
import logging

from django.db import connection
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

logger = logging.getLogger(__name__)


class HealthCheckView(APIView):
    """Unauthenticated dependency probe. Must stay cheap — it runs every 30s."""

    permission_classes = [AllowAny]
    authentication_classes = []  # never touch auth backends on a probe
    # JSON only. The browsable renderer builds absolute URLs from the Host header, which
    # is exactly what a probe answered before the host check must never read.
    renderer_classes = [JSONRenderer]

    def get(self, request):
        db_ok = self._check_db()
        cache_ok = self._check_cache()

        if not db_ok:
            payload = {"status": "unhealthy", "database": "down",
                       "cache": "ok" if cache_ok else "down"}
            return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(
            {
                "status": "ok" if cache_ok else "degraded",
                "database": "ok",
                "cache": "ok" if cache_ok else "down",
            },
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _check_db() -> bool:
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception as e:
            logger.error(f"Health check: database unreachable: {e}")
            return False

    @staticmethod
    def _check_cache() -> bool:
        try:
            from django.core.cache import cache
            cache.set("healthcheck:ping", "1", 10)
            return cache.get("healthcheck:ping") == "1"
        except Exception as e:
            logger.warning(f"Health check: cache unreachable: {e}")
            return False


class BackgroundHealthView(APIView):
    """GET /api/auth/health/background/ — is the background side alive? Public, read-only.

    For an external uptime monitor to poll (every minute is plenty). It is NOT the
    platform's machine health check and must never become one: see
    `training_platform/background_health.py`.

    Needs no headers and no body. Answers:

    * 200 `{"status": "ok", "heartbeat": {...}, "jobs": {...}}` — beat scheduled, a worker
      ran the heartbeat within `max_age_seconds`, and no watched job is stale.
    * 503 `{"status": "stale", ...}` — the heartbeat is `missing` or `stale`, or a watched
      job is `stale` (its last success is older than its allowance, or it has never
      succeeded in that long). Alert on this.
    * 503 `{"status": "unreachable", "detail"}` — the store the markers live in (Redis)
      cannot be read.
    * 200 `{"status": "inline", "detail"}` — tasks run inline (development, the test
      suite); there is nothing to watch.

    `heartbeat` and each entry of `jobs` (keyed by beat entry name) carry `status`
    (`ok` | `stale` | `missing` for the heartbeat, `ok` | `stale` | `waiting` for a job),
    `age_seconds` (null when never seen) and `max_age_seconds`. The same on every build:
    it says nothing about any user and is the same for whoever asks.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    renderer_classes = [JSONRenderer]

    def get(self, request):
        from training_platform.background_health import report

        http_status, body = report()
        return Response(body, status=http_status)
