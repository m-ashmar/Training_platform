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
from typing import Callable, Dict, List, Optional, Sequence, Tuple

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
class Move:
    """One adjustment the optimiser can make, and which deviation it addresses."""

    name: str
    macro: str
    direction: int            # +1 raises the macro, -1 lowers it
    apply: Callable[[Components, PlannerPolicy, float], Components]


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


def _scale_macro(components: Components, policy: PlannerPolicy,
                 macro: str, factor: float) -> Components:
    """Scale the foods richest in `macro`, respecting portion bounds.

    Only the contributors are touched, so raising protein does not silently inflate
    carbohydrate as a whole-meal rescale would.
    """
    from .candidates import classify_food

    out: Components = []
    for food, grams in components:
        if classify_food(food) == macro and _macro_of(food, macro) > 0:
            cap = policy.cap_for(macro)
            new = min(cap, max(policy.floor_portion_for(macro), float(grams) * factor))
            out.append((food, policy.round_grams(new)))
        else:
            out.append((food, grams))
    return out


def _build_moves() -> List[Move]:
    """The move set. Each mirrors one of the original correctors, minus the blind firing."""
    moves: List[Move] = []
    for macro in ("protein", "carb", "fat"):
        moves.append(Move(f"raise_{macro}", macro, +1,
                          lambda c, p, s, m=macro: _scale_macro(c, p, m, 1.0 + s)))
        moves.append(Move(f"lower_{macro}", macro, -1,
                          lambda c, p, s, m=macro: _scale_macro(c, p, m, 1.0 - s)))
    return moves


MOVES = _build_moves()


def optimize_meal(components: Components, targets: Dict[str, float],
                  policy: PlannerPolicy) -> OptimizeResult:
    """Nudge a meal toward its macro targets, keeping the best version seen."""
    if not components:
        return OptimizeResult(components, MacroDeviation(), 0, False, ["empty meal"])

    best = list(components)
    best_dev = deviation_of(totals_of(best), targets)
    current = list(components)
    trace: List[str] = []

    for step in range(policy.max_optimiser_passes):
        dev = deviation_of(totals_of(current), targets)
        if dev.within(policy.tolerance):
            trace.append(f"pass {step}: within tolerance")
            best, best_dev = current, dev
            break

        macro, _score = dev.worst(policy.tolerance)

        # Energy out while every macro is in means the portion is simply the wrong size —
        # the dish is right, there is just too much or too little of it. Scaling the whole
        # meal keeps its composition. Without this move a recipe that was -4.9% protein,
        # +3.4% carb and -7.4% fat was still rejected for being +11% calories.
        macros_ok = all(abs(getattr(dev, m)) <= policy.tolerance.get(m, 0.2)
                        for m in ("protein", "carb", "fat"))
        if macros_ok and abs(dev.calories) > policy.tolerance.get("calories", 0.1):
            factor = 1.0 / (1.0 + dev.calories)
            factor = max(0.6, min(1.6, factor))
            scaled = [(food, policy.round_grams(
                max(policy.floor_portion_for(_slot(food)),
                    min(policy.cap_for(_slot(food)), float(grams) * factor))))
                for food, grams in current]
            scaled_dev = deviation_of(totals_of(scaled), targets)
            if abs(scaled_dev.calories) < abs(dev.calories):
                current = scaled
                trace.append(f"pass {step}: scale_portion x{factor:.2f} "
                             f"calories {dev.calories:+.1%} -> {scaled_dev.calories:+.1%}")
                if scaled_dev.within(policy.tolerance):
                    best, best_dev = current, scaled_dev
                    trace.append(f"pass {step}: within tolerance")
                    break
                if scaled_dev.magnitude < best_dev.magnitude:
                    best, best_dev = current, scaled_dev
                continue
            trace.append(f"pass {step}: scale_portion did not help; stopping")
            break

        if macro == "calories":
            # Calories are not adjusted directly; the macro furthest out is what moved
            # them. Excluding it prevents a whole-meal rescale that fixes the headline
            # number while leaving the real shortfall untouched — exactly the failure
            # where fat sat at -37% while the pipeline chased a -6.6% calorie gap.
            macro, _score = max(
                (("protein", abs(dev.protein) / max(policy.tolerance.get("protein", .15), 1e-9)),
                 ("carb", abs(dev.carb) / max(policy.tolerance.get("carb", .2), 1e-9)),
                 ("fat", abs(dev.fat) / max(policy.tolerance.get("fat", .2), 1e-9))),
                key=lambda t: t[1],
            )

        need = getattr(dev, macro)
        direction = -1 if need > 0 else +1
        move = next((m for m in MOVES if m.macro == macro and m.direction == direction), None)
        if move is None:
            break

        # Step proportional to the gap, damped so it converges instead of oscillating.
        candidate = move.apply(current, policy, min(0.5, abs(need) * 0.6))
        cand_dev = deviation_of(totals_of(candidate), targets)

        if cand_dev.magnitude >= dev.magnitude - 1e-6:
            # The move did not help. Stopping here is what turns a silent no-op — the
            # MacroShortageBooster behaviour — into a visible non-convergence.
            trace.append(f"pass {step}: {move.name} did not improve; stopping")
            break

        current = candidate
        trace.append(f"pass {step}: {move.name} {dev.magnitude:.3f} -> {cand_dev.magnitude:.3f}")
        if cand_dev.magnitude < best_dev.magnitude:
            best, best_dev = current, cand_dev

    return OptimizeResult(best, best_dev, len(trace), best_dev.within(policy.tolerance), trace)
