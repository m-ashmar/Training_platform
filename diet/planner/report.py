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

    def within(self, tolerance: Dict[str, float]) -> bool:
        """Inside every macro's own band, or missing it by an amount too small to mean
        anything."""
        cached = self.__dict__.get("_within")
        if cached is not None and cached[0] is tolerance:
            return cached[1]
        result = True
        for macro in MACROS:
            if abs(getattr(self, macro)) <= tolerance.get(macro, 0.10):
                continue
            missed = abs(self.absolute.get(macro, float("inf")))
            if missed <= NEGLIGIBLE.get(macro, 0.0):
                continue
            result = False
            break
        # `better` asks the incumbent the same question at every step of a search.
        self.__dict__["_within"] = (tolerance, result)
        return result

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


def better(candidate, incumbent, tolerance: Dict[str, float]) -> bool:
    """Is `candidate` the deviation to keep?

    Inside tolerance beats lowest magnitude, because they are not the same point.
    Magnitude averages the four numbers; `within` demands each one separately, so the
    smallest average is regularly a portioning that fails on one macro while a
    neighbouring one passes on all of them. This is the ONLY comparison the engine
    makes between two portionings; the template solver used to keep its own
    (magnitude alone) and discarded fitting plates for closer-on-average ones.
    """
    if incumbent is None:
        return True
    inside, was_inside = candidate.within(tolerance), incumbent.within(tolerance)
    if inside != was_inside:
        return inside
    return candidate.magnitude < incumbent.magnitude - 1e-9


def totals_of(components) -> Dict[str, float]:
    """Calories and macros for a list of (food, grams).

    The one implementation. There were three: this arithmetic in `optimize`, a second
    copy in `portion` reading the per-gram columns instead of the per-hundred-gram ones,
    and the estimates the planner writes onto each ingredient. `calories` and
    `calories_per_gram` are separate stored columns reconciled only when one of them is
    zero, so two stages could add the same meal up and disagree.
    """
    kcal = protein = carb = fat = 0.0
    for food, grams in components:
        g = float(grams or 0.0)
        try:
            k, p, c, f = food._per_gram
        except AttributeError:
            # Read once per food instance. This is the innermost loop of every search
            # (700k calls per week-long plan) and the attribute reads dominated it.
            k, p, c, f = food._per_gram = (
                float(getattr(food, "calories", 0) or 0) / 100.0,
                float(getattr(food, "protein", 0) or 0) / 100.0,
                float(getattr(food, "carbs", 0) or 0) / 100.0,
                float(getattr(food, "fat", 0) or 0) / 100.0)
        kcal += k * g; protein += p * g; carb += c * g; fat += f * g
    return {"calories": kcal, "protein": protein, "carb": carb, "fat": fat}


def deviation_of(totals: Dict[str, float], targets: Dict[str, float]) -> MacroDeviation:
    """Relative deviation of achieved totals from target."""
    out, absolute = {}, {}
    for macro in MACROS:
        target = float(targets.get(macro, 0.0) or 0.0)
        actual = float(totals.get(macro, 0.0) or 0.0)
        out[macro] = 0.0 if target <= 0 else (actual - target) / target
        absolute[macro] = actual - target
    return MacroDeviation(absolute=absolute, **out)
