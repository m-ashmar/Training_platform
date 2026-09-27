"""
training_platform/cache.py — Centralized cache accessor helpers.

Use these instead of `from django.core.cache import cache` everywhere.
This ensures the correct Redis DB segment is used for each data type,
and gracefully falls back to 'default' in environments that only define it
(e.g., local dev with LocMemCache).

Redis DB segmentation (production, on REDIS_URL alone):
    DB0 — sessions        → caches['default']
    DB1 — rate limiting   → caches['ratelimit']
    DB2 — public cache    → caches['public']
    DB3 — private cache   → caches['private']
    DB4 — Edamam API      → caches['edamam']
    DB5 — channel layer   → channels_redis (not Django CACHES)
With REDIS_CACHE_URL set, the five caches share the one database it names and are kept
apart by their KEY_PREFIX (training_platform/redis_layout.py).
"""
from django.core.cache import caches


def _get(alias: str):
    """Get a named cache backend, falling back to 'default' if not configured."""
    from django.core.cache import caches as _caches
    try:
        return _caches[alias]
    except Exception:
        return _caches['default']


def public_cache():
    """DB2 — public/role-invariant API response cache."""
    return _get('public')


def private_cache():
    """DB3 — per-user private response cache."""
    return _get('private')


def ratelimit_cache():
    """DB1 — rate limiting counters. Must NOT be shared with sessions."""
    return _get('ratelimit')


def edamam_cache():
    """DB4 — Edamam API response cache (24h TTL)."""
    return _get('edamam')


def seconds_left(cache, key: str, default: int) -> int:
    """Seconds until `key` expires in `cache`, or `default` when the backend cannot say
    (the in-memory cache in tests has no `ttl`) or the key has no expiry. For the
    `Retry-After` of a throttle: the window's real remainder, not its full length."""
    try:
        ttl = cache.ttl(key)
    except Exception:
        ttl = None
    return int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else int(default)
