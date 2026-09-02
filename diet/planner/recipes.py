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
    """
    out: Components = []
    for line in recipe.ingredients.select_related("food"):
        grams = float(line.grams or 0)
        if line.scalable:
            grams *= servings
        from .candidates import classify_food
        macro = classify_food(line.food)
        grams = max(policy.floor_portion_for(macro), min(policy.cap_for(macro), grams))
        out.append((line.food, policy.round_grams(grams)))
    return out


def find_recipe(meal_name: str, targets: Dict[str, float], policy: PlannerPolicy,
                allergen_checker=None, exclude_ids: Sequence[int] = (),
                recipes: Optional[Sequence] = None) -> Optional[RecipeMatch]:
    """Best dish for this meal's macro target, or None if nothing fits.

    Scaling is chosen from calories — the dominant constraint — and the result is then
    handed to the same optimiser the component path uses, so a recipe is held to
    exactly the same tolerance as anything else.
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

    if best is not None and not best.deviation.within(policy.tolerance):
        logger.debug("Best recipe for %s is outside tolerance (%s); caller may fall back",
                     meal_name, best.deviation.human())
    return best
