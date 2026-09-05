"""The objective function.

The old pipeline had seven correctors and no shared notion of "better", so a stage could
run, change nothing, and nobody noticed (`MacroShortageBooster` against a 35% fat gap),
or move the plan further from target and nobody noticed either (`CalorieTrimmer` on a
plan already 3.7% under). One measure, used by every stage, removes both.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

MACROS = ("calories", "protein", "carb", "fat")

#: Below these absolute misses a percentage is not information. A 200 kcal snack with a
#: 30/50/20 split asks for 4.4 g of fat, and a 20% band on that is plus or minus nine
#: tenths of a gram — a fifth of a teaspoon. No system that serves food in amounts
#: people recognise can hit it, so every snack recipe was rejected for missing a target
#: finer than the resolution of a spoon, and the snack slot fell back to assembly on
#: every single day measured. These are also below the accuracy of the nutrition data
#: itself, which is quoted to the gram per hundred grams.
NEGLIGIBLE = {"calories": 40.0, "protein": 4.0, "carb": 6.0, "fat": 3.0}


@dataclass(frozen=True)
class MacroDeviation:
    """Signed relative deviation from target, per macro. 0.0 means exactly on target."""

    calories: float = 0.0
    protein: float = 0.0
    carb: float = 0.0
    fat: float = 0.0
    #: The same misses in grams and kilocalories. A ratio alone cannot say whether a
    #: deviation matters, because it has thrown away the size of the thing it is a
    #: deviation from.
    absolute: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, float]:
        return {m: getattr(self, m) for m in MACROS}

    def worst(self, tolerance: Dict[str, float]) -> tuple[str, float]:
        """The macro furthest outside its own tolerance band.

        Normalising by each macro's tolerance is what lets protein (0.15) outrank carbs
        (0.20) at the same raw deviation — the tolerances were declared in settings and
        previously read by nothing.
        """
        worst_macro, worst_score = "calories", 0.0
        for macro in MACROS:
            tol = max(tolerance.get(macro, 0.10), 1e-9)
            score = abs(getattr(self, macro)) / tol
            if score > worst_score:
                worst_macro, worst_score = macro, score
        return worst_macro, worst_score

    def within(self, tolerance: Dict[str, float]) -> bool:
        """Inside every macro's own band, or missing it by an amount too small to mean
        anything."""
        for macro in MACROS:
            if abs(getattr(self, macro)) <= tolerance.get(macro, 0.10):
                continue
            missed = abs(self.absolute.get(macro, float("inf")))
            if missed <= NEGLIGIBLE.get(macro, 0.0):
                continue
            return False
        return True

    #: Energy's share of the objective. Calories used to be excluded entirely, because
    #: the optimiser was a hill-climb that stopped as soon as a move failed to improve:
    #: a move correctly cutting a +30% carbohydrate surplus also cut calories, the
    #: doubly-counted total failed to improve, the move was rejected and the surplus
    #: stayed. There is no such loop now — portions are chosen by enumerating a food's
    #: own ladder — so nothing is rejected for improving two things at once, and
    #: leaving energy out of the objective simply meant nothing steered it. Every plan
    #: then landed 2 to 4% under target, in the same direction every time.
    CALORIE_WEIGHT = 0.5

    @property
    def magnitude(self) -> float:
        """Single scalar for comparing two candidate plans."""
        macros = sum(abs(getattr(self, m)) for m in ("protein", "carb", "fat"))
        return macros + self.CALORIE_WEIGHT * abs(self.calories)

    def human(self) -> str:
        return ", ".join(f"{m} {getattr(self, m):+.1%}" for m in MACROS)


def totals_of(components) -> Dict[str, float]:
    """Calories and macros for a list of (food, grams).

    The one implementation. There were three: this arithmetic in `optimize`, a second
    copy in `portion` reading the per-gram columns instead of the per-hundred-gram ones,
    and the estimates the planner writes onto each ingredient. `calories` and
    `calories_per_gram` are separate stored columns reconciled only when one of them is
    zero, so two stages could add the same meal up and disagree.
    """
    out = {"calories": 0.0, "protein": 0.0, "carb": 0.0, "fat": 0.0}
    for food, grams in components:
        g = float(grams or 0.0) / 100.0
        out["calories"] += float(getattr(food, "calories", 0) or 0) * g
        out["protein"] += float(getattr(food, "protein", 0) or 0) * g
        out["carb"] += float(getattr(food, "carbs", 0) or 0) * g
        out["fat"] += float(getattr(food, "fat", 0) or 0) * g
    return out


def deviation_of(totals: Dict[str, float], targets: Dict[str, float]) -> MacroDeviation:
    """Relative deviation of achieved totals from target."""
    out, absolute = {}, {}
    for macro in MACROS:
        target = float(targets.get(macro, 0.0) or 0.0)
        actual = float(totals.get(macro, 0.0) or 0.0)
        out[macro] = 0.0 if target <= 0 else (actual - target) / target
        absolute[macro] = actual - target
    return MacroDeviation(absolute=absolute, **out)
