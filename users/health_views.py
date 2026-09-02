"""
users/health_views.py — liveness/readiness probe.

Mounted at /api/auth/health/ because fly.toml's [[http_service.checks]] polls that
exact path every 30s. Three things must all hold or the check fails and Fly marks the
machine unhealthy (failing the deploy):

  1. The route exists and returns 200.
  2. It is exempt from SECURE_SSL_REDIRECT — Fly's internal check hits the container
     over plain HTTP without X-Forwarded-Proto, so a redirect would return 301.
  3. It is exempt from RateLimitMiddleware — 30s polling is 120 req/hour against an
     anonymous limit of 100/hour, which would start returning 429.

Semantics:
  * DB unreachable      -> 503 (the app genuinely cannot serve requests)
  * Cache unreachable   -> 200 with status "degraded" (JWT auth is stateless and the
                           public/edamam caches ignore Redis errors, so we can serve)
"""
import logging

from django.db import connection
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

logger = logging.getLogger(__name__)


class HealthCheckView(APIView):
    """Unauthenticated dependency probe. Must stay cheap — it runs every 30s."""

    permission_classes = [AllowAny]
    authentication_classes = []  # never touch auth backends on a probe

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
