"""Energy and macro targets.

Extracted so the numbers a plan is judged against are computed in one place and can be
asserted directly, instead of being recomputed inside the fill loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .policy import PlannerPolicy


@dataclass(frozen=True)
class MealTargets:
    name: str
    calories: float
    protein: float
    carb: float
    fat: float

    def as_dict(self) -> Dict[str, float]:
        return {"calories": self.calories, "protein": self.protein,
                "carb": self.carb, "fat": self.fat}


@dataclass(frozen=True)
class DayTargets:
    calories: float
    protein: float
    carb: float
    fat: float
    meals: List[MealTargets] = field(default_factory=list)

    def as_dict(self) -> Dict[str, float]:
        return {"calories": self.calories, "protein": self.protein,
                "carb": self.carb, "fat": self.fat}


def day_macro_grams(daily_kcal: float, policy: PlannerPolicy,
                    weight_kg: float | None = None) -> Dict[str, float]:
    """The day's protein, carbohydrate and fat in grams. The one place.

    With a bodyweight, protein is grams per kilogram — bounded — and the remaining
    energy is split between carbohydrate and fat in the policy's ratio. Without one it
    falls back to the percentage split. The planner, persistence and convergence all
    read this, so they cannot disagree.
    """
    kcal = max(0.0, float(daily_kcal or 0.0))
    if weight_kg and weight_kg > 0:
        protein = max(policy.protein_floor_g,
                      min(policy.protein_ceiling_g, float(weight_kg) * policy.protein_g_per_kg))
        protein = min(protein, kcal / 4.0)  # never more protein than there is energy
        remaining = max(0.0, kcal - protein * 4.0)
        share = policy.carb_ratio + policy.fat_ratio or 1.0
        carb = remaining * (policy.carb_ratio / share) / 4.0
        fat = remaining * (policy.fat_ratio / share) / 9.0
        return {"protein": protein, "carb": carb, "fat": fat}
    return {"protein": kcal * policy.protein_ratio / 4.0,
            "carb": kcal * policy.carb_ratio / 4.0,
            "fat": kcal * policy.fat_ratio / 9.0}


def compute_targets(daily_kcal: float, policy: PlannerPolicy,
                    meal_names: List[str], snack_count: int = 0,
                    weight_kg: float | None = None) -> DayTargets:
    """Split a daily energy target into per-meal macro targets.

    Snack calories come off the top (as before), then the remainder is split by the
    policy's per-meal fractions, renormalised so they always sum to 1 even when the
    caller asks for a meal count the split does not name.
    """
    daily_kcal = max(0.0, float(daily_kcal or 0.0))
    snack_kcal = policy.snack_kcal * max(0, int(snack_count))
    main_kcal = max(0.0, daily_kcal - snack_kcal)

    weights = {m: policy.meal_kcal_split.get(m, 1.0 / max(1, len(meal_names)))
               for m in meal_names}
    total_weight = sum(weights.values()) or 1.0

    day = day_macro_grams(daily_kcal, policy, weight_kg)

    def macros_for(kcal: float, name: str) -> MealTargets:
        # Each meal takes its share of the DAY's grams, so the day sums to the day.
        share = (kcal / daily_kcal) if daily_kcal > 0 else 0.0
        return MealTargets(name=name, calories=kcal, protein=day["protein"] * share,
                           carb=day["carb"] * share, fat=day["fat"] * share)

    meals = [macros_for(main_kcal * (weights[m] / total_weight), m) for m in meal_names]
    for i in range(max(0, int(snack_count))):
        meals.append(macros_for(policy.snack_kcal, "Snack" if i == 0 else f"Snack {i+1}"))

    return DayTargets(calories=daily_kcal, protein=day["protein"], carb=day["carb"],
                      fat=day["fat"], meals=meals)


def plan_targets(diet_plan, meal_names, snack_count: int = 0) -> DayTargets:
    """The targets a persisted plan was built to, for the day and per meal.

    Six sites recomputed these by hand from a hardcoded 30/50/20 regardless of goal,
    and two of them split calories equally across meals while the engine builds to the
    policy split, so a correctly converged Lose plan rendered as over-target in the app.
    This reads the plan's own goal and stored targets and applies the same split the
    planner used. Everything that shows a client a target reads it from here.
    """
    from .policy import load_policy

    policy = load_policy(getattr(diet_plan, "goal", "maintain"))
    weight = getattr(getattr(diet_plan, "user", None), "weight", None)
    day = compute_targets(float(getattr(diet_plan, "daily_calories", 0) or 0.0),
                          policy, list(meal_names), snack_count, weight_kg=weight)
    stored = tuple(getattr(diet_plan, f, None) for f in
                   ("target_protein", "target_carbs", "target_fat"))
    if all(v is not None for v in stored):
        # Persisted grams win over re-derivation; per-meal values scale with each
        # meal's share of the day's energy so the day still sums to the stored totals.
        kcal = day.calories or 1.0
        meals = [MealTargets(m.name, m.calories,
                             stored[0] * m.calories / kcal,
                             stored[1] * m.calories / kcal,
                             stored[2] * m.calories / kcal) for m in day.meals]
        return DayTargets(day.calories, float(stored[0]), float(stored[1]),
                          float(stored[2]), meals)
    return day
