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


def compute_targets(daily_kcal: float, policy: PlannerPolicy,
                    meal_names: List[str], snack_count: int = 0) -> DayTargets:
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

    def macros_for(kcal: float, name: str) -> MealTargets:
        return MealTargets(
            name=name,
            calories=kcal,
            protein=kcal * policy.protein_ratio / 4.0,
            carb=kcal * policy.carb_ratio / 4.0,
            fat=kcal * policy.fat_ratio / 9.0,
        )

    meals = [macros_for(main_kcal * (weights[m] / total_weight), m) for m in meal_names]
    for i in range(max(0, int(snack_count))):
        meals.append(macros_for(policy.snack_kcal, "Snack" if i == 0 else f"Snack {i+1}"))

    return DayTargets(
        calories=daily_kcal,
        protein=daily_kcal * policy.protein_ratio / 4.0,
        carb=daily_kcal * policy.carb_ratio / 4.0,
        fat=daily_kcal * policy.fat_ratio / 9.0,
        meals=meals,
    )
