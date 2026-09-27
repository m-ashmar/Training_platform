"""
training_platform/cache.py — Centralized cache accessor helpers.

Use these instead of `from django.core.cache import cache` everywhere.
This ensures the correct Redis DB segment is used for each data type,
and gracefully falls back to 'default' in environments that only define it
(e.g., local dev with LocMemCache).

Redis DB segmentation (production):
    DB0 — sessions        → caches['default']
    DB1 — rate limiting   → caches['ratelimit']
    DB2 — public cache    → caches['public']
    DB3 — private cache   → caches['private']
    DB4 — Edamam API      → caches['edamam']
    DB5 — channel layer   → channels_redis (not Django CACHES)
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


def ops_cache():
    """DB0 — operational markers shared by every process: the background heartbeat and
    the last success of each watched beat job (`training_platform/background_health.py`).

    A handful of tiny keys. It has to be a cache every machine reads — the worker writes
    them and a web machine answers the monitor — and one that does NOT ignore Redis
    errors, so a Redis that is down reads as "unreachable" rather than as "no heartbeat".
    """
    return _get('default')


def seconds_left(cache, key: str, default: int) -> int:
    """Seconds until `key` expires in `cache`, or `default` when the backend cannot say
    (the in-memory cache in tests has no `ttl`) or the key has no expiry. For the
    `Retry-After` of a throttle: the window's real remainder, not its full length."""
    try:
        ttl = cache.ttl(key)
    except Exception:
        ttl = None
    return int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else int(default)
