"""Apply the optimiser to a persisted DietPlan.

Replaces the seven-stage corrector chain. Each meal is nudged toward its own macro
target and the best version seen is written back — instead of seven blind passes whose
net effect was measured going from +4.1% (inside tolerance) to -6.6% (shipped).
"""
from __future__ import annotations

import logging
from typing import Dict, List

from .optimize import optimize_meal, totals_of
from .policy import PlannerPolicy, load_policy
from .report import deviation_of
from .targets import compute_targets

logger = logging.getLogger(__name__)


def _movable_indices(meal, components):
    """Which of this meal's components convergence may move.

    A meal built from a library dish carries the author's `scalable` flags on the
    recipe's lines; everything else is free to move.
    """
    recipe = getattr(meal, "recipe", None)
    if recipe is None:
        return None
    try:
        pinned = {line.food_id for line in recipe.ingredients.all() if not line.scalable}
    except Exception:
        # Fail CLOSED. `None` means "every line movable", so an error reading the
        # recipe's lines sent the pinch of salt to its ladder ceiling — the exact
        # regression this function exists to prevent, silently. Moving nothing is the
        # safe answer: the meal keeps the portions the planner chose.
        logger.warning("could not read pinned lines for meal %s", getattr(meal, "pk", None),
                       exc_info=True)
        return []
    if not pinned:
        return None
    return [i for i, (food, _g) in enumerate(components)
            if getattr(food, "id", None) not in pinned]


def _average_kcal(targets) -> float:
    """The mean per-meal calorie share of this day."""
    meals = list(targets.meals)
    return sum(t.calories for t in meals) / len(meals) if meals else 0.0


def converge_plan(diet_plan, policy: PlannerPolicy | None = None) -> Dict:
    """Bring every day of a persisted plan inside tolerance, and report what happened.

    Returns a per-date summary so the caller can surface "this plan is 3% under your
    protein target" rather than silently shipping whatever came out.
    """
    from diet.models import MealComponent

    policy = policy or load_policy(getattr(diet_plan, "goal", "maintain"))
    daily_kcal = float(getattr(diet_plan, "daily_calories", 0) or 0)
    summary: Dict[str, Dict] = {}

    dates = sorted({m.date for m in diet_plan.meals.all()})
    for day in dates:
        meals = list(diet_plan.meals.filter(date=day).prefetch_related("components__food"))
        names = [m.meal_type for m in meals if (m.meal_type or "").lower() != "snack"]
        snacks = sum(1 for m in meals if (m.meal_type or "").lower() == "snack")
        # The same targets the plan was built to, bodyweight included. Without the
        # weight, convergence re-optimised every meal toward the percentage split while
        # the planner had built it to grams per kilogram, and the two disagreed by enough
        # that 114 of 144 persisted meals read as off the proven optimum.
        targets = compute_targets(daily_kcal, policy, names or ["Breakfast", "Lunch", "Dinner"],
                                  snacks, weight_kg=getattr(getattr(diet_plan, "user", None), "weight", None))
        by_name = {t.name: t for t in targets.meals}

        day_totals = {"calories": 0.0, "protein": 0.0, "carb": 0.0, "fat": 0.0}
        for meal in meals:
            target = by_name.get(meal.meal_type)
            if target is None:
                # The AVERAGE share, not `targets.meals[0]`. That is the first name in
                # the split — Breakfast, at 30-35% of the day — so any meal the split
                # does not name (a trainer's "Pre-workout", a renamed slot) was
                # optimised toward a full breakfast and those portions were written back.
                target = min(targets.meals,
                             key=lambda t: abs(t.calories - _average_kcal(targets)))

            components = []
            for comp in meal.components.all():
                grams = _grams(comp.quantity)
                if grams > 0:
                    components.append((comp.food, grams))
            if not components:
                continue

            # Lines the recipe author pinned stay pinned. `find_recipe` carefully holds
            # `scalable=False` lines still, and then convergence re-optimised the same
            # meal with every line movable and sent the pinch of salt to its ladder
            # ceiling — so the plan that was saved was not the plan that was planned.
            movable = _movable_indices(meal, components)

            # The target the planner BUILT this meal to, residual carry included, when
            # it recorded one; the split only for rows that predate the column.
            built = getattr(meal, "target", None)
            aim = (dict(built) if isinstance(built, dict) and built.get("calories")
                   else target.as_dict())
            result = optimize_meal(components, aim, policy, movable=movable)
            _write_back(meal, result.components, MealComponent)

            for key, value in totals_of(result.components).items():
                day_totals[key] += value

            if not result.converged:
                logger.info(
                    "Meal %s on %s did not converge: %s | %s",
                    meal.meal_type, day, result.deviation.human(), "; ".join(result.trace[-2:]),
                )

        dev = deviation_of(day_totals, targets.as_dict())
        summary[str(day)] = {
            "totals": {k: round(v, 1) for k, v in day_totals.items()},
            "targets": {k: round(v, 1) for k, v in targets.as_dict().items()},
            "deviation": {k: round(v, 4) for k, v in dev.as_dict().items()},
            "within_tolerance": dev.within(policy.tolerance),
        }
        logger.info("Day %s converged=%s %s", day, dev.within(policy.tolerance), dev.human())

    return summary


def _grams(quantity) -> float:
    """Quantity is stored numerically, but tolerate a "180g" string from older rows."""
    try:
        return float(str(quantity).lower().replace("g", "").strip() or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _write_back(meal, components, MealComponent) -> None:
    """Persist adjusted portions. Only the quantity changes; nothing is added or removed."""
    by_food = {food.id: grams for food, grams in components}
    for comp in meal.components.all():
        grams = by_food.get(comp.food_id)
        if grams is None:
            continue
        # quantity is a numeric field; write the number, not a "180g" string.
        if abs(float(comp.quantity or 0) - grams) > 1e-6:
            comp.quantity = grams
            comp.save(update_fields=["quantity"])
