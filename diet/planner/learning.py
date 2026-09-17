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


def _was_offered(meal, today, later_plans_by_plan) -> bool:
    """Did the client actually get this meal, or at least touch it?

    A plan writes every meal of its 7 or 30 days up front, and a regenerated plan is
    deactivated with all its meals still in the table. Counting every row as "served"
    read tomorrow's dinner, and every meal of a plan replaced on day two, as food the
    client was given and did not finish — the same silence-as-dislike inversion
    `score_from` describes, arriving by another door. A client who uses the completion
    screen had every food of their CURRENT plan pushed down nightly for the weeks it had
    left, and each regeneration added a month of phantom refusals.

    Offered means: due before today on a plan nothing newer replaced for that date — or
    touched at all (completed, rated, a quantity logged), which is evidence whatever the
    date says.
    """
    if meal.date >= today:
        return False
    return not any(start <= meal.date <= end for start, end in later_plans_by_plan.get(
        meal.diet_plan_id, ()))


def observations_for(user) -> Dict[int, Dict[str, float]]:
    """Per-food evidence from the meals this user was actually offered."""
    from diet.models import DietPlan, MealComponent
    from training_platform.user_time import today_for

    today = today_for(user)
    plans = list(DietPlan.objects.filter(user=user).values_list(
        "id", "created_at", "start_date", "end_date"))
    later_plans_by_plan = {
        pid: [(s, e) for other, c2, s, e in plans if (c2, other) > (created, pid)]
        for pid, created, _s, _e in plans
    }

    stats: Dict[int, Dict[str, float]] = {}
    components = (
        MealComponent.objects
        .filter(meal__diet_plan__user=user)
        .select_related("meal")
        .only("food_id", "quantity", "actual_quantity_consumed", "is_completed", "is_liked",
              "meal__is_liked", "meal__date", "meal__diet_plan")
    )
    for comp in components:
        touched = (comp.is_completed or comp.is_liked is not None
                   or comp.actual_quantity_consumed is not None
                   or getattr(comp.meal, "is_liked", None) is not None)
        if not touched and not _was_offered(comp.meal, today, later_plans_by_plan):
            continue
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


def score_from(rec: Dict[str, float], uses_completion: bool = True) -> float:
    """Turn evidence into a target weight in [MIN_WEIGHT, MAX_WEIGHT].

    `uses_completion` says whether this client has ever marked anything complete. It is
    not a refinement, it is the difference between a rule that works and one that
    inverts. The completion term is `0.3 * (completed/served - 0.5)`, so a client who
    has never opened the completion screen scores 0.85 for every food they were served —
    while a food they have never been served has no row at all and scores the neutral
    1.0. The nightly job then walks each served food toward 0.85, `W_LEARNED` turns that
    into -3.0 points, and the engine steadily steers the client away from everything it
    has ever given them. Silence was being read as dislike. It is read as silence now.
    """
    served = rec["served"] or 1.0
    refusal = rec["refused"] / served
    liking = (rec["liked"] - rec["disliked"]) / served

    # Centred on 1.0: finishing and liking push up, refusing pushes down.
    target = 1.0 + 0.3 * liking - 0.5 * refusal
    if uses_completion:
        target += 0.3 * (rec["completed"] / served - 0.5)
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
    # Whether this client uses the completion feature AT ALL, decided once over their
    # whole history rather than per food — a food they happen not to have finished yet
    # says nothing on its own.
    uses_completion = any(rec["completed"] for rec in stats.values())
    existing = {w.food_id: w for w in UserFoodWeight.objects.filter(user=user, food_id__in=stats)}
    for food_id, rec in stats.items():
        if rec["served"] < 2:
            continue  # one observation is noise
        row = existing.get(food_id)
        current = float(row.weight if row else 1.0)
        target = score_from(rec, uses_completion)
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


def replay_swaps(user, days: int = 7, seed_salt: str = "replay") -> Dict[str, float]:
    """Would the next plan have avoided what this client swapped away?

    Offline evaluation, before any weight is trusted: for every recorded swap, plan
    `days` ahead with the weights as they stand and ask whether the rejected food is
    served again in that slot. Reported, never asserted against a target — the client's
    own signal can be biased (someone who dislikes vegetables is not evidence against
    vegetables), which is why learning stays hand-set until this says yes on real data.

    Returns {"swaps": n, "avoided": k, "avoided_share": k/n, "still_served": [...]}.
    """
    from diet.models import MealSwap
    from diet.services.rule_based_planner import RuleBasedPlanner

    swaps = list(MealSwap.objects.filter(user=user).select_related("meal", "rejected_food"))
    if not swaps:
        return {"swaps": 0, "avoided": 0, "avoided_share": 0.0, "still_served": []}
    out = RuleBasedPlanner(user, seed_salt=seed_salt).generate(
        daily_kcal=float(user.calculate_daily_calories()), meal_count=3, snack_count=1,
        duration_days=days)
    served: Dict[str, set] = {}
    for meal in out.plan:
        served.setdefault(meal.meal_type, set()).update(
            i.food_id for i in meal.ingredients if i.food_id is not None)
    still = []
    for swap in swaps:
        slot = swap.meal.meal_type
        if swap.rejected_food_id in served.get(slot, set()):
            name = getattr(swap.rejected_food, "name_en", None) or swap.rejected_food.name
            still.append(f"{name} at {slot}")
    avoided = len(swaps) - len(still)
    return {"swaps": len(swaps), "avoided": avoided,
            "avoided_share": avoided / len(swaps), "still_served": still}
