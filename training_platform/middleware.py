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
                'client': {'requests': 500, 'window': 3600},
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
            _msg = _('Too many requests. Please try again later.')
            return JsonResponse({
                'detail': _msg,
                'error': _msg,
                'code': 'rate_limited',
                'message': _msg,          # kept: existing key
                'retry_after': 3600,
            }, status=429, headers={'Retry-After': '3600'})
        
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
            '/admin/',
            '/static/',
            '/media/',
        ]
        return any(request.path.startswith(path) for path in skip_paths)
    
    def _get_client_id(self, request):
        """
        Get unique client identifier
        """
        # Use user ID if authenticated, otherwise IP address
        if hasattr(request, 'user') and request.user.is_authenticated:
            return f"user_{request.user.id}"
        return f"ip_{self._get_client_ip(request)}"
    
    def _get_client_ip(self, request):
        """
        Get client IP address from the trusted proxy chain (spoof-resistant).
        """
        return get_trusted_client_ip(request)
    
    def _get_user_type(self, request):
        """
        Get user type for rate limiting
        """
        if hasattr(request, 'user') and request.user.is_authenticated:
            return getattr(request.user, 'user_type', 'client')
        return 'anonymous'
    
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


def normalize_error_envelope(response):
    """
    Give every error response ONE shape.

    DRF emits {"detail": ...} for 401/403/404/405 while the project's own views emit
    {"error": ...}. A mobile client had to branch on both. This adds the missing key
    as a mirror (keeping the original for backward compatibility) so clients can read
    `error` everywhere.
    """
    try:
        if not (400 <= response.status_code < 600):
            return response
        data = getattr(response, 'data', None)
        if not isinstance(data, dict):
            return response
        if 'error' not in data and 'detail' in data:
            data['error'] = data['detail']
            if hasattr(response, '_is_rendered'):
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
        
        return JsonResponse(error_response, status=error_response['status'])
    
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
            cache_key = self._build_key(request, identity, rule)
            cache_backend = private_cache() if is_private else public_cache()
            cached_response = cache_backend.get(cache_key)
            if cached_response is not None:
                logger.debug(f"Cache hit for {request.path} (from {'private' if is_private else 'public'} cache)")
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
            cache_key = self._build_key(request, identity, rule)

            try:
                # Only cache JSON responses
                if response.get('Content-Type', '').startswith('application/json'):
                    response_data = json.loads(response.content)
                    backend = private_cache() if is_private else public_cache()
                    backend.set(cache_key, response_data, rule['ttl'])
                    logger.debug(f"Cached response for {request.path} into {'private' if is_private else 'public'} cache")
                    # Inform CDN / proxies of dimensions that affect this response
                    response['Vary'] = 'Accept-Language, Authorization'
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
                if user_id:
                    return f"user:{user_id}", True
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

    def _build_key(self, request, identity, rule=None):
        """
        Build a versioned, hashed cache key.
        Dimensions: identity + Accept-Language + path + querystring. User-Agent is
        intentionally excluded (public catalog data does not vary by device).
        """
        from django.utils import translation as trans
        import hashlib

        qs = request.GET.urlencode()
        key_parts = ['api_cache', trans.get_language() or 'en', identity, request.path, qs]

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

        # 1. Accept-Language header
        accept_lang = request.headers.get("Accept-Language")
        if accept_lang:
            language = translation.get_language_from_request(request)

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

    def __call__(self, request):
        if request.method in ('POST', 'PUT', 'PATCH'):
            length = request.META.get('CONTENT_LENGTH') or 0
            try:
                length = int(length)
            except (TypeError, ValueError):
                length = 0
            if length > self.max_bytes:
                logger.warning(
                    "Rejected %s %s: body %d bytes exceeds cap %d",
                    request.method, request.path, length, self.max_bytes,
                )
                return JsonResponse(
                    {
                        'error': 'request_too_large',
                        'message': f'Request body exceeds {self.max_bytes // (1024 * 1024)} MB.',
                        'status': 413,
                    },
                    status=413,
                )
        return self.get_response(request)
