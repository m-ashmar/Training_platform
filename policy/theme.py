"""Resolving the remote theme for a build, and keeping the launch fetch cheap.

The only inputs are the channel and the version the build declares about itself. There
is no `request` in any signature here, and that is the point: a function that cannot
see the user cannot answer two users of one build differently.

The schedule needs saying out loud, because "a Ramadan skin" and "the answer never
depends on the date" sound like a contradiction and are not:

* `starts_at` and `ends_at` are the OPERATOR's decision, stored on the row and applied
  by the server's own clock. They are the same kind of thing as an operator opening the
  console at midnight and switching the row on by hand — the server just does not need
  anyone awake to do it.
* What the store rule forbids is behaviour that differs BETWEEN users of one build:
  by account, country, IP, device clock, experiment bucket, or a "review mode". None of
  those is readable from here. At any instant every build on a channel and version gets
  byte-identical bytes, and when the window closes it closes for all of them at once.
* The CLIENT's date is never consulted. `valid_until` is published so the app can drop
  a cached skin on its own, but the server's answer does not change because a phone's
  clock says Ramadan.

The cache respects that: an entry never outlives the next schedule boundary, so the
moment a window opens or closes is the moment the answer changes for everybody.
"""
from __future__ import annotations

import hashlib
import json

from django.utils import timezone

from training_platform.cache import public_cache

#: How long a resolved theme may be served from cache, when no schedule boundary comes
#: sooner. Short: this is fetched once at launch, so the hit rate comes from the herd
#: of launches in a minute, not from a long tail.
CACHE_TTL_SECONDS = 300

_GENERATION_KEY = "policy:theme:generation"


def _generation() -> int:
    """A counter every cache key carries, bumped whenever an operator saves a row.

    Cheaper and safer than deleting keys: the version part of a key is unbounded, so
    there is nothing to enumerate. A cache flush loses the counter and the entries
    together, which is consistent by construction.
    """
    cache = public_cache()
    value = cache.get(_GENERATION_KEY)
    if value is None:
        value = 1
        cache.set(_GENERATION_KEY, value, None)
    return value


def bump_generation() -> None:
    """Orphan every cached theme. Called on commit of any `ThemeOverride` write."""
    cache = public_cache()
    try:
        cache.incr(_GENERATION_KEY)
    except (ValueError, AttributeError):
        # No counter yet (or a backend without `incr`): start one. Overshooting is
        # harmless — the key only has to CHANGE.
        cache.set(_GENERATION_KEY, (cache.get(_GENERATION_KEY) or 0) + 1, None)


def _digest(body: dict) -> str:
    """A short, stable fingerprint of the answer. Changes if and only if it does."""
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _payload(row) -> dict:
    """The response body, in the order the app reads it."""
    tokens = dict(row.tokens) if row is not None else {}
    valid_until = (row.ends_at.isoformat()
                   if row is not None and row.ends_at else None)
    identity = row.pk if row is not None else None
    version = _digest({"id": identity, "tokens": tokens, "valid_until": valid_until})
    return {"id": identity, "version": version,
            "valid_until": valid_until, "tokens": tokens}


def _sort_key(row):
    """Highest `priority` wins; then the one that started most recently; then the
    newest row. Total and deterministic, so two servers never disagree."""
    started = row.starts_at.timestamp() if row.starts_at else float("-inf")
    return (row.priority, started, row.pk)


def theme_for(channel: str | None, version: tuple | None, now=None) -> dict:
    """The override a build should apply right now, or the empty one.

    Takes a channel, a version and the server's clock, and NOTHING else. A caller that
    declares no known channel gets the empty override — the same "look, do not change"
    answer `policy.services.STRICTEST` gives for capabilities.
    """
    from .models import ThemeOverride

    if channel is None:
        return _payload(None)
    version = version or (0, 0, 0)
    now = now or timezone.now()
    matching = [row for row in ThemeOverride.objects.filter(is_enabled=True)
                if row.applies_to(channel, version, now)]
    return _payload(max(matching, key=_sort_key) if matching else None)


def _seconds_to_next_boundary(now) -> int | None:
    """Seconds until the earliest `starts_at`/`ends_at` still ahead of `now`.

    Without this an entry written a second before a window closes would keep serving
    last season's skin for the rest of its TTL — behaviour that depends on when a
    server happened to fill its cache, which is exactly what must not happen.
    """
    from .models import ThemeOverride

    ahead = []
    for row in ThemeOverride.objects.filter(is_enabled=True):
        for moment in (row.starts_at, row.ends_at):
            if moment is not None and moment > now:
                ahead.append(moment)
    if not ahead:
        return None
    return max(1, int((min(ahead) - now).total_seconds()))


def cached_theme_for(channel: str | None, version: tuple | None) -> dict:
    """`theme_for`, memoised per (channel, version). What the endpoint calls."""
    if channel is None:
        return _payload(None)
    cache = public_cache()
    stamp = ".".join(str(part) for part in (version or (0, 0, 0)))
    key = f"policy:theme:{_generation()}:{channel}:{stamp}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    now = timezone.now()
    payload = theme_for(channel, version, now=now)
    boundary = _seconds_to_next_boundary(now)
    ttl = CACHE_TTL_SECONDS if boundary is None else min(CACHE_TTL_SECONDS, boundary)
    cache.set(key, payload, ttl)
    return payload


def etag_for(payload: dict) -> str:
    """The `ETag` header for a payload. Strong: the body is byte-identical per version."""
    return f'"{payload["version"]}"'
