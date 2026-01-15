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
from django.conf import settings
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
        # Relax limits in development to avoid throttling during testing
        if getattr(settings, 'WALLET_DEV_MODE', False) or settings.DEBUG:
            self.rate_limits = {
                'anonymous': {'requests': 1000000, 'window': 60},
                'client': {'requests': 1000000, 'window': 60},
                'trainer': {'requests': 1000000, 'window': 60},
                'admin': {'requests': 1000000, 'window': 60},
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
                'message': 'Too many requests. Please try again later.',
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
        Check if client is rate limited
        """
        limits = self.rate_limits.get(user_type, self.rate_limits['anonymous'])
        
        # Use Redis for rate limiting if available
        cache_key = f"rate_limit:{client_id}"
        
        try:
            # Get current request count
            current_requests = cache.get(cache_key, 0)
            
            # Check if limit exceeded
            if current_requests >= limits['requests']:
                return True
            
            # Increment counter
            cache.set(cache_key, current_requests + 1, limits['window'])
            return False
            
        except Exception as e:
            logger.error(f"Rate limiting error: {e}")
            # Fail open - don't block requests if rate limiting fails
            return False


class ErrorHandlingMiddleware(MiddlewareMixin):
    """
    Standardized error handling middleware
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
        
        if isinstance(exception, ValidationError):
            return {
                'error': 'validation_error',
                'message': 'Invalid input data',
                'details': (exception.message_dict 
                           if hasattr(exception, 'message_dict') 
                           else str(exception)),
                'status': 400
            }
        elif isinstance(exception, PermissionDenied):
            return {
                'error': 'permission_denied',
                'message': 'You do not have permission to perform this action',
                'status': 403
            }
        elif isinstance(exception, Http404):
            return {
                'error': 'not_found',
                'message': 'The requested resource was not found',
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
                'message': 'An unexpected error occurred. Please try again later.',
                'status': 500
            }


class SecurityHeadersMiddleware(MiddlewareMixin):
    """
    Add security headers to all responses
    """
    
    def process_response(self, request, response):
        """
        Add security headers to response
        """
        # Security headers
        response['X-Content-Type-Options'] = 'nosniff'
        response['X-Frame-Options'] = 'DENY'
        response['X-XSS-Protection'] = '1; mode=block'
        response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        
        # Content Security Policy
        if not settings.DEBUG:
            response['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "connect-src 'self'"
            )
        
        return response

    def process_request(self, request):
        """
        If SECURE_SSL_REDIRECT is enabled in prod, allow exemptions for API paths to ease local testing
        when requests are made over HTTP.
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
        # Expanded list of cacheable paths
        self.cacheable_paths = [
            '/api/food/categories/',
            '/api/exercises/',
            '/api/subscription/plans/',
            '/api/food/',  # Food list (frequently accessed)
            '/api/diet/templates/',  # Diet plan templates (mostly static)
            '/api/routine/templates/',  # Routine templates
        ]
        self.cache_duration = 300  # 5 minutes
    
    def process_request(self, request):
        """
        Check if response is cached
        """
        if request.method == 'GET' and self._is_cacheable(request):
            cache_key = self._get_cache_key(request)
            cached_response = cache.get(cache_key)
            
            if cached_response:
                logger.debug(f"Cache hit for {request.path}")
                return JsonResponse(cached_response)
        
        return None
    
    def process_response(self, request, response):
        """
        Cache successful GET responses
        """
        if (request.method == 'GET' and 
            response.status_code == 200 and 
            self._is_cacheable(request)):
            
            cache_key = self._get_cache_key(request)
            
            try:
                # Only cache JSON responses
                if response.get('Content-Type', '').startswith('application/json'):
                    response_data = json.loads(response.content)
                    cache.set(cache_key, response_data, self.cache_duration)
                    logger.debug(f"Cached response for {request.path}")
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
        Generate cache key for request
        """
        key_parts = [
            'api_cache',
            request.path,
            request.GET.urlencode()
        ]
        return ':'.join(filter(None, key_parts))


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


# Middleware order is important for proper functionality
MIDDLEWARE_ORDER = [
    'training_platform.middleware.SecurityHeadersMiddleware',
    'training_platform.middleware.RateLimitMiddleware',
    'training_platform.middleware.RequestLoggingMiddleware',
    'training_platform.middleware.DatabaseQueryCountMiddleware',
    'training_platform.middleware.CacheMiddleware',
    'training_platform.middleware.APIVersionMiddleware',
    'training_platform.middleware.ErrorHandlingMiddleware',
] 