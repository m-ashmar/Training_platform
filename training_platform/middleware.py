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
from training_platform.cache import ratelimit_cache, public_cache
from django.db import connection, reset_queries
from django.utils import timezone
from django.shortcuts import redirect
import re

logger = logging.getLogger(__name__)


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
                'anonymous': {'requests': 100, 'window': 3600},
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
            return JsonResponse({
                'error': 'Rate limit exceeded',
                'message': _('Too many requests. Please try again later.'),
                'retry_after': 3600
            }, status=429)
        
        return None
    
    def _should_skip(self, request):
        """
        Skip rate limiting for certain paths
        """
        skip_paths = [
            '/health/',
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
        Get client IP address
        """
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', 'unknown')
    
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


class ErrorHandlingMiddleware(MiddlewareMixin):
    """
    Standardized error handling middleware with i18n support.
    """
    
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
    
    def __init__(self, get_response=None):
        super().__init__(get_response)
        self.start_time = None
    
    def process_request(self, request):
        """
        Log request start and set start time
        """
        self.start_time = time.time()
        request.id = self._generate_request_id()
        
        # Log request details
        logger.info(f"Request {request.id}: {request.method} {request.path}")
        
        return None
    
    def process_response(self, request, response):
        """
        Log response details and performance metrics
        """
        if self.start_time:
            duration = time.time() - self.start_time
            
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
        """
        Reset query count (only in DEBUG)
        """
        if not self.enabled:
            return None
            
        try:
            reset_queries()
        except Exception:
            try:
                connection.queries.clear()
            except Exception:
                pass
        return None
    
    def process_response(self, request, response):
        """
        Log query count and slow queries (only in DEBUG)
        """
        if not self.enabled:
            return response
            
        query_count = len(connection.queries)
        
        # Log high query count
        if query_count > 20:
            logger.warning(
                f"High query count for {request.path}: {query_count} queries"
            )
        
        # Log slow queries
        for query in connection.queries:
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
    
    def __init__(self, get_response=None):
        super().__init__(get_response)
        # Expanded list of cacheable paths — now securely routes dynamically
        # to DB2 (public) or DB3 (private) based on user identity.
        self.cacheable_paths = [
            '/api/food/categories/',
            '/api/exercises/',
            '/api/subscription/plans/',
            '/api/food/',  # Food list (public)
            '/api/achievements/',
            '/api/routine/templates/',
        ]
        self.cache_duration = 300  # 5 minutes
    
    def process_request(self, request):
        """
        Check if response is cached. Uses DB2 (public) for anon, DB3 (private) for auth.
        """
        if request.method == 'GET' and self._is_cacheable(request):
            cache_key = self._get_cache_key(request)
            
            # Determine correct cache segment based on key prefix
            if cache_key.startswith("custom_cache:"):
                # Extract identity part from cache key (typically last segment)
                if ":user:" in cache_key:
                    cache_backend = private_cache()
                else:
                    cache_backend = public_cache()
                    
                cached_response = cache_backend.get(cache_key)

                if cached_response is not None:
                    logger.debug(f"Cache hit for {request.path} (from {'private' if ':user:' in cache_key else 'public'} cache)")
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

            cache_key = self._get_cache_key(request)

            try:
                # Only cache JSON responses
                if response.get('Content-Type', '').startswith('application/json'):
                    response_data = json.loads(response.content)
                    
                    # Route to correct segment
                    if ":user:" in cache_key:
                        private_cache().set(cache_key, response_data, self.cache_duration)
                        logger.debug(f"Cached response for {request.path} into private_cache")
                    else:
                        public_cache().set(cache_key, response_data, self.cache_duration)
                        logger.debug(f"Cached response for {request.path} into public_cache")
                        
                    # Inform CDN / proxies of dimensions that affect this response
                    response['Vary'] = 'Accept-Language, Authorization'
            except (json.JSONDecodeError, AttributeError):
                pass

        return response
    
    def _is_cacheable(self, request):
        """
        Check if request is cacheable
        """
        return any(request.path.startswith(path) for path in self.cacheable_paths)
    
    def _get_cache_key(self, request):
        """
        Generate cache key for request.

        Dimensions included:
        - user identity (JWT user_id or anon:{ip}) — prevents cross-user leakage
        - Accept-Language — i18n safety
        - path + querystring — distinct paginated endpoints get distinct keys

        User-Agent intentionally excluded: public food/exercise data does not
        differ by device; including it creates one entry per browser version
        and destroys cache hit rate.
        """
        from django.utils import translation as trans
        from rest_framework_simplejwt.tokens import AccessToken

        user_identity = "anon"

        # 1. Try to extract user ID from JWT Token.
        # DRF authenticates in the view, so request.user is usually AnonymousUser here.
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            try:
                token_str = auth_header.split(' ', 1)[1]
                token = AccessToken(token_str)
                user_id = token.get('user_id')
                if user_id:
                    user_identity = f"user:{user_id}"
            except Exception:
                pass

        # 2. Check session authentication as fallback
        if user_identity == "anon" and hasattr(request, 'user') and request.user.is_authenticated:
            user_identity = f"user:{request.user.id}"

        # 3. Handle Anonymous - Use IP to prevent cache poisoning across clients
        if user_identity == "anon":
            client_ip = (
                request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                or request.META.get('REMOTE_ADDR', 'unknown')
            )
            user_identity = f"anon:{client_ip}"

        qs = request.GET.urlencode()
        key_parts = [
            'api_cache',
            trans.get_language() or 'en',
            user_identity,
            request.path,
            qs,
        ]
        
        # Determine global version based on route prefix
        version = 1
        model_name = ""
        if "/api/exercises/" in request.path:
            model_name = "EXERCISE"
        elif "/api/achievements/" in request.path:
            model_name = "ACHIEVEMENT"
        elif "/api/subscription/plans/" in request.path:
            model_name = "SUBSCRIPTIONPLAN"
        elif "/api/routine/templates/" in request.path:
            model_name = "ROUTINETEMPLATE"
            
        if model_name:
            v_cache = public_cache().get(f"CACHE_VERSION_{model_name}")
            if v_cache:
                version = v_cache

        import hashlib
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