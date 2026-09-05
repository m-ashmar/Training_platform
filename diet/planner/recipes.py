"""Dish assembly.

Turns a macro target into a recognisable meal instead of a list of foods that happens to
add up. The planner's component fill stays as the fallback, because a recipe library
will never cover every target — but when a dish fits, a dish is what the user gets.
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .optimize import Components, _better, refine, totals_of
from .policy import PlannerPolicy
from .report import deviation_of
# One temperature for both paths. templates does not import this module, so it is safe.
from .templates import SOFTMAX_T

logger = logging.getLogger(__name__)


@dataclass
class RecipeMatch:
    recipe: object
    components: Components
    servings: float
    deviation: object

    @property
    def name(self) -> str:
        return getattr(self.recipe, "name", "meal")


def _recipe_lines(recipe) -> List[Tuple[object, float, bool]]:
    """A recipe's ingredients as plain data, read from the prefetched rows.

    `recipe.ingredients.select_related("food")` looked like an optimisation and was the
    opposite: adding `select_related` to an already-prefetched manager discards the
    prefetch and issues a fresh query per recipe, per meal. Measured at 8 extra queries
    for a single lunch against a 16-recipe library, growing linearly with it.
    """
    return [(line.food, float(line.grams or 0), bool(line.scalable))
            for line in recipe.ingredients.all()]


def _portion_recipe(lines: Sequence[Tuple[object, float, bool]],
                    servings: float, ladders: Dict[int, List[float]]) -> Components:
    """Scale a recipe, holding the parts that should not scale, and land on the ladder.

    Doubling a dish doubles the rice; it does not double the pinch of salt or the
    teaspoon of oil that makes it work. `scalable=False` lines stay put.

    Every amount then snaps to a portion of that food a person would serve — a whole
    number of eggs, half a cup of oats. Scaling by calories alone is how 210 g of oats
    reached a plate from a recipe that asked for 60: the macros were inside tolerance
    the whole way, because tolerance is about the total and says nothing about any one
    ingredient. Snapping also removes a bias, because the old rounding step could only
    push an amount up, so a scaled recipe missed high and never low.
    """
    out: Components = []
    for food, grams, scalable in lines:
        wanted = grams * servings if scalable else grams
        rungs = ladders[id(food)]
        out.append((food, min(rungs, key=lambda g: abs(g - wanted))))
    return out


#: Servings are searched, not computed. Dividing the target by the recipe's own energy
#: gives the scale that would be right if portions were continuous; once every line
#: snaps to a rung, the arithmetic answer is frequently not the best one available.
SERVINGS_LO, SERVINGS_HI, SERVINGS_STEP = 0.5, 2.0, 0.05




#: How hard a dish served inside the no-repeat window is pushed down. A penalty, not a
#: ban: with sixteen recipes across four meals, excluding outright exhausts the library
#: within a week and the planner falls back to assembling components, which trades a
#: repeated dish for a pile of ingredients. That is the wrong trade. Once the library
#: grows the penalty alone is enough to stop repeats.
RECENCY_PENALTY = 0.05


def chosen_food_ids(user, meal_name: str) -> frozenset:
    """The foods this client picked for this meal, across every macro slot.

    `UserFoodCategoryPreference` is the model behind "choose your breakfast, lunch and
    dinner items". It was already captured, already ranked first by `build_pool`, and
    never consulted here, because `find_recipe` had no user parameter — so a client who
    filled it in received the same plan as one who ignored it.
    """
    from diet.models import UserFoodCategoryPreference

    if user is None or not getattr(user, "pk", None):
        return frozenset()
    return frozenset(
        UserFoodCategoryPreference.objects
        .filter(user=user, meal=meal_name)
        .values_list("food_id", flat=True)
    )



#: How much a chosen food's preference counts by what it is in the dish.
PREFERENCE_ROLE_WEIGHT = {"staple": 1.0, "accompaniment": 0.3, "condiment": 0.1}


def recipe_pool_score(lines: Sequence[Tuple[object, float, bool]], pool, meal_name: str,
                      edges: Optional[Dict[int, "collections.Counter"]] = None) -> float:
    """One number, on the pool's own scale, for how much this client wants this dish.

    NOT a mean. Each ingredient's pool score for this meal is weighted by the
    ingredient's share of the recipe's calories at one serving. A plain mean let a spoon
    of oil drag chicken down and rewarded short recipes over complete ones; calorie share
    is the recipe's own composition and needs no constant. A garnish weighs what a
    garnish weighs. Pairing affinity is added in the same units as the template path
    adds it, so the two paths are commensurable.

    Additive and unwired until the distribution tests in the plan (9.2) pass on the
    seeded catalogue; only then does selection read it.
    """
    from .candidates import classify_food
    from .templates import SOFTMAX_T, W_PAIRING, _affinity

    if not lines or pool is None:
        return 0.0
    kcal = [max(0.0, float(getattr(f, "calories", 0) or 0) * float(g) / 100.0) for f, g, _s in lines]
    total = sum(kcal) or 1.0
    ids = [getattr(f, "id", None) for f, _g, _s in lines]
    structure = 0.0
    preference = 0.0
    pairing = 0.0
    has_preference = hasattr(pool, "preference")
    for (food, _g, _s), k in zip(lines, kcal):
        share = k / total
        slot = classify_food(food)
        if has_preference:
            struct_i = float(pool.weights(meal_name, slot).get(food.id, 0.0)) \
                       - float(pool.preference(meal_name, slot).get(food.id, 0.0))
            pref_i = float(pool.preference(meal_name, slot).get(food.id, 0.0))
        else:
            struct_i, pref_i = float(pool.weights(meal_name, slot).get(food.id, 0.0)), 0.0
        # Structure by calorie share: a garnish weighs what a garnish weighs.
        structure += share * struct_i
        # Preference by presence, weighted by ROLE. A client who chose chicken chose the
        # chicken dish whatever chicken's share of its calories — share-weighting gave
        # P(chosen wins) = 0.13. But a plain max counted a chosen garnish exactly like a
        # chosen anchor (choosing olive oil moved the dish +100, same as chicken), and
        # the role field exists to say which foods a meal is built on.
        preference = max(preference, PREFERENCE_ROLE_WEIGHT.get(
            getattr(food, "role", "staple"), 1.0) * pref_i)
        pairing += share * W_PAIRING * _affinity(food.id, [i for i in ids if i != food.id], edges or {})
    return structure + preference + pairing


def softmax_weights(scores: Sequence[float], temperature: float) -> List[float]:
    """The template path's sampling rule, exposed so the recipe path can use the same one."""
    import math
    if not scores:
        return []
    top = max(scores)
    return [max(math.exp((v - top) / temperature), 1e-9) for v in scores]


def find_recipe(meal_name: str, targets: Dict[str, float], policy: PlannerPolicy,
                allergen_checker=None, constraints=None,
                exclude_ids: Sequence[int] = (),
                recipes: Optional[Sequence] = None, user=None, rng=None,
                recent_ids: Sequence[int] = (),
                ladders: Optional[Dict[int, List[float]]] = None,
                pool=None, edges: Optional[Dict[int, "collections.Counter"]] = None
                ) -> Optional[RecipeMatch]:
    """A dish for this meal's macro target, or None if nothing fits.

    Every serving size between half and double is tried, each one snapped to the
    amounts the foods declare, and the closest is kept. That is the whole of the fit
    calculation now: the dish is scaled and portioned in one search rather than scaled
    by arithmetic and then repaired by a separate optimiser that knew nothing about
    what a serving is.

    Two things decide which dish, in this order. Fit is a filter: anything outside
    `policy.tolerance` cannot be served whatever else is true of it. Among the dishes
    that pass, the choice is weighted by how much of each one the client actually
    picked for this meal, and sampled rather than maximised.

    Sampling matters as much as the preference term. Returning the single best fit is
    deterministic, so the same client and the same target produced the same dish every
    day: five recipes out of sixteen were ever served and one appeared 48 times in a
    week of measurements. `rng` is the planner's per-user, per-day seeded generator, so
    the result is varied but reproducible.
    """
    from diet.models import Recipe

    from .constraints import ClientConstraints
    from .portion import portions_for

    if constraints is None:
        constraints = (ClientConstraints(allergen_checker=allergen_checker)
                       if allergen_checker is not None else ClientConstraints.for_user(user))

    if recipes is None:
        recipes = list(
            Recipe.objects.filter(is_active=True)
            .exclude(id__in=list(exclude_ids))
            .prefetch_related("ingredients__food__category")
        )

    # Exclusions apply to the list we were GIVEN as well as the one we would build.
    # They were applied only inside the query above, so the moment the planner began
    # passing a cached library, same-day exclusion silently stopped: a dish was served
    # twice in one day on 23 of 42 measured days, recipes fit every slot, and the
    # template path never got a turn.
    if exclude_ids:
        banned = set(exclude_ids)
        recipes = [r for r in recipes if getattr(r, "id", None) not in banned]

    target_kcal = float(targets.get("calories", 0) or 0)
    if target_kcal <= 0 or not recipes:
        return None

    recent = set(recent_ids or ())
    steps = int(round((SERVINGS_HI - SERVINGS_LO) / SERVINGS_STEP)) + 1
    within: List[Tuple[RecipeMatch, float]] = []
    best: Optional[RecipeMatch] = None
    for recipe in recipes:
        suits = getattr(recipe, "meal_types", None) or []
        if suits and meal_name not in suits:
            continue

        lines = _recipe_lines(recipe)
        if not lines:
            continue

        # Every hard constraint, not just allergens. This checked allergies and nothing
        # else, so a dish built on a food the client had explicitly rejected was served
        # to them — and persistence then refused the finished plan outright rather than
        # producing one without it, so the client received an error instead of dinner.
        if constraints is not None and constraints.active:
            if constraints.forbids_any(food for food, _g, _s in lines):
                continue
            if not constraints.cuisine.allows(getattr(recipe, "cuisine", None)):
                continue

        # Ladders are a pure function of the food; cached per generation when the
        # caller provides a dict, recomputed otherwise.
        if ladders is None:
            ladders = {}
        line_ladders = {}
        for food, _g, _s in lines:
            key = getattr(food, "id", None) or id(food)
            if key not in ladders:
                ladders[key] = [p.grams for p in portions_for(food)]
            line_ladders[id(food)] = ladders[key]

        # A dish that cannot REACH the target is not a candidate, and the servings search
        # need not run to find that out. The bound is the dish's real reach — every line
        # at its ladder floor, and every line at its ladder ceiling — which is exactly the
        # space the search and refinement can produce, so it can never reject a fit. A
        # slack multiplier on the base serving could: refinement moves single lines to
        # their ceilings and took Mujadara from a 450 kcal base to a 1,120 kcal lunch,
        # which a 2.5x window discarded and the day landed 6.9% under. At a thousand
        # recipes this is the difference between scanning a thousand and scanning tens.
        reach_lo = sum(min(line_ladders[id(f)]) * float(getattr(f, "calories", 0) or 0) / 100.0
                       for f, _g, _s in lines)
        reach_hi = sum(max(line_ladders[id(f)]) * float(getattr(f, "calories", 0) or 0) / 100.0
                       for f, _g, _s in lines)
        tol = float(policy.tolerance.get("calories", 0.10))
        if reach_hi <= 0 or not (reach_lo * (1.0 - tol) <= target_kcal <= reach_hi * (1.0 + tol)):
            continue

        # Prefer a serving that is inside tolerance over the one with the smallest
        # magnitude. They are not the same point and the difference decides whether the
        # dish can be served at all: magnitude averages the macros, `within` requires
        # every one of them to be inside its own bound, so the lowest-magnitude serving
        # is regularly one that fails on a single macro while a neighbouring serving
        # passes on all four.
        match: Optional[RecipeMatch] = None
        closest: Optional[RecipeMatch] = None
        for index in range(steps):
            servings = SERVINGS_LO + index * SERVINGS_STEP
            components = _portion_recipe(lines, servings, line_ladders)
            dev = deviation_of(totals_of(components), targets)
            candidate = RecipeMatch(recipe, components, servings, dev)
            if closest is None or dev.magnitude < closest.deviation.magnitude:
                closest = candidate
            if dev.within(policy.tolerance):
                if match is None or dev.magnitude < match.deviation.magnitude:
                    match = candidate
        match = match or closest
        if match is None:
            continue

        # A dish whose macro ratio does not match the target cannot be fixed by serving
        # more of all of it, so after the best serving is chosen the scalable lines are
        # adjusted individually — on their own ladders, so the dish stays servable.
        #
        # Unconditionally. This used to run only when the serving was OUTSIDE tolerance,
        # which is an early exit on first-acceptable rather than best-available: a dish
        # that scraped inside the band was served as it landed while a better portioning
        # of the same dish sat one move away. Against exhaustive search that accounted
        # for most of the meals the engine served that it could have beaten.
        movable = [i for i, (_f, _g, scalable) in enumerate(lines) if scalable]
        if movable:
            tuned, dev = refine(match.components, targets, policy.tolerance, movable)
            if _better(dev, match.deviation, policy.tolerance):
                match = RecipeMatch(recipe, tuned, match.servings, dev)

        if best is None or match.deviation.magnitude < best.deviation.magnitude:
            best = match

        if match.deviation.within(policy.tolerance):
            # Fit is a FILTER — inside tolerance or not — never a weight. Among the
            # dishes that fit, the dish is scored on the pool's own scale, the same
            # scale the template path samples on: structure by calorie share,
            # preference by presence and role, pairing bounded. One scorer for both
            # paths, so "which path served this meal" can be a judged decision rather
            # than an accident of ordering. The two paths used to weight preference
            # at 1.7:1 and 148:1 respectively.
            score = (recipe_pool_score(lines, pool, meal_name, edges) if pool is not None
                     else -float(match.deviation.magnitude))
            within.append((match, score, recipe))

    if within:
        if rng is None or len(within) == 1:
            return max(within, key=lambda triple: triple[1])[0]
        matches = [m for m, _s, _r in within]
        weights = softmax_weights([s for _m, s, _r in within], SOFTMAX_T)
        for i, (_m, _s, r) in enumerate(within):
            if constraints is not None:
                # A dish from the cuisine the client asked for less of is still
                # servable at a mixed ratio; it is simply chosen less often.
                weights[i] *= max(0.1, constraints.cuisine.weight(getattr(r, "cuisine", None)))
            if r.id in recent:
                weights[i] *= RECENCY_PENALTY
        return rng.choices(matches, weights=weights, k=1)[0]

    if best is not None:
        logger.debug("Best recipe for %s is outside tolerance (%s); caller may fall back",
                     meal_name, best.deviation.human())
    return best
