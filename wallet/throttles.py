from rest_framework.throttling import SimpleRateThrottle


class ChargingRateThrottle(SimpleRateThrottle):
    scope = "charging"

    def get_ident(self, request):
        """The client address from the trusted proxy chain — the rate limiter's helper.

        DRF's own `get_ident`, with no `NUM_PROXIES` in REST_FRAMEWORK, keys on the WHOLE
        X-Forwarded-For header, and the client writes the left-hand part of it: rotating
        a fake first entry gave every request a fresh bucket, so this throttle on the
        money routes throttled nothing.
        """
        from training_platform.middleware import get_trusted_client_ip

        return get_trusted_client_ip(request)

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }
