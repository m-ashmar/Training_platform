"""Per-user food weights, learned from what was actually eaten.

Weights live in `UserFoodWeight`, one row per (client, food). They used to be written
onto `FoodItem.smart_score_weight`, a single global column, so the nightly task had every
client overwriting every other and one person refusing lentils lowered lentils for the
whole platform. Meanwhile `Meal.is_liked`, `MealComponent.is_completed` and
`actual_quantity_consumed` were being collected on every plan and read by nothing.

The signal was already there. This turns it into ranking input.
"""
from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# How far a food may drift from neutral. Bounded so one bad week cannot bury an item.
MIN_WEIGHT = 0.5
MAX_WEIGHT = 1.5
LEARNING_RATE = 0.15

# Below this fraction of the planned amount, the food was effectively refused.
REFUSED_RATIO = 0.5


def observations_for(user) -> Dict[int, Dict[str, float]]:
    """Per-food evidence from this user's completed meals."""
    from diet.models import MealComponent

    stats: Dict[int, Dict[str, float]] = {}
    components = (
        MealComponent.objects
        .filter(meal__diet_plan__user=user)
        .select_related("meal")
        .only("food_id", "quantity", "actual_quantity_consumed", "is_completed", "is_liked",
              "meal__is_liked")
    )
    for comp in components:
        rec = stats.setdefault(comp.food_id, {"served": 0.0, "completed": 0.0,
                                              "refused": 0.0, "liked": 0.0, "disliked": 0.0})
        rec["served"] += 1
        if comp.is_completed:
            rec["completed"] += 1
        planned = float(comp.quantity or 0)
        actual = comp.actual_quantity_consumed
        if planned > 0 and actual is not None:
            if float(actual) / planned < REFUSED_RATIO:
                rec["refused"] += 1
        # A per-food rating outranks the meal's; the meal's applies only where the
        # client said nothing about the food itself.
        liked = comp.is_liked if comp.is_liked is not None else getattr(comp.meal, "is_liked", None)
        if liked is True:
            rec["liked"] += 1
        elif liked is False:
            rec["disliked"] += 1

    # A swap is the strongest signal there is: the client rejected one food and named
    # another. It counts as a refusal of the first and a like of the second.
    from diet.models import MealSwap
    for swap in MealSwap.objects.filter(user=user).only("rejected_food_id", "chosen_food_id"):
        rej = stats.setdefault(swap.rejected_food_id, {"served": 0.0, "completed": 0.0,
                                                        "refused": 0.0, "liked": 0.0, "disliked": 0.0})
        rej["served"] += 1; rej["refused"] += 1
        got = stats.setdefault(swap.chosen_food_id, {"served": 0.0, "completed": 0.0,
                                                      "refused": 0.0, "liked": 0.0, "disliked": 0.0})
        got["served"] += 1; got["liked"] += 1
    return stats


def score_from(rec: Dict[str, float]) -> float:
    """Turn evidence into a target weight in [MIN_WEIGHT, MAX_WEIGHT]."""
    served = rec["served"] or 1.0
    completion = rec["completed"] / served
    refusal = rec["refused"] / served
    liking = (rec["liked"] - rec["disliked"]) / served

    # Centred on 1.0: finishing and liking push up, refusing pushes down.
    target = 1.0 + 0.3 * (completion - 0.5) + 0.3 * liking - 0.5 * refusal
    return max(MIN_WEIGHT, min(MAX_WEIGHT, target))


def update_weights(user, dry_run: bool = False) -> Dict[int, float]:
    """Move each food's weight a step toward what this user's behaviour implies.

    A step rather than a jump: one skipped meal should nudge a food, not exile it.
    """
    from diet.models import UserFoodWeight

    stats = observations_for(user)
    if not stats:
        return {}

    changes: Dict[int, float] = {}
    existing = {w.food_id: w for w in UserFoodWeight.objects.filter(user=user, food_id__in=stats)}
    for food_id, rec in stats.items():
        if rec["served"] < 2:
            continue  # one observation is noise
        row = existing.get(food_id)
        current = float(row.weight if row else 1.0)
        target = score_from(rec)
        new = current + LEARNING_RATE * (target - current)
        new = round(max(MIN_WEIGHT, min(MAX_WEIGHT, new)), 4)
        if abs(new - current) > 1e-4:
            changes[food_id] = new
            if not dry_run:
                UserFoodWeight.objects.update_or_create(
                    user=user, food_id=food_id,
                    defaults={"weight": new, "observations": int(rec["served"])})
    if changes:
        logger.info("Adjusted %s food weights from consumption by user %s",
                    len(changes), getattr(user, "id", "?"))
    return changes
