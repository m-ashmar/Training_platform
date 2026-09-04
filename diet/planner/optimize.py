"""Convergence.

**Root B.** The old design was: greedy fill that deliberately overshot ("10% slack for
the first two macros"), followed by seven correctors run once each in a fixed order,
with no shared objective and no measurement of whether a stage helped. Traced on a real
plan it reached **+4.1% (inside tolerance) after stage 3 and then degraded to -6.6%**,
and that degraded plan is what shipped. `CalorieTrimmer` trimmed a plan already under
target; `MacroShortageBooster` did nothing against a 35% fat gap; `SnackCalorieEnforcer`
had to be invoked twice because later stages broke what it set.

Here the correctors become *moves*. A move is chosen because it addresses the macro that
is furthest outside its own tolerance, it is kept only if it improved the objective, and
the best plan seen is what gets returned. Three of the five findings dissolve rather than
being patched.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .policy import PlannerPolicy
from .report import MacroDeviation, deviation_of

logger = logging.getLogger(__name__)

# A meal is a list of (FoodItem, grams).
Components = List[Tuple[object, float]]


def totals_of(components: Components) -> Dict[str, float]:
    """Macro totals for a component list."""
    out = {"calories": 0.0, "protein": 0.0, "carb": 0.0, "fat": 0.0}
    for food, grams in components:
        g = float(grams or 0.0) / 100.0
        out["calories"] += float(getattr(food, "calories", 0) or 0) * g
        out["protein"] += float(getattr(food, "protein", 0) or 0) * g
        out["carb"] += float(getattr(food, "carbs", 0) or 0) * g
        out["fat"] += float(getattr(food, "fat", 0) or 0) * g
    return out


@dataclass
class OptimizeResult:
    components: Components
    deviation: MacroDeviation
    passes: int
    converged: bool
    trace: List[str] = field(default_factory=list)


def _slot(food) -> str:
    from .candidates import classify_food
    return classify_food(food)


def _macro_of(food, macro: str) -> float:
    return float(getattr(food, {"protein": "protein", "carb": "carbs",
                                "fat": "fat"}.get(macro, "calories"), 0) or 0)


def _snapped(components: Components, factor: float) -> Components:
    """Rescale a meal and land every line on an amount a person would serve."""
    from .portion import nearest_portion

    return [(food, nearest_portion(food, float(grams) * factor).grams)
            for food, grams in components]


#: The whole-meal scale is searched rather than hill-climbed. A meal that is 30% short
#: on fat cannot be fixed by adding oil past two tablespoons, and a hill-climber that
#: is told to make a 5% adjustment on a ladder whose smallest rung is half a unit
#: overshoots, fails its own improve-or-stop test, and gives up on pass zero. Searching
#: is both simpler and stronger: 211 evaluations of a four-line meal is nothing.
SCALE_LO, SCALE_HI, SCALE_STEP = 0.4, 2.5, 0.01


def _better(candidate, incumbent, tolerance: Dict[str, float]) -> bool:
    """Is `candidate` the deviation to keep?

    Inside tolerance beats lowest magnitude, because they are not the same point.
    Magnitude averages the four numbers; `within` demands each one separately, so the
    smallest average is regularly a portioning that fails on one macro while a
    neighbouring one passes on all of them.
    """
    if incumbent is None:
        return True
    inside, was_inside = candidate.within(tolerance), incumbent.within(tolerance)
    if inside != was_inside:
        return inside
    return candidate.magnitude < incumbent.magnitude - 1e-9


def refine(components: Components, targets: Dict[str, float],
           tolerance: Dict[str, float], movable: Optional[Sequence[int]] = None,
           max_passes: int = 4) -> Tuple[Components, "MacroDeviation"]:
    """Move one portion at a time to a different rung, keeping only what helps.

    Scaling a whole meal preserves its composition, which is right for a dish and not
    enough for a meal that is the right size and the wrong shape: a recipe whose macro
    ratio does not match the target cannot be fixed by serving more of all of it. This
    adjusts the balance, still only ever between amounts the food itself declares.

    `movable` restricts which lines may change, so a recipe's pinch of salt stays a
    pinch while its rice moves.
    """
    from .portion import portions_for

    best = list(components)
    best_dev = deviation_of(totals_of(best), targets)
    indices = list(range(len(best))) if movable is None else list(movable)
    ladders = {i: portions_for(best[i][0]) for i in indices}

    for _ in range(max_passes):
        improved = False
        for index in indices:
            food = best[index][0]
            for option in ladders[index]:
                if abs(option.grams - best[index][1]) < 1e-6:
                    continue
                candidate = list(best)
                candidate[index] = (food, option.grams)
                dev = deviation_of(totals_of(candidate), targets)
                if _better(dev, best_dev, tolerance):
                    best, best_dev, improved = candidate, dev, True
        if not improved:
            break
    return best, best_dev


def optimize_meal(components: Components, targets: Dict[str, float],
                  policy: PlannerPolicy) -> OptimizeResult:
    """Bring a meal as close to its macro targets as servable portions allow.

    Two stages, both searches over the same discrete space: the amounts each food
    declares a person would serve. First the whole meal is scaled, which keeps its
    composition; then single portions are adjusted, which can fix a meal that is the
    right size and the wrong shape.

    The result is on the ladder by construction. The previous implementation was a
    hill-climb over continuous gram figures bounded only by a per-macro cap, and it ran
    immediately after the portion solver — so it took a snapped 225 g of rice back up
    to the 300 g carbohydrate cap and 198 g of egg white to 280 g. An input that is
    already off the ladder is repaired here rather than preserved.
    """
    if not components:
        return OptimizeResult(components, MacroDeviation(), 0, False, ["empty meal"])

    trace: List[str] = []
    best: Optional[Components] = None
    best_dev: Optional[MacroDeviation] = None

    # Inside tolerance beats lowest magnitude. Magnitude averages the four numbers;
    # `within` demands each one separately, so the smallest average is frequently a
    # scale that fails on one macro while a neighbouring scale passes on all of them.
    steps = int(round((SCALE_HI - SCALE_LO) / SCALE_STEP)) + 1
    best_factor = 1.0
    for index in range(steps):
        factor = SCALE_LO + index * SCALE_STEP
        candidate = _snapped(components, factor)
        dev = deviation_of(totals_of(candidate), targets)
        if _better(dev, best_dev, policy.tolerance):
            best, best_dev, best_factor = candidate, dev, factor

    trace.append(f"scale x{best_factor:.2f} -> {best_dev.human()}")
    if not best_dev.within(policy.tolerance):
        refined, refined_dev = refine(best, targets, policy.tolerance)
        if _better(refined_dev, best_dev, policy.tolerance):
            trace.append(f"refine portions -> {refined_dev.human()}")
            best, best_dev = refined, refined_dev

    converged = best_dev.within(policy.tolerance)
    if not converged:
        trace.append("no servable combination reaches tolerance")
    return OptimizeResult(best, best_dev, len(trace), converged, trace)
