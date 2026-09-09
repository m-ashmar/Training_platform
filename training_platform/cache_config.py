"""
training_platform/cache_config.py — the single source of truth for HTTP response caching.

Why this file exists
--------------------
Caching used to be described in two places that silently drifted apart:
`CacheMiddleware.cacheable_paths` decided WHAT to cache, and a separate if/elif chain
decided WHICH version counter to read. Four of the six configured paths pointed at
routes that did not exist (`/api/exercises/` instead of `/api/routine/exercises/`,
`/api/subscription/plans/` instead of `/api/subscription/v1/plans/`, …), so the cache
never engaged for them — while `signals.py` dutifully incremented version counters that
nothing read.

Everything is now declared once, here, and consumed by both the middleware and the
invalidation signals. A path that is not in this registry is not cached.

Scope
-----
``public``  The response is byte-identical for every viewer (a global catalog). The
            cache key omits user identity, so all users share one entry — this is where
            the real hit-rate comes from. ONLY use it for endpoints whose queryset does
            not depend on request.user.

``private`` The response varies per user. The key includes the user id and the entry
            lives in the private Redis segment (DB3). Use this whenever the view scopes
            its queryset by the caller — e.g. /api/routine/exercises/ returns different
            rows to clients, trainers and admins.

Invalidation
------------
Each entry names the model whose writes make it stale. `training_platform/signals.py`
bumps `CACHE_VERSION_<MODEL>` on save/delete of that model; the version is part of the
cache key, so a bump instantly orphans every stale entry with no key scanning.
"""

# path prefix -> {scope, model, ttl}
#
# A cached response is returned from `process_request`, BEFORE the view and therefore
# before its permission classes. So a route may only appear here if the cache key already
# carries everything that decides who may see the response.
#
# `scope: private` puts the user id in the key, which covers "whose rows are these". It
# does NOT cover "may this user still have them": an entitlement can lapse — a
# subscription expires, is cancelled, is refunded — and the key does not change, so the
# client keeps being served the paid response for the rest of the TTL. That is why the two
# diet food routes are NOT here any more. They were, at 600 s and 3600 s, behind
# `HasDietAccess`; a lapsed subscriber kept the catalogue for up to an hour. Serving them
# from the view costs four or five queries, which is the right price.
#
# The remaining four are gated on identity and role alone, both of which are in the key or
# fixed for the life of the token.
CACHEABLE_ROUTES = {
    # ---- Global catalog, no entitlement: identical for every viewer -> shared entry ----
    "/api/subscription/v1/plans/":    {"scope": "public",  "model": "SUBSCRIPTIONPLAN", "ttl": 600},

    # ---- User-scoped: queryset depends on the caller -> per-user entry in DB3 ----
    # /api/routine/exercises/ returns different rows per role (global exercises, the
    # caller's own, their trainer's, ones in their assigned routines) — it MUST be
    # private or one user's catalog would be served to another.
    "/api/routine/exercises/":        {"scope": "private", "model": "EXERCISE",         "ttl": 300},
    # Trainers see their own templates plus public ones from others.
    "/api/routine/templates/":        {"scope": "private", "model": "ROUTINETEMPLATE",  "ttl": 300},
    "/api/achievements/":             {"scope": "private", "model": "ACHIEVEMENT",      "ttl": 300},
}

# Model class name -> version counter name. Derived from the registry so the two can
# never disagree.
VERSIONED_MODELS = {entry["model"].title().replace("_", ""): entry["model"]
                    for entry in CACHEABLE_ROUTES.values()}
# Explicit map, keyed by `Model._meta.label` -> version key.
#
# The bare class name is ambiguous: `Achievement` is a model in `achievements` AND in
# `social`, and `/api/achievements/` is served by the first. Keyed by class name, a write
# to a social achievement flushed the cache for a route it has nothing to do with.
MODEL_VERSION_KEYS = {
    "subscription.SubscriptionPlan": "SUBSCRIPTIONPLAN",
    "routine.Exercise": "EXERCISE",
    "routine.RoutineTemplate": "ROUTINETEMPLATE",
    # All three, not just the catalogue. `/api/achievements/` and its `my`, `progress` and
    # `leaderboard` sub-paths return the caller's OWN state, built from `UserAchievement`
    # and `AchievementProgress`; versioning on the static `Achievement` catalogue meant a
    # user earned a badge, got the push, opened the screen and it was not there for up to
    # five minutes — including their own progress bars.
    "achievements.Achievement": "ACHIEVEMENT",
    "achievements.UserAchievement": "ACHIEVEMENT",
    "achievements.AchievementProgress": "ACHIEVEMENT",
}


def match_route(path: str):
    """Return the cache rule for `path`, or None if the path is not cacheable.

    Longest prefix wins so a more specific rule can override a broader one.
    """
    best = None
    best_len = -1
    for prefix, rule in CACHEABLE_ROUTES.items():
        if path.startswith(prefix) and len(prefix) > best_len:
            best, best_len = rule, len(prefix)
    return best
