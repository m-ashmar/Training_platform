"""Dish assembly.

Turns a macro target into a recognisable meal instead of a list of foods that happens to
add up. The planner's component fill stays as the fallback, because a recipe library
will never cover every target — but when a dish fits, a dish is what the user gets.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .optimize import Components, optimize_meal, totals_of
from .policy import PlannerPolicy
from .report import deviation_of

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


def _scaled_components(recipe, servings: float, policy: PlannerPolicy) -> Components:
    """Scale a recipe, holding the parts that should not scale.

    Doubling a dish doubles the rice; it does not double the pinch of salt or the
    teaspoon of oil that makes it work. `scalable=False` lines stay put.

    Every scaled amount then snaps to a servable portion of that food — a whole number
    of eggs, half a cup of oats — rather than to whatever gram figure the arithmetic
    produced. Scaling by calories alone is how 210 g of oats reached a plate from a
    recipe that asked for 60: the macros were inside tolerance the whole way, because
    tolerance is about the total and says nothing about any one ingredient.

    Snapping also removes a bias. `policy.round_grams` rounded to a step, and the floor
    below it could only ever push an amount up, so a scaled recipe missed high and never
    low. Choosing the nearest servable amount misses in both directions.
    """
    from .portion import nearest_portion

    out: Components = []
    for line in recipe.ingredients.select_related("food"):
        grams = float(line.grams or 0)
        if line.scalable:
            grams *= servings
        from .candidates import classify_food
        macro = classify_food(line.food)
        grams = max(policy.floor_portion_for(macro), min(policy.cap_for(macro), grams))
        out.append((line.food, nearest_portion(line.food, grams).grams))
    return out


#: How much a dish being made of the client's own choices counts against how neatly it
#: hits the macro target. Fit is a constraint — anything outside tolerance is discarded
#: before this is consulted — so among dishes that all work, what the client picked is
#: what should decide.
W_PREFERENCE = 2.0
W_FIT = 1.0

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


def _preference_share(recipe, wanted: frozenset) -> float:
    """Fraction of this dish's ingredients the client asked for. 0.0 when they asked for nothing."""
    if not wanted:
        return 0.0
    lines = list(recipe.ingredients.all())
    if not lines:
        return 0.0
    return sum(1 for line in lines if line.food_id in wanted) / len(lines)


def find_recipe(meal_name: str, targets: Dict[str, float], policy: PlannerPolicy,
                allergen_checker=None, exclude_ids: Sequence[int] = (),
                recipes: Optional[Sequence] = None, user=None, rng=None,
                recent_ids: Sequence[int] = ()) -> Optional[RecipeMatch]:
    """A dish for this meal's macro target, or None if nothing fits.

    Scaling is chosen from calories — the dominant constraint — and the result is then
    handed to the same optimiser the component path uses, so a recipe is held to
    exactly the same tolerance as anything else.

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
    from diet.services.meal_validator import VIOLATION

    if recipes is None:
        recipes = list(
            Recipe.objects.filter(is_active=True)
            .exclude(id__in=list(exclude_ids))
            .prefetch_related("ingredients__food")
        )

    target_kcal = float(targets.get("calories", 0) or 0)
    if target_kcal <= 0 or not recipes:
        return None

    wanted = chosen_food_ids(user, meal_name)
    recent = set(recent_ids or ())
    within: List[Tuple[RecipeMatch, float]] = []
    best: Optional[RecipeMatch] = None
    for recipe in recipes:
        suits = getattr(recipe, "meal_types", None) or []
        if suits and meal_name not in suits:
            continue

        if allergen_checker is not None and getattr(allergen_checker, "active", False):
            unsafe = any(
                allergen_checker.check_food(line.food).verdict == VIOLATION
                for line in recipe.ingredients.all()
            )
            if unsafe:
                continue

        base = recipe.nutrition()
        base_kcal = float(base.get("calories", 0) or 0)
        if base_kcal <= 0:
            continue

        # A dish may be scaled between a half and a double serving; beyond that it is
        # not the same dish any more.
        servings = max(0.5, min(2.0, target_kcal / base_kcal))
        components = _scaled_components(recipe, servings, policy)
        result = optimize_meal(components, targets, policy)
        match = RecipeMatch(recipe, result.components, servings, result.deviation)

        if best is None or match.deviation.magnitude < best.deviation.magnitude:
            best = match

        if match.deviation.within(policy.tolerance):
            # Closeness of fit, normalised so a perfect match scores 1 and one at the
            # edge of tolerance scores near 0, plus the share of the dish the client
            # asked for. Weighted so preference decides between dishes that all fit.
            fit = 1.0 / (1.0 + float(match.deviation.magnitude))
            weight = W_FIT * fit + W_PREFERENCE * _preference_share(recipe, wanted)
            if recipe.id in recent:
                weight *= RECENCY_PENALTY
            within.append((match, max(weight, 1e-6)))

    if within:
        if rng is None or len(within) == 1:
            return max(within, key=lambda pair: pair[1])[0]
        matches, weights = zip(*within)
        return rng.choices(matches, weights=weights, k=1)[0]

    if best is not None:
        logger.debug("Best recipe for %s is outside tolerance (%s); caller may fall back",
                     meal_name, best.deviation.human())
    return best
