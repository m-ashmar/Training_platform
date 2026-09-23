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
    # ---- User-scoped: queryset depends on the caller -> per-user entry in DB3 ----
    # The plan catalogue WAS `public`, one shared entry, because the rows were the same
    # for everyone and only the BUILD changed the response (which is in the key). It is
    # not any more: since the owner made both currencies first class (2026-09-21) every
    # price is converted into the READER'S own currency (`subscription.serializers
    # .PlanPriceInReadersCurrency`), so a shared entry serves one person's pounds to the
    # next person's dollars. Measured by the price-list test in
    # tests/test_two_currencies.py, which asks for the list as a pound reader and then
    # as a dollar reader and got the pound answer twice.
    #
    # Private is enough here and nothing more is needed: the currency is a property of
    # the user, the user id is in the key, and a person's currency cannot change once
    # their wallet has been used. While it still can, the wallet is empty and they have
    # bought nothing — a stale catalogue for up to the TTL costs them a price shown in
    # the currency they just left, and the checkout charges in the new one regardless.
    "/api/subscription/v1/plans/":    {"scope": "private", "model": "SUBSCRIPTIONPLAN", "ttl": 600},


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
    # The plan list is not a function of the plan rows alone. Every price on it is
    # converted at the operator's rate, so a new rate left every reader the old figure
    # for up to ten minutes — and POST gateways/ then answered 409 "the price has
    # changed" while a refresh returned the same cached number (SIDE_BUGS S-078).
    # `ExchangeRate` is append-only, so this is one bump per rate the operator sets.
    "wallet.ExchangeRate": "SUBSCRIPTIONPLAN",
    # And whether a build may see a price at all is a `ChannelCapabilities` row: an
    # operator withdrawing prices from a store build left that build receiving them
    # for the rest of the TTL (SIDE_BUGS S-139). The same counter is the home
    # screen's policy generation (`home/views.py`), whose price-gated sections have the
    # same dependency.
    "policy.ChannelCapabilities": "SUBSCRIPTIONPLAN",
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
    # An exercise's media no longer lives on the exercise row. `/api/routine/exercises/`
    # is cached for five minutes and now carries `media_slots`, so without this a coach
    # uploaded their demonstration, opened the app and saw the old one — for up to five
    # minutes, on their own upload. Blunt on purpose: a `MediaAsset` write for a trainer
    # portrait or an app banner bumps this counter too. That costs a re-fetch of one
    # cached route; the alternative, filtering by owner type here, would put a second
    # copy of the slot vocabulary in the cache layer.
    "mediakit.MediaAsset": "EXERCISE",
    # The same hole, left open for the LEGACY media model. `/api/routine/exercises/` is
    # prefix-matched, so the list, the detail and `exercises/{id}/add-media/` GET are
    # all cached here — and they serve `media[]`, `thumbnail` and `media_items[]` from
    # `ExerciseMedia`, which bumped nothing. A coach added or deleted a tip or a photo
    # through `add-media/` and saw the old set for up to five minutes (SIDE_BUGS S-138).
    "routine.ExerciseMedia": "EXERCISE",
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
