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
from .report import better as _better, MacroDeviation, deviation_of, totals_of

logger = logging.getLogger(__name__)

# A meal is a list of (FoodItem, grams).
Components = List[Tuple[object, float]]




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


def _at_ladder_ends(components: Components, ladders: dict) -> int:
    """How many lines sit on the first or last rung of their own ladder."""
    count = 0
    for i, (_food, grams) in enumerate(components):
        ladder = ladders.get(i)
        if not ladder or len(ladder) < 2:
            continue
        if (abs(ladder[0].grams - grams) < 1e-6
                or abs(ladder[-1].grams - grams) < 1e-6):
            count += 1
    return count


def _snapped(components: Components, factor: float,
             movable: Optional[List[int]] = None,
             ladders: Optional[dict] = None) -> Components:
    """Rescale a meal and land every line on an amount a person would serve.

    `movable` names the lines the caller allows to change. A recipe's author pins the
    pinch of salt and the teaspoon of oil; scaling those with the rest is how a garnish
    reached its ladder ceiling during convergence.

    `ladders` is each line's portion ladder, built once by the caller. Without it this
    rebuilt every ladder on every one of the 211 scale steps — `unit_levels` plus
    `fallback_cap` plus `classify_food` per food per step, about a thousand rebuilds for
    a five-line meal, and one `optimize_meal` per meal per day inside convergence.
    """
    from .portion import portions_for

    allowed = None if movable is None else set(movable)
    out = []
    for i, (food, grams) in enumerate(components):
        if allowed is not None and i not in allowed:
            out.append((food, grams))
            continue
        options = (ladders or {}).get(i) or portions_for(food)
        want = float(grams) * factor
        out.append((food, min(options, key=lambda p: abs(p.grams - want)).grams))
    return out


#: The whole-meal scale is searched rather than hill-climbed. A meal that is 30% short
#: on fat cannot be fixed by adding oil past two tablespoons, and a hill-climber that
#: is told to make a 5% adjustment on a ladder whose smallest rung is half a unit
#: overshoots, fails its own improve-or-stop test, and gives up on pass zero. Searching
#: is both simpler and stronger: 211 evaluations of a four-line meal is nothing.
SCALE_LO, SCALE_HI, SCALE_STEP = 0.4, 2.5, 0.01


#: A pair pass costs C(n,2) x |ladder_i| x |ladder_j|. A meal is one food per slot, so
#: five foods at thirteen rungs is about 1,700 evaluations — nothing. The guard is here
#: so a future meal shape with more parts degrades to single moves rather than stalling
#: the planner.
MAX_PAIR_EVALUATIONS = 20_000


def _single_pass(best: Components, best_dev, targets, tolerance, indices, ladders):
    """Try every amount of every food, one food at a time."""
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
    return best, best_dev, improved


def _pair_pass(best: Components, best_dev, targets, tolerance, indices, ladders):
    """Try every amount of every two foods, changed together as one candidate.

    This is the escape from the local minima single moves cannot leave. A meal that is
    over on carbohydrate and under on protein needs the rice down AND the chicken up:
    either move alone makes the objective worse, so a search that only ever moves one
    food declares itself finished and stops. Measured on 327 meals, that accounted for
    the large majority of every portioning the engine served that it could have beaten.

    The pair is atomic. Both amounts change, then the whole meal is judged, which is
    what makes a move that is individually worse but jointly better visible at all.
    """
    improved = False
    for position, left in enumerate(indices):
        for right in indices[position + 1:]:
            if len(ladders[left]) * len(ladders[right]) > MAX_PAIR_EVALUATIONS:
                continue
            left_food, right_food = best[left][0], best[right][0]
            # Everything but the two lines being moved is summed once per pair; the
            # candidate's totals are that plus the two lines. This loop is the
            # engine's hottest (700k evaluations per week-long plan) and summing all
            # five lines for every combination was most of its cost.
            rest = totals_of([c for i, c in enumerate(best) if i not in (left, right)])
            for left_option in ladders[left]:
                left_part = totals_of([(left_food, left_option.grams)])
                for right_option in ladders[right]:
                    if (abs(left_option.grams - best[left][1]) < 1e-6
                            and abs(right_option.grams - best[right][1]) < 1e-6):
                        continue
                    right_part = totals_of([(right_food, right_option.grams)])
                    totals = {k: rest[k] + left_part[k] + right_part[k] for k in rest}
                    dev = deviation_of(totals, targets)
                    if _better(dev, best_dev, tolerance):
                        candidate = list(best)
                        candidate[left] = (left_food, left_option.grams)
                        candidate[right] = (right_food, right_option.grams)
                        best, best_dev, improved = candidate, dev, True
    return best, best_dev, improved


#: How far a line may move in the three-at-once pass, in rungs either way. The full
#: cross-product of three ladders is C(n,3) x 13^3 — tens of thousands of evaluations per
#: meal — so the move is a neighbourhood instead: (2w+1)^3 per triple.
#:
#: Measured over 240 meals — share at the proven optimum / worst objective gap / seconds
#: to plan them. `lines` is how many of a meal's lines the pass ranges over, largest
#: energy contributor first; `width` is how many rungs either side of the current amount
#: each of the three may move.
#:
#:     no triple pass            0.920   0.088    33s
#:     width 1, all lines        0.925   0.088    36s
#:     width 3, all lines        0.945   0.073    69s
#:     width 4, all lines        0.960   0.048    87s
#:     width 4, 5 lines          0.945   0.048    60s
#:     width 4, 6 lines          0.965   0.048    77s   <- chosen
#:
#: Ranging over every line is both slower AND slightly worse than stopping at six: the
#: seventh line of a dinner is a herb or a squeeze of lemon, it cannot move a macro, and
#: including it spends the pass's improvement budget on triples that go nowhere.
TRIPLE_NEIGHBOURHOOD = 4
TRIPLE_LINES = 6


def _neighbours(ladder, grams, width):
    """The current rung and the `width` rungs either side of it."""
    if not ladder:
        return []
    at = min(range(len(ladder)), key=lambda i: abs(ladder[i].grams - grams))
    lo = max(0, at - width)
    hi = min(len(ladder), at + width + 1)
    return ladder[lo:hi]


def _triple_pass(best: Components, best_dev, targets, tolerance, indices, ladders,
                 width: int = TRIPLE_NEIGHBOURHOOD):
    """Move three lines at once, each by at most one rung.

    Pairs escape the local minima single moves cannot, and triples escape the ones pairs
    cannot: a six-line dinner that is over on carbohydrate, under on protein and over on
    fat needs the potato down, the mince up AND the oil down, and every two of those
    three make the objective worse on their own. Measured against the exhaustive
    optimum, meals of five to seven lines were where the engine still settled short.
    """
    improved = False
    # The lines that carry the meal's energy, largest first. A triple of a garnish, a
    # herb and a squeeze of lemon cannot move a macro; including them turned C(4,3)=4
    # triples on a seven-line dinner into C(7,3)=35 and bought nothing.
    ranked = sorted(indices,
                    key=lambda i: totals_of([best[i]])["calories"], reverse=True)
    considered = ranked[:TRIPLE_LINES]
    for i_pos, first in enumerate(considered):
        for j_pos, second in enumerate(considered[i_pos + 1:], start=i_pos + 1):
            for third in considered[j_pos + 1:]:
                rest = totals_of([c for k, c in enumerate(best)
                                  if k not in (first, second, third)])
                current = (best[first][1], best[second][1], best[third][1])
                options = [_neighbours(ladders[k], best[k][1], width)
                           for k in (first, second, third)]
                for a_opt in options[0]:
                    a_part = totals_of([(best[first][0], a_opt.grams)])
                    for b_opt in options[1]:
                        b_part = totals_of([(best[second][0], b_opt.grams)])
                        for c_opt in options[2]:
                            if (a_opt.grams, b_opt.grams, c_opt.grams) == current:
                                continue
                            c_part = totals_of([(best[third][0], c_opt.grams)])
                            totals = {k: rest[k] + a_part[k] + b_part[k] + c_part[k]
                                      for k in rest}
                            dev = deviation_of(totals, targets)
                            if _better(dev, best_dev, tolerance):
                                candidate = list(best)
                                candidate[first] = (best[first][0], a_opt.grams)
                                candidate[second] = (best[second][0], b_opt.grams)
                                candidate[third] = (best[third][0], c_opt.grams)
                                best, best_dev, improved = candidate, dev, True
                                current = (a_opt.grams, b_opt.grams, c_opt.grams)
    return best, best_dev, improved


def refine(components: Components, targets: Dict[str, float],
           tolerance: Dict[str, float], movable: Optional[Sequence[int]] = None,
           max_passes: int = 6,
           ladders: Optional[dict] = None) -> Tuple[Components, "MacroDeviation"]:
    """Search the neighbourhood of a portioning, widening the move only when it stalls.

    Scaling a whole meal preserves its composition, which is right for a dish and not
    enough for a meal that is the right size and the wrong shape: a recipe whose macro
    ratio does not match the target cannot be fixed by serving more of all of it.

    Single moves first, because they are cheap and usually sufficient. Paired moves only
    when no single move helps, because that is the signature of a local minimum rather
    than an optimum. Every amount comes from the food's own ladder, so the ceilings,
    minimums and step sizes hold by construction, and the set of foods never changes, so
    allergens, dislikes and the meal's shape are untouched by anything here.

    `movable` restricts which lines may change, so a recipe's pinch of salt stays a
    pinch while its rice moves.
    """
    from .portion import portions_for

    best = list(components)
    best_dev = deviation_of(totals_of(best), targets)
    indices = list(range(len(best))) if movable is None else list(movable)
    if not indices:
        return best, best_dev
    ladders = ladders or {i: portions_for(best[i][0]) for i in indices}

    for _ in range(max_passes):
        best, best_dev, improved = _single_pass(
            best, best_dev, targets, tolerance, indices, ladders)
        if not improved:
            best, best_dev, improved = _pair_pass(
                best, best_dev, targets, tolerance, indices, ladders)
        if not improved and len(indices) >= 3:
            best, best_dev, improved = _triple_pass(
                best, best_dev, targets, tolerance, indices, ladders)
        if not improved:
            break
    return best, best_dev


def optimize_meal(components: Components, targets: Dict[str, float],
                  policy: PlannerPolicy,
                  movable: Optional[List[int]] = None) -> OptimizeResult:
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
    # Built once for the whole search and handed to both stages.
    from .portion import portions_for
    movable_indices = (list(range(len(components))) if movable is None else list(movable))
    ladders = {i: portions_for(components[i][0]) for i in movable_indices}
    best_pinned = None
    for index in range(steps):
        factor = SCALE_LO + index * SCALE_STEP
        candidate = _snapped(components, factor, movable, ladders)
        dev = deviation_of(totals_of(candidate), targets)
        pinned = _at_ladder_ends(candidate, ladders)
        if _better(dev, best_dev, policy.tolerance):
            best, best_dev, best_factor, best_pinned = candidate, dev, factor, pinned
        elif (best_dev is not None and best_pinned is not None
              and pinned < best_pinned
              and dev.within(policy.tolerance) == best_dev.within(policy.tolerance)
              and abs(dev.magnitude - best_dev.magnitude) < 1e-9):
            # Same accuracy, fewer lines parked at the end of their own ladder. Scaling
            # a whole meal moves every line by the same factor, and a line that carries
            # almost no energy — the half lemon, the glass of coffee, the figs — hits its
            # ceiling long before the factor stops helping the lines that do carry
            # energy. It then STAYS there, because moving it back changes the objective
            # by nothing and `_better` keeps only improvements. Measured on the harness,
            # three foods were served at exactly one amount every single time, and it
            # was the top of their ladder each time: six figs, a large coffee, half a
            # lemon. This is a pure tie-break — it cannot cost a gram of accuracy.
            best, best_dev, best_factor, best_pinned = candidate, dev, factor, pinned

    trace.append(f"scale x{best_factor:.2f} -> {best_dev.human()}")
    if not best_dev.within(policy.tolerance):
        refined, refined_dev = refine(best, targets, policy.tolerance, movable,
                                      ladders=ladders)
        if _better(refined_dev, best_dev, policy.tolerance):
            trace.append(f"refine portions -> {refined_dev.human()}")
            best, best_dev = refined, refined_dev

    converged = best_dev.within(policy.tolerance)
    if not converged:
        trace.append("no servable combination reaches tolerance")
    return OptimizeResult(best, best_dev, len(trace), converged, trace)
