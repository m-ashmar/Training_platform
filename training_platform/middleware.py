"""
Middleware for Training Platform

This module provides comprehensive middleware for rate limiting,
error handling, security, and performance monitoring.
"""

import time
import json
import logging
from django.core.cache import cache
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin
from django.utils.translation import gettext as _
from django.conf import settings
from django.utils import timezone
import secrets
# Named-backend accessors — routes to the correct Redis DB segment
from training_platform.cache import ratelimit_cache, public_cache, private_cache
from django.db import connection
from django.utils import timezone
from django.shortcuts import redirect
import re

logger = logging.getLogger(__name__)


def get_trusted_client_ip(request):
    """
    Derive the client IP from the trusted proxy chain.

    X-Forwarded-For is attacker-controllable: a client may inject arbitrary
    entries on the LEFT of the header. Our own proxies append the true upstream
    IP on the RIGHT, so the reliable client IP is the entry `NUM_PROXIES` from
    the right. Trusting the leftmost entry (the previous behaviour) let an
    attacker rotate the rate-limit key at will and bypass throttling entirely.

    Falls back to REMOTE_ADDR when the header is absent or malformed.
    """
    num_proxies = getattr(settings, 'NUM_PROXIES', 1) or 1
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        parts = [p.strip() for p in xff.split(',') if p.strip()]
        if len(parts) >= num_proxies:
            return parts[-num_proxies]
    return request.META.get('REMOTE_ADDR', 'unknown')


class RateLimitMiddleware(MiddlewareMixin):
    """
    Rate limiting middleware with different limits for different user types
    """
    
    def __init__(self, get_response=None):
        super().__init__(get_response)
        # Rate limits are always enforced. In DEBUG mode, limits are higher
        # for development convenience but never disabled entirely.
        if settings.DEBUG:
            self.rate_limits = {
                'anonymous': {'requests': 10000, 'window': 60},
                'client': {'requests': 10000, 'window': 60},
                'trainer': {'requests': 10000, 'window': 60},
                'admin': {'requests': 10000, 'window': 60},
            }
        else:
            self.rate_limits = {
                # Anonymous callers are keyed by IP (see _get_client_id). Syrian mobile
                # carriers run carrier-grade NAT, so thousands of real users share a
                # handful of public addresses — at 100/hour one carrier's entire
                # subscriber base shares a bucket that a few dozen signups exhaust,
                # locking everyone else out of login, registration and OTP.
                #
                # This bucket is a coarse DoS ceiling, NOT the brute-force control. The
                # real limits are identity-scoped and unaffected by NAT: OTP resend is
                # 3/hour per email+IP, password reset 3/hour per email, and OTP verify
                # locks after 5 attempts on the record itself. Raising this does not
                # weaken any of them.
                'anonymous': {'requests': 2000, 'window': 3600},
                # Authenticated buckets are keyed by user id, so NAT does not apply.
                # 1500, the owner's number (2026-09-19): a workout session with set
                # logging, home and chat outruns 500 an hour.
                'client': {'requests': 1500, 'window': 3600},
                'trainer': {'requests': 1000, 'window': 3600},
                'admin': {'requests': 5000, 'window': 3600},
            }
    
    def process_request(self, request):
        """
        Check rate limits before processing request
        """
        # Skip rate limiting for certain paths
        if self._should_skip(request):
            return None
        
        # Get client identifier
        client_id = self._get_client_id(request)
        user_type = self._get_user_type(request)
        
        # Check rate limit
        if self._is_rate_limited(client_id, user_type):
            # This response never reaches the DRF exception handler, so it carries
            # the contract keys itself: every error in the API has detail/error/code.
            # The seconds the window really has left (BACKEND_BUGS B-022), not the
            # window's full length every time.
            wait = self._seconds_left(client_id, user_type)
            _msg = _('Too many requests. Please try again later.')
            return JsonResponse({
                'detail': _msg,
                'error': _msg,
                'code': 'rate_limited',
                'message': _msg,          # kept: existing key
                'retry_after': wait,
                'retry_after_seconds': wait,
            }, status=429, headers={'Retry-After': str(wait)})
        
        return None
    
    def _should_skip(self, request):
        """
        Skip rate limiting for certain paths
        """
        skip_paths = [
            '/health/',
            # Fly polls this every 30s = 120 req/hour, which would blow the
            # anonymous 100/hour limit and start returning 429 -> machine unhealthy.
            '/api/auth/health/',
            # '/admin/' is deliberately NOT here. It was, and `startswith` made the
            # exemption cover /admin/login/ — django.contrib.admin's own login form,
            # which has no attempt limit of its own. That left the one login page in the
            # product with no ceiling at all, against accounts that can reverse
            # transactions and read the audit log, while /dj-admin/ and /api/auth/login/
            # were both limited. Its static assets are already covered by '/static/'.
            '/static/',
            '/media/',
        ]
        return any(request.path.startswith(path) for path in skip_paths)
    
    def _get_client_id(self, request):
        """Who this request is, for rate-limiting purposes.

        The JWT is read here because DRF's authentication runs in the VIEW, after every
        middleware. `request.user` is populated only by session auth, so every request
        from the mobile app — which authenticates with a bearer token — fell through to
        the IP bucket. Behind carrier-grade NAT that is one shared bucket for a whole
        network: a few hundred users exhaust the anonymous allowance and everyone on
        that carrier gets 429. The two middlewares beside this one already read the
        token for exactly this reason.
        """
        if hasattr(request, 'user') and request.user.is_authenticated:
            return f"user_{request.user.id}"

        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            try:
                from rest_framework_simplejwt.tokens import AccessToken

                user_id = AccessToken(auth_header.split(' ', 1)[1]).get('user_id')
                if user_id:
                    return f"user_{user_id}"
            except Exception:
                # An invalid or expired token is an anonymous caller, and the IP
                # bucket is the right place for it.
                logger.debug("rate limit: unusable bearer token", exc_info=True)
        return f"ip_{self._get_client_ip(request)}"
    
    def _get_client_ip(self, request):
        """
        Get client IP address from the trusted proxy chain (spoof-resistant).
        """
        return get_trusted_client_ip(request)
    
    def _get_user_type(self, request):
        """The tier of the caller, from the same identity `_get_client_id` reads.

        The tier came from the session user only, which bearer-token callers (the whole
        mobile app) never have: every one of them was bucketed by user id but limited as
        `anonymous` (BACKEND_BUGS B-006). The type is read once per user and minute.
        """
        if hasattr(request, 'user') and request.user.is_authenticated:
            return getattr(request.user, 'user_type', 'client')
        client_id = self._get_client_id(request)
        if not client_id.startswith('user_'):
            return 'anonymous'
        user_id = client_id[len('user_'):]
        key = f"rl_tier:{user_id}"
        # Fail open like the limiter itself: a cache outage must not turn every
        # authenticated request into a 500.
        try:
            tier = ratelimit_cache().get(key)
        except Exception:
            tier = None
        if tier is None:
            from django.contrib.auth import get_user_model
            tier = (get_user_model().objects.filter(pk=user_id)
                    .values_list('user_type', flat=True).first()) or 'anonymous'
            try:
                ratelimit_cache().set(key, tier, 60)
            except Exception:
                logger.warning("rate limit: could not memoise the tier", exc_info=True)
        return tier if tier in self.rate_limits else 'client'
    
    def _seconds_left(self, client_id, user_type):
        from training_platform.cache import seconds_left
        limits = self.rate_limits.get(user_type, self.rate_limits['anonymous'])
        return seconds_left(ratelimit_cache(), f"rate_limit:{client_id}", limits['window'])

    def _is_rate_limited(self, client_id, user_type):
        """
        Check if client is rate limited.
        Uses atomic incr to prevent race conditions.
        Uses ratelimit_cache (DB1) — isolated from session store.
        """
        limits = self.rate_limits.get(user_type, self.rate_limits['anonymous'])
        cache_key = f"rate_limit:{client_id}"
        rl = ratelimit_cache()

        try:
            # Atomic increment — no race condition between read and write
            current = rl.incr(cache_key)
        except ValueError:
            # Key does not exist yet — initialise with TTL
            rl.set(cache_key, 1, limits['window'])
            return False
        except Exception as e:
            logger.error(f"Rate limiting error: {e}")
            return False  # Fail open

        # If this is the first request, set TTL on the newly created key
        if current == 1:
            rl.expire(cache_key, limits['window']) if hasattr(rl, 'expire') else None

        return current > limits['requests']


#: Status -> the `code` an error carries when the view did not name one. Not a
#: substitute for a specific code, but the contract says every error response has one
#: and a client that must branch on `code` cannot branch on its absence.
STATUS_CODES = {
    400: "bad_request", 401: "not_authenticated", 403: "permission_denied",
    404: "not_found", 405: "method_not_allowed", 409: "conflict",
    413: "payload_too_large", 415: "unsupported_media_type",
    422: "unprocessable", 429: "throttled",
}


def normalize_error_envelope(response):
    """
    Give every error response ONE shape: `error`, `detail` and `code`, always.

    DRF emits {"detail": ...} for 401/403/404/405 while the project's own views emit
    {"error": ...}, so a mobile client had to branch on both. Worse, the contract makes
    `code` mandatory and says to branch on it rather than on `detail` — which is
    translated — and 128 of the diet app's error returns carried neither. An Arabic
    client received `{"error": "تعذّر إتمام الطلب."}` and could not tell a bad date from
    a server fault in any language.

    Only fills what is missing: a view that names its own `code` keeps it.
    """
    try:
        if not (400 <= response.status_code < 600):
            return response
        data = getattr(response, 'data', None)
        if not isinstance(data, dict):
            return response
        changed = False
        if 'error' not in data and 'detail' in data:
            data['error'] = data['detail']
            changed = True
        if 'detail' not in data and 'error' in data:
            data['detail'] = data['error']
            changed = True
        if 'code' not in data:
            data['code'] = STATUS_CODES.get(
                response.status_code,
                "server_error" if response.status_code >= 500 else "bad_request")
            changed = True
        if changed and hasattr(response, '_is_rendered'):
            response._is_rendered = False
            response.render()
    except Exception:
        # Optional side effect: swallowing this silently is what made the
        # surrounding failures invisible in logs. Control flow is unchanged.
        logger.debug('suppressed non-fatal error', exc_info=True)
    return response


class ErrorHandlingMiddleware(MiddlewareMixin):
    """
    Standardized error handling middleware with i18n support.
    """

    def process_response(self, request, response):
        return normalize_error_envelope(response)
    
    def process_exception(self, request, exception):
        """
        Handle exceptions and return standardized error responses
        """
        logger.error(f"Exception in {request.path}: {exception}", exc_info=True)
        
        # Determine error type and response
        error_response = self._get_error_response(exception)

        # Add request context to error
        error_response['request_id'] = getattr(request, 'id', 'unknown')
        error_response['timestamp'] = timezone.now().isoformat()

        # The SAME envelope every other error uses. This one is built by hand and
        # returned as a JsonResponse, so `normalize_error_envelope` never touches it —
        # and `error` held a machine string while `message` held the sentence, the exact
        # inversion of the documented shape, with no `code` and no `detail` at all. A
        # client told to branch on `code` could not handle any genuinely unhandled
        # exception on the platform.
        status_code = error_response['status']
        machine = error_response.pop('error', None)
        human = error_response.pop('message', '')
        error_response['code'] = machine or STATUS_CODES.get(
            status_code, "server_error" if status_code >= 500 else "bad_request")
        error_response['error'] = human
        error_response['detail'] = human

        return JsonResponse(error_response, status=status_code)
    
    def _get_error_response(self, exception):
        """
        Generate standardized error response based on exception type
        """
        from django.core.exceptions import ValidationError, PermissionDenied
        from django.http import Http404
        from rest_framework.exceptions import APIException
        from django.utils.translation import gettext as _
        
        if isinstance(exception, ValidationError):
            return {
                'error': 'validation_error',
                'message': _('Invalid input data'),
                'details': (exception.message_dict 
                           if hasattr(exception, 'message_dict') 
                           else str(exception)),
                'status': 400
            }
        elif isinstance(exception, PermissionDenied):
            return {
                'error': 'permission_denied',
                'message': _('You do not have permission to perform this action'),
                'status': 403
            }
        elif isinstance(exception, Http404):
            return {
                'error': 'not_found',
                'message': _('The requested resource was not found'),
                'status': 404
            }
        elif isinstance(exception, APIException):
            return {
                'error': 'api_error',
                'message': str(exception.detail),
                'status': exception.status_code
            }
        else:
            # Generic server error
            return {
                'error': 'internal_server_error',
                'message': _('An unexpected error occurred. Please try again later.'),
                'status': 500
            }


class SecurityHeadersMiddleware(MiddlewareMixin):
    """
    Add security headers to all responses.
    Generates a per-request CSP nonce to replace unsafe-inline.
    Templates should use: <script nonce="{{ request.csp_nonce }}">
    """

    def process_request(self, request):
        """
        Generate a cryptographically secure per-request nonce for CSP.
        Store on request so views/templates can reference it.
        """
        import secrets as _secrets
        request.csp_nonce = _secrets.token_urlsafe(16)
        return None

    def process_response(self, request, response):
        """
        Add security headers to response.
        """
        # Security headers
        response['X-Content-Type-Options'] = 'nosniff'
        response['X-Frame-Options'] = 'DENY'
        response['X-XSS-Protection'] = '1; mode=block'
        response['Referrer-Policy'] = 'strict-origin-when-cross-origin'

        # Content Security Policy — nonce-based, no unsafe-inline
        nonce = getattr(request, 'csp_nonce', '')
        if not settings.DEBUG:
            response['Content-Security-Policy'] = (
                f"default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                f"style-src 'self' 'nonce-{nonce}'; "
                f"img-src 'self' data: https:; "
                f"connect-src 'self'; "
                f"frame-ancestors 'none'"
            )
        else:
            # In DEBUG, allow unsafe-inline for dev convenience only
            response['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "connect-src 'self'"
            )

        return response

    def _handle_ssl_redirect(self, request):
        """
        If SECURE_SSL_REDIRECT is enabled, allow exemptions for API paths.
        """
        try:
            if getattr(settings, 'SECURE_SSL_REDIRECT', False):
                path = request.path or ''
                exempt = any(re.match(pattern, path) for pattern in getattr(settings, 'SECURE_REDIRECT_EXEMPT', []))
                if exempt:
                    return None
        except Exception:
            return None
        return None


class RequestLoggingMiddleware(MiddlewareMixin):
    """
    Log all requests for monitoring and analytics
    """
    
    def process_request(self, request):
        """
        Log request start and set start time.
        Timing is stored on the request (not on self) — the middleware instance is
        shared across concurrent requests under ASGI, so per-request state on self
        would race and mis-report durations.
        """
        request._log_start_time = time.time()
        request.id = self._generate_request_id()

        # Log request details
        logger.info(f"Request {request.id}: {request.method} {request.path}")

        return None

    def process_response(self, request, response):
        """
        Log response details and performance metrics
        """
        start_time = getattr(request, '_log_start_time', None)
        if start_time:
            duration = time.time() - start_time
            
            # Log response
            logger.info(
                f"Response {getattr(request, 'id', 'unknown')}: "
                f"{response.status_code} in {duration:.3f}s"
            )
            
            # Log slow requests
            if duration > 2.0:
                logger.warning(
                    f"Slow request {getattr(request, 'id', 'unknown')}: "
                    f"{request.method} {request.path} took {duration:.3f}s"
                )
        
        return response
    
    def _generate_request_id(self):
        """
        Generate unique request ID
        """
        import uuid
        return str(uuid.uuid4())[:8]


class DatabaseQueryCountMiddleware(MiddlewareMixin):
    """
    Monitor database query count for performance optimization.
    Only active in DEBUG mode to avoid overhead in production.
    """
    
    def __init__(self, get_response=None):
        super().__init__(get_response)
        # Only enable in DEBUG mode
        self.enabled = settings.DEBUG
    
    def process_request(self, request):
        """Remember where this request's queries begin.

        This used to call `reset_queries()`, which clears the log that
        `CaptureQueriesContext` — and therefore `assertNumQueries` — reads. Any query
        count asserted around a test-client request observed zero and passed, so the
        two N+1s in this codebase could not have been caught by a test. Recording the
        offset counts the same queries and leaves the log intact for whoever else is
        reading it.
        """
        if not self.enabled:
            return None
        try:
            request._query_log_offset = len(connection.queries)
        except Exception:
            request._query_log_offset = 0
        return None
    
    def process_response(self, request, response):
        """
        Log query count and slow queries (only in DEBUG)
        """
        if not self.enabled:
            return response

        offset = getattr(request, '_query_log_offset', 0)
        queries = connection.queries[offset:]
        query_count = len(queries)

        # Log high query count
        if query_count > 20:
            logger.warning(
                f"High query count for {request.path}: {query_count} queries"
            )

        # Log slow queries
        for query in queries:
            if float(query['time']) > 0.1:  # Queries > 100ms
                logger.warning(
                    f"Slow query ({query['time']}s): {query['sql'][:200]}..."
                )

        return response


class CacheMiddleware(MiddlewareMixin):
    """
    Cache commonly requested data for performance optimization.
    Caches GET requests to static or semi-static endpoints.
    """
    
    # WHAT gets cached, in which segment, for how long, and which model invalidates
    # it, is declared once in training_platform/cache_config.py. Both this middleware
    # and the invalidation signals read that registry, so they cannot drift apart —
    # previously four of six configured paths pointed at routes that did not exist.
    
    def process_request(self, request):
        """
        Check if response is cached. Routes to DB3 (private) for authenticated
        callers and DB2 (public) for anonymous — decided by real identity, never
        by inspecting the hashed key.
        """
        from training_platform.cache_config import match_route
        rule = match_route(request.path) if request.method == 'GET' else None
        if rule:
            identity, is_private = self._scope_for(request, rule)
            if identity is None:
                # A token whose account is gone: never a hit, never a write.
                return None
            cache_key = self._build_key(request, identity, rule)
            cache_backend = private_cache() if is_private else public_cache()
            cached_response = cache_backend.get(cache_key)
            if cached_response is not None:
                logger.debug(f"Cache hit for {request.path} (from {'private' if is_private else 'public'} cache)")
                # Django runs `process_response` for the response returned here too.
                # Without this mark it wrote the hit straight back with a fresh TTL, so
                # an entry read once per TTL never expired, and the staleness the
                # registry bounds at 300 or 600 s had no bound at all for anything that
                # does not bump the model version (an exchange rate, a wallet's
                # currency, a trainer-client relation).
                request._api_cache_hit = True
                return JsonResponse(cached_response, safe=False)
        return None
    
    def process_response(self, request, response):
        """
        Cache successful GET responses.
        Writes to private_cache (DB3) if authenticated, public_cache (DB2) if anonymous.
        Sets Vary header so CDNs know which dimensions affect the response.
        """
        if (request.method == 'GET' and
            response.status_code == 200 and
            self._is_cacheable(request)):

            from training_platform.cache_config import match_route
            rule = match_route(request.path)
            identity, is_private = self._scope_for(request, rule)
            if identity is None:
                return response
            cache_key = self._build_key(request, identity, rule)

            is_json = response.get('Content-Type', '').startswith('application/json')
            try:
                # Only cache JSON responses. A hit is served, never written again: the
                # entry keeps the expiry it was stored with (see `process_request`).
                if is_json and not getattr(request, '_api_cache_hit', False):
                    response_data = json.loads(response.content)
                    backend = private_cache() if is_private else public_cache()
                    backend.set(cache_key, response_data, rule['ttl'])
                    logger.debug(f"Cached response for {request.path} into {'private' if is_private else 'public'} cache")
                if is_json:
                    # Inform CDN / proxies of dimensions that affect this response.
                    # PATCHED, not assigned: a plain assignment threw away the `Vary`
                    # already on the response — `Cookie` from the session middleware,
                    # `Accept-Encoding` from gzip — on exactly the four cached routes,
                    # so a proxy was free to serve one user's gzipped, session-scoped
                    # variant to the next caller.
                    from django.utils.cache import patch_vary_headers
                    patch_vary_headers(response, (
                        'Accept-Language', 'Authorization',
                        'X-App-Channel', 'X-App-Version'))
            except (json.JSONDecodeError, AttributeError):
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

        return response
    
    def _is_cacheable(self, request):
        """Cacheable only if the path is declared in the registry."""
        from training_platform.cache_config import match_route
        return match_route(request.path) is not None
    
    def _scope_for(self, request, rule):
        """
        Decide the cache identity + segment from the route's declared scope.

        public  -> one shared entry, key carries NO user identity. Only valid where the
                   response is identical for every viewer (verified per route).
        private -> per-user entry in DB3. Required whenever the view scopes its
                   queryset by request.user, e.g. /api/routine/exercises/.
        """
        if rule and rule.get('scope') == 'public':
            return 'public', False
        return self._resolve_identity(request)

    def _resolve_identity(self, request):
        """
        Return (identity, is_private).
        Authenticated (verified JWT or session) → ("user:<id>", True) → DB3.
        Anonymous → ("anon:<trusted_ip>", False) → DB2.
        """
        from rest_framework_simplejwt.tokens import AccessToken

        # 1. Verified JWT user id (DRF auth runs in the view, so do it here).
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            try:
                token = AccessToken(auth_header.split(' ', 1)[1])
                user_id = token.get('user_id')
                if user_id and self._account_is_usable(user_id):
                    return f"user:{user_id}", True
                if user_id:
                    # The token verifies but the account behind it does not. Falling
                    # through to the anonymous bucket would SERVE a public entry; make
                    # it a cache miss instead and let the view answer 401.
                    return None, True
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

        # 2. Session-authenticated fallback.
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            return f"user:{user.id}", True

        # 3. Anonymous — trusted proxy-chain IP (spoof-resistant), not raw XFF.
        return f"anon:{get_trusted_client_ip(request)}", False

    @staticmethod
    def _account_is_usable(user_id) -> bool:
        """Is the account behind this token still allowed to be used?

        `AccessToken(...)` verifies a signature and an expiry and nothing else — it
        cannot know the account was deactivated, and simplejwt has no revocation for
        access tokens. This middleware returns a cached response from the middleware
        chain, which runs BEFORE DRF authentication, so a banned account kept reading
        its exercises, templates and achievements for the full TTL while every uncached
        route correctly answered 401.

        One cheap keyed read per cached request, memoised for a minute, so a deactivation
        takes effect within a minute rather than never.
        """
        from django.contrib.auth import get_user_model

        cache_key = f"acct_usable:{user_id}"
        cached = private_cache().get(cache_key)
        if cached is not None:
            return bool(cached)
        usable = get_user_model().objects.filter(pk=user_id, is_active=True).exists()
        private_cache().set(cache_key, 1 if usable else 0, 60)
        return usable

    def _build_key(self, request, identity, rule=None):
        """
        Build a versioned, hashed cache key.
        Dimensions: identity + Accept-Language + the declared build + path + querystring.
        User-Agent is intentionally excluded (public catalog data does not vary by device).

        The build (`X-App-Channel`, `X-App-Version`) is a dimension because responses
        differ by it: a store build is never sent a plan's price (`policy/services.py`).
        Without it the catalogue cached for the direct APK, prices included, was served
        to the next iOS request for the same path — found by the test that asks for the
        list as each build in turn.
        """
        from django.utils import translation as trans
        import hashlib

        from policy.channels import VERSION_HEADER, channel_of

        qs = request.GET.urlencode()
        build = f"{channel_of(request) or '-'}@{(request.headers.get(VERSION_HEADER) or '').strip()[:17]}"
        key_parts = ['api_cache', trans.get_language() or 'en', identity, build, request.path, qs]

        # Version bucket comes from the registry entry, so it can never point at a
        # different resource than the caching rule itself (the old if/elif chain
        # matched paths that no longer existed).
        version = 1
        model_name = (rule or {}).get('model')
        if model_name:
            v_cache = public_cache().get(f"CACHE_VERSION_{model_name}")
            if v_cache:
                version = v_cache

        raw = ':'.join(filter(None, [str(p) for p in key_parts]))
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        return f"custom_cache:v{version}:{key_hash}"


class APIVersionMiddleware(MiddlewareMixin):
    """
    Handle API versioning
    """
    
    def process_request(self, request):
        """
        Set API version from header or default
        """
        api_version = request.META.get('HTTP_API_VERSION', 'v1')
        request.api_version = api_version
        return None


from django.utils import translation


class LanguageResolutionMiddleware(MiddlewareMixin):
    """
    Language resolution: Accept-Language header → JWT user preference → default.

    DRF authentication runs inside the view, not in Django middleware.
    So we do the JWT lookup ourselves in process_request.
    """

    def process_request(self, request):
        """Resolve language from header or user preference."""
        language = None

        # 1. Accept-Language header — but only a language we actually serve.
        # `get_language_from_request` never returns None: when nothing in the header is
        # supported it falls through to LANGUAGE_CODE. So ANY header — `*`, `fr-FR`, a
        # transport default — short-circuited the stored preference below, and a user
        # who chose Arabic in their profile got the whole API in English. An explicit
        # `ar` or `en` still wins, as API_CONTRACT.md documents; a header that names no
        # supported language now defers to what the user chose.
        accept_lang = request.headers.get("Accept-Language")
        if accept_lang:
            language = self._supported_language_in(accept_lang)

        # 2. Authenticated user's preferred_language via JWT
        if not language:
            language = self._get_language_from_jwt(request)

        # 3. Session-authenticated user
        if not language:
            user = getattr(request, 'user', None)
            if user and getattr(user, 'is_authenticated', False):
                user_lang = getattr(user, 'preferred_language', None)
                if user_lang:
                    language = user_lang

        # 4. Default
        if not language:
            language = settings.LANGUAGE_CODE

        translation.activate(language)
        request.LANGUAGE_CODE = translation.get_language()

    @staticmethod
    def _supported_language_in(header):
        """The first language in an Accept-Language header that we serve, or None."""
        from django.utils.translation.trans_real import parse_accept_lang_header

        for code, _quality in parse_accept_lang_header(header):
            if not code or code == "*":
                continue
            try:
                return translation.get_supported_language_variant(code)
            except LookupError:
                continue
        return None

    def _get_language_from_jwt(self, request):
        """Extract preferred_language from JWT bearer token if present."""
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return None
        try:
            from rest_framework_simplejwt.tokens import AccessToken
            from django.contrib.auth import get_user_model
            token = AccessToken(auth_header.split(' ', 1)[1])
            user_id = token.get('user_id')
            if user_id:
                User = get_user_model()
                lang = (
                    User.objects
                    .filter(id=user_id)
                    .values_list('preferred_language', flat=True)
                    .first()
                )
                return lang or None
        except Exception:
            return None
        return None

    def process_response(self, request, response):
        from django.utils.cache import patch_vary_headers
        lang = translation.get_language() or settings.LANGUAGE_CODE
        response['Content-Language'] = lang
        patch_vary_headers(response, ['Accept-Language', 'Cookie'])
        return response


# Middleware order is important for proper functionality
MIDDLEWARE_ORDER = [
    'training_platform.middleware.SecurityHeadersMiddleware',
    'training_platform.middleware.RateLimitMiddleware',
    'training_platform.middleware.RequestLoggingMiddleware',
    'training_platform.middleware.LanguageResolutionMiddleware',
    'training_platform.middleware.DatabaseQueryCountMiddleware',
    'training_platform.middleware.CacheMiddleware',
    'training_platform.middleware.APIVersionMiddleware',
    'training_platform.middleware.ErrorHandlingMiddleware',
] 

class RequestSizeLimitMiddleware:
    """Reject oversized request bodies before Django buffers them to disk.

    Nothing in this stack caps request size: there is no reverse proxy in front of
    Daphne (no nginx `client_max_body_size`), and Django's MultiPartParser streams the
    ENTIRE body into FILE_UPLOAD_TEMP_DIR before any view runs — so the per-file size
    check in `process_uploaded_image` only fires once the bytes are already on disk.
    On a 1 GB Fly volume a single large POST fills the machine.

    Content-Length is client-supplied, but a client that lies low still gets stopped by
    the per-file checks downstream; this guard exists to stop the disk write, not to be
    the only size control.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.max_bytes = getattr(settings, 'MAX_REQUEST_BODY_BYTES', 15 * 1024 * 1024)
        # Named paths with their own, larger ceiling. Video does not fit under 15 MB and
        # raising the cap for the whole API to suit one route would remove the guard
        # everywhere. An exact path match, never a prefix: a prefix would hand the
        # bigger ceiling to every route underneath it, including ones added later.
        self.path_max_bytes = dict(getattr(settings, 'LARGE_UPLOAD_PATHS', {}) or {})

    def _cap_for(self, request):
        return self.path_max_bytes.get(request.path, self.max_bytes)

    def __call__(self, request):
        if request.method in ('POST', 'PUT', 'PATCH'):
            max_bytes = self._cap_for(request)
            raw_length = request.META.get('CONTENT_LENGTH')
            try:
                length = int(raw_length) if raw_length not in (None, '') else None
            except (TypeError, ValueError):
                length = None

            # A body with NO declared length is the case this cap missed entirely.
            # `Transfer-Encoding: chunked` sends no Content-Length, the old code read
            # that as `0`, and `0 > max_bytes` is never true — so the only body-size
            # control in the stack (there is no nginx in front of Daphne) was skipped by
            # setting one header. Django spools the whole body to FILE_UPLOAD_TEMP_DIR
            # before any middleware or per-file check runs, and in production that is
            # `/data/tmp` on the same 1 GB volume as all user media: a 400 MB chunked
            # POST was measured buffering past 368 MB against a 15 MB cap.
            #
            # We cannot know a chunked body's size in advance, so we refuse it. Every
            # client this API has sends a length and nothing here needs streaming.
            # A missing Content-Length WITHOUT chunked encoding means no body at all
            # (HTTP says so), which is an ordinary empty POST and must still pass.
            encoding = (request.META.get('HTTP_TRANSFER_ENCODING') or '').lower()
            if 'chunked' in encoding:
                logger.warning(
                    "Rejected %s %s: chunked body, no declared Content-Length",
                    request.method, request.path)
                message = 'A request body must declare its Content-Length.'
                return JsonResponse(
                    {
                        'error': message,
                        'detail': message,
                        'code': 'length_required',
                        'status': 411,
                    },
                    status=411,
                )

            if length is not None and length > max_bytes:
                logger.warning(
                    "Rejected %s %s: body %d bytes exceeds cap %d",
                    request.method, request.path, length, max_bytes,
                )
                # The documented envelope. This middleware is FIRST, so it short-circuits
                # before `ErrorHandlingMiddleware` is ever entered and
                # `normalize_error_envelope` cannot reach it — and it returns a
                # JsonResponse, which that function skips anyway. A 413 carried no `code`
                # and no `detail` while the contract says every error has both.
                message = (f'Request body exceeds '
                           f'{max_bytes // (1024 * 1024)} MB.')
                return JsonResponse(
                    {
                        'error': message,
                        'detail': message,
                        'code': 'payload_too_large',
                        'status': 413,
                    },
                    status=413,
                )
        return self.get_response(request)


class AdminLoginThrottleMiddleware(MiddlewareMixin):
    """Cap password attempts against Django's own admin login form.

    `django.contrib.admin` is mounted at /admin/ alongside the custom dashboard at
    /dj-admin/, and its login view has no attempt limit: Django counts nothing, locks
    nothing, and answers a wrong password as fast as it can. The OTP flow locks a record
    after 5 attempts and password reset is capped at 3/hour per email; this was the one
    login page in the product with neither, guarding accounts that can reverse
    transactions and read the audit log.

    Keyed on username + trusted client IP, hashed, in the rate-limit Redis DB — the same
    shape as the OTP resend limit, so a shared carrier NAT address cannot lock out an
    unrelated administrator and a rotating IP cannot escape the username's own counter.
    Only failures count: `process_response` clears the counter on the redirect that a
    successful login returns.
    """

    LIMIT = 5
    WINDOW = 900  # 15 minutes, matching the OTP lockout the product already uses.

    def _keys(self, request):
        username = (request.POST.get("username") or "").strip().lower()[:150]
        ip = get_trusted_client_ip(request)
        import hashlib
        by_user = hashlib.sha256(f"admin:{username}".encode()).hexdigest()[:32]
        by_ip = hashlib.sha256(f"admin:{ip}".encode()).hexdigest()[:32]
        return [f"rl:admin_login:u:{by_user}", f"rl:admin_login:i:{by_ip}"]

    #: Every admin login page in the product. `/dj-admin/` is a full AdminSite subclass
    #: authenticating the SAME staff and superuser accounts, and its session is valid on
    #: both sites — but this matched `/admin/` only, so the throttle added for exactly
    #: these accounts was defeated by attacking its sibling path.
    LOGIN_PATHS = ("/admin/login/", "/admin/", "/dj-admin/login/", "/dj-admin/")

    def _is_admin_login(self, request):
        return request.method == "POST" and request.path in self.LOGIN_PATHS

    def process_request(self, request):
        if not self._is_admin_login(request):
            return None
        rl = ratelimit_cache()
        for key in self._keys(request):
            if (rl.get(key) or 0) >= self.LIMIT:
                logger.warning("admin login lockout on %s", key)
                message = _('Too many failed sign-in attempts. Try again in 15 minutes.')
                return JsonResponse(
                    {'error': message, 'detail': message, 'code': 'rate_limited'},
                    status=429, headers={'Retry-After': str(self.WINDOW)})
        return None

    def process_response(self, request, response):
        if not self._is_admin_login(request):
            return response
        rl = ratelimit_cache()
        # Django's admin answers a successful login with a redirect and re-renders the
        # form (200) on failure. Anything else is not an outcome we can read, so it is
        # left alone rather than counted as either.
        if response.status_code in (301, 302):
            for key in self._keys(request):
                rl.delete(key)
        elif response.status_code == 200:
            for key in self._keys(request):
                try:
                    rl.incr(key)
                except ValueError:
                    rl.set(key, 1, self.WINDOW)
        return response
