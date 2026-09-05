"""Nutrition policy — every constant that used to be hardcoded.

58 distinct magic numbers were embedded in the planner: protein floors, carb floors, the
overshoot slack, snack calories, per-meal splits, item caps. They ARE the nutrition
policy, and burying them in control flow meant a dietitian could not change one without
a deploy, and no two goals could differ except where someone had written an `if`.

`DietConfig` already existed for exactly this and held two fields. Everything lives here
now, with defaults that reproduce the previous behaviour, overridable per deployment via
settings and per row via DietConfig.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List

from django.conf import settings

GOALS = ("lose", "maintain", "gain")


@dataclass(frozen=True)
class PlannerPolicy:
    """All tunable nutrition rules for one goal."""

    goal: str = "maintain"

    # --- energy split -----------------------------------------------------
    snack_kcal: float = 200.0
    meal_kcal_split: Dict[str, float] = field(
        default_factory=lambda: {"Breakfast": 0.35, "Lunch": 0.40, "Dinner": 0.25}
    )

    # --- macro ratios (fractions of daily kcal) ---------------------------
    protein_ratio: float = 0.30
    carb_ratio: float = 0.45
    fat_ratio: float = 0.25
    #: Protein by bodyweight, in grams per kilogram. A percentage of energy is the wrong
    #: primary model: a 110 kg client cutting landed near 0.95 g/kg because the 1,200 kcal
    #: floor capped the calories the percentage was taken of. When a weight is known,
    #: protein comes from here and carbohydrate and fat split the remaining energy.
    protein_g_per_kg: float = 1.6
    protein_floor_g: float = 60.0
    protein_ceiling_g: float = 250.0

    # --- feasibility --------------------------------------------------------
    #: What one slot can carry in servable portions. Measured: three meals and a snack
    #: top out near 3,700 kcal, because every portion is bounded by an amount a person
    #: would serve. Checked BEFORE planning so a 5,000 kcal request is refused with a
    #: suggestion, not built at 3,900 and labelled 5,000.
    max_kcal_per_meal: float = 1250.0
    max_kcal_per_snack: float = 400.0

    # --- per-meal floors --------------------------------------------------
    protein_floor_main_g: float = 40.0      # lunch / dinner
    protein_floor_other_g: float = 35.0     # breakfast / snack
    carb_floor_g: float = 50.0

    # --- fill behaviour ---------------------------------------------------
    # Was 0.10 — a deliberate overshoot that existed only to feed the correctors.
    # Zero means selection aims AT the target and the optimiser closes the gap.
    fill_slack: float = 0.0
    max_items_per_macro: int = 2
    macro_order: List[str] = field(default_factory=lambda: ["protein", "carb", "fat"])

    # --- portions ---------------------------------------------------------
    gram_rounding: float = 5.0
    portion_caps_g: Dict[str, float] = field(
        default_factory=lambda: {"protein": 300.0, "carb": 300.0, "fat": 60.0,
                                 "vegetable": 300.0, "fruit": 200.0}
    )
    # Per macro. A single 15 g floor was applied to every scaled component, so a 5 g
    # olive-oil portion was forced up to 15 g — 133 kcal — turning a small corrective
    # step into a 3x jump that overshot fat from -30% to +21% and stalled the loop.
    min_portions_g: Dict[str, float] = field(
        default_factory=lambda: {"fat": 3.0, "protein": 30.0, "carb": 20.0,
                                 "vegetable": 30.0, "fruit": 50.0}
    )
    fruit_portion_g: float = 120.0
    fruit_meals_per_day: int = 2

    # --- convergence ------------------------------------------------------
    # MACRO_TOLERANCE was declared in settings and read by nothing.
    tolerance: Dict[str, float] = field(
        default_factory=lambda: {"calories": 0.10, "protein": 0.15,
                                 "carb": 0.20, "fat": 0.20}
    )
    max_optimiser_passes: int = 12

    # --- variety ----------------------------------------------------------
    no_repeat_days: int = 3

    def floor_for(self, meal_name: str) -> float:
        main = (meal_name or "").lower() in ("lunch", "dinner")
        return self.protein_floor_main_g if main else self.protein_floor_other_g

    def cap_for(self, dominant_macro: str) -> float:
        return self.portion_caps_g.get(dominant_macro, 300.0)

    def floor_portion_for(self, dominant_macro: str) -> float:
        return self.min_portions_g.get(dominant_macro, 15.0)

    fine_rounding_below_g: float = 50.0
    fine_gram_rounding: float = 1.0

    def round_grams(self, grams: float) -> float:
        """Round a portion to something a person can actually measure.

        A flat 5 g step is right for rice and wrong for oil: at a 15 g portion it is a
        33% quantum, so the optimiser would raise fat, overshoot, and then find every
        corrective step rounding back to where it started — it stalled with fat at
        +21%. Small portions round to 1 g (a teaspoon of oil is ~5 g), larger ones to 5.
        """
        step = (self.fine_gram_rounding if grams < self.fine_rounding_below_g
                else (self.gram_rounding or 1.0))
        return round(grams / step) * step


# Goal-specific overrides. Previously these were scattered `if "lose" in goal` branches.
_GOAL_POLICIES = {
    "lose": dict(
        meal_kcal_split={"Breakfast": 0.30, "Lunch": 0.40, "Dinner": 0.30},
        macro_order=["protein", "carb", "fat"],
        carb_floor_g=0.0,          # carbs are not floored when cutting
        protein_g_per_kg=2.0,
    ),
    "gain": dict(
        meal_kcal_split={"Breakfast": 0.35, "Lunch": 0.35, "Dinner": 0.30},
        macro_order=["carb", "protein", "fat"],
        protein_g_per_kg=1.8,
    ),
    "maintain": {},
}


def load_policy(goal: str = "maintain") -> PlannerPolicy:
    """Build the policy for a goal: defaults → goal overrides → settings → DietConfig."""
    goal = (goal or "maintain").lower()
    goal = next((g for g in GOALS if g in goal), "maintain")

    policy = replace(PlannerPolicy(goal=goal), **_GOAL_POLICIES.get(goal, {}))

    # Macro ratios come from the ONE canonical source. `diet/utils/nutrition.py`
    # says "All files should import and use this instead of duplicating the logic" —
    # and an earlier version of this module duplicated it anyway, with maintain at
    # C45/F25 against the canonical C50/F20. The planner aimed at one target and the
    # optimiser judged against another, so carbohydrate read as +27% over on every
    # plan that was in fact correct.
    from diet.utils.nutrition import get_macro_ratios

    ratios = get_macro_ratios(goal)
    policy = replace(
        policy,
        protein_ratio=float(ratios.get("protein", policy.protein_ratio)),
        carb_ratio=float(ratios.get("carb", policy.carb_ratio)),
        fat_ratio=float(ratios.get("fat", policy.fat_ratio)),
    )

    overrides = getattr(settings, "DIET_PLANNER_POLICY", {}) or {}
    merged = {**overrides.get("*", {}), **overrides.get(goal, {})}
    if merged:
        policy = replace(policy, **{k: v for k, v in merged.items()
                                    if k in PlannerPolicy.__dataclass_fields__})

    tol = getattr(settings, "DIET_CONFIG", {}).get("MACRO_TOLERANCE") if hasattr(settings, "DIET_CONFIG") else None
    if isinstance(tol, dict) and tol:
        policy = replace(policy, tolerance={**policy.tolerance, **tol})

    return policy
