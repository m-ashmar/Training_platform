"""Ground truth for the portion optimiser.

A meal is a handful of foods, each of which may be served at one of a handful of
amounts. That space is small enough to enumerate completely, which means the best
possible portioning of a given meal is not a matter of opinion: it can be computed and
compared against what the engine actually served.

This turns the acceptance criterion from "nothing failed" into "every feasible meal is
at the proven optimum, or off it by a stated amount". The distinction matters because a
green gate says only that no assertion tripped. It cannot tell you the optimiser settled
for a portioning it had the information to beat.

Three outcomes per meal, and the third is not a defect:

* **at the optimum** — no valid combination scores better on the objective.
* **off the optimum** — one does, and the optimiser did not find it.
* **infeasible** — no combination of servable amounts is inside tolerance at all. The
  target cannot be met with these foods, whatever the search does.

Usage::

    from tests.optimiser_benchmark import run
    report = run(days=3)
    print(report.summary())
"""
from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: A meal whose ladder product is larger than this is measured, not enumerated. Five
#: foods at thirteen rungs is 371,293 combinations, and the benchmark would spend longer
#: proving one meal optimal than the engine spends planning a week.
MAX_COMBINATIONS = 60_000

#: Calorie targets spanning the engine's accepted range, so the benchmark sees both the
#: easy middle and the ends where the ladder starts to bind.
BENCH_KCAL: Sequence[int] = (1400, 1800, 2200, 2600, 3000)


@dataclass
class MealResult:
    """One meal, judged against every portioning it could have had."""

    slot: str
    dish: str
    foods: Tuple[str, ...]
    combinations: int
    feasible: bool
    served_magnitude: float = 0.0
    optimum_magnitude: float = 0.0
    served_calorie_error: float = 0.0
    best_calorie_error: float = 0.0

    @property
    def objective_gap(self) -> float:
        """How much worse the served portioning is than the best one available."""
        return max(0.0, self.served_magnitude - self.optimum_magnitude)

    @property
    def calorie_gap(self) -> float:
        """Kilocalories of accuracy left on the table, against the best VALID fit."""
        return max(0.0, self.served_calorie_error - self.best_calorie_error)

    @property
    def at_optimum(self) -> bool:
        return self.feasible and self.objective_gap <= 1e-6


@dataclass
class BenchmarkReport:
    results: List[MealResult] = field(default_factory=list)
    skipped_too_large: int = 0
    elapsed_seconds: float = 0.0
    plan_seconds: float = 0.0

    @property
    def feasible(self) -> List[MealResult]:
        return [r for r in self.results if r.feasible]

    @property
    def infeasible(self) -> List[MealResult]:
        return [r for r in self.results if not r.feasible]

    @property
    def at_optimum(self) -> List[MealResult]:
        return [r for r in self.feasible if r.at_optimum]

    @property
    def off_optimum(self) -> List[MealResult]:
        return [r for r in self.feasible if not r.at_optimum]

    @property
    def worst_objective_gap(self) -> float:
        return max((r.objective_gap for r in self.feasible), default=0.0)

    @property
    def worst_calorie_gap(self) -> float:
        return max((r.calorie_gap for r in self.feasible), default=0.0)

    @property
    def optimal_share(self) -> float:
        return len(self.at_optimum) / len(self.feasible) if self.feasible else 1.0

    def summary(self) -> str:
        lines = [
            f"{'meals enumerated exhaustively':<38}{len(self.results)}",
            f"{'  feasible (a valid portioning exists)':<38}{len(self.feasible)}",
            f"{'  infeasible (target unreachable)':<38}{len(self.infeasible)}",
            f"{'  skipped, space too large':<38}{self.skipped_too_large}",
            f"{'at the proven optimum':<38}{len(self.at_optimum)}"
            f" ({self.optimal_share:.0%} of feasible)",
            f"{'off the optimum':<38}{len(self.off_optimum)}",
            f"{'worst objective gap':<38}{self.worst_objective_gap:.4f}",
            f"{'worst calorie gap':<38}{self.worst_calorie_gap:.0f} kcal",
            f"{'planning time':<38}{self.plan_seconds:.2f}s",
            f"{'benchmark time':<38}{self.elapsed_seconds:.2f}s",
        ]
        for r in sorted(self.off_optimum, key=lambda r: -r.objective_gap)[:10]:
            lines.append(
                f"    {r.slot:<10} {r.dish[:26]:<28} objective {r.served_magnitude:.3f}"
                f" vs {r.optimum_magnitude:.3f}   kcal {r.served_calorie_error:.0f}"
                f" vs {r.best_calorie_error:.0f}")
        return "\n".join(lines)


def _contribution_rows(foods, ladders):
    """Per-rung (grams, kcal, protein, carb, fat) so enumeration is plain addition."""
    from diet.planner.portion import kcal_of, macro_of

    rows = []
    for food, rungs in zip(foods, ladders):
        k = kcal_of(food)
        p = macro_of(food, "protein")
        c = macro_of(food, "carb")
        f = macro_of(food, "fat")
        rows.append([(g, g * k, g * p, g * c, g * f) for g in rungs])
    return rows


def exhaustive_optimum(foods: Sequence, ladders: Sequence[Sequence[float]],
                       targets: Dict[str, float], tolerance: Dict[str, float]):
    """The best portioning that exists, by enumeration.

    Returns `(best_magnitude, best_calorie_error, combinations)`. Both are `None` when no
    combination is inside tolerance, which means the target is unreachable with these
    foods rather than that the optimiser failed.
    """
    from diet.planner.report import deviation_of

    rows = _contribution_rows(foods, ladders)
    combos = 1
    for row in rows:
        combos *= max(len(row), 1)

    best_mag = best_kcal = None
    for combo in itertools.product(*rows):
        totals = {
            "calories": sum(x[1] for x in combo),
            "protein": sum(x[2] for x in combo),
            "carb": sum(x[3] for x in combo),
            "fat": sum(x[4] for x in combo),
        }
        dev = deviation_of(totals, targets)
        if not dev.within(tolerance):
            continue
        error = abs(dev.absolute["calories"])
        if best_mag is None or dev.magnitude < best_mag:
            best_mag = dev.magnitude
        if best_kcal is None or error < best_kcal:
            best_kcal = error
    return best_mag, best_kcal, combos


def judge_meal(meal, targets: Dict[str, float], tolerance: Dict[str, float],
               resolve) -> Optional[MealResult]:
    """Compare one served meal against every portioning it could have had."""
    from diet.planner.optimize import totals_of
    from diet.planner.portion import portions_for
    from diet.planner.report import deviation_of

    served = []
    for ingredient in meal.ingredients:
        food = resolve(ingredient.name)
        if food is None:
            return None
        served.append((food, float(str(ingredient.quantity).replace("g", ""))))
    if not served:
        return None

    foods = [f for f, _g in served]
    ladders = [[p.grams for p in portions_for(f)] for f in foods]
    space = 1
    for ladder in ladders:
        space *= max(len(ladder), 1)
    if space > MAX_COMBINATIONS:
        return None

    served_dev = deviation_of(totals_of(served), targets)
    best_mag, best_kcal, combos = exhaustive_optimum(foods, ladders, targets, tolerance)
    return MealResult(
        slot=meal.meal_type or "?",
        dish=meal.meal_name or "?",
        foods=tuple(f.name for f in foods),
        combinations=combos,
        feasible=best_mag is not None,
        served_magnitude=served_dev.magnitude,
        optimum_magnitude=best_mag if best_mag is not None else 0.0,
        served_calorie_error=abs(served_dev.absolute["calories"]),
        best_calorie_error=best_kcal if best_kcal is not None else 0.0,
    )


def run(days: int = 3, kcal_targets: Sequence[int] = BENCH_KCAL,
        profiles: Optional[Sequence[tuple]] = None) -> BenchmarkReport:
    """Plan real days, then prove or disprove the portioning of every meal in them."""
    from diet.models import FoodItem
    from diet.planner.policy import load_policy
    from diet.planner.targets import compute_targets
    from diet.services.rule_based_planner import RuleBasedPlanner

    from tests.diet_quality import PROFILES, _make_client

    profiles = profiles if profiles is not None else PROFILES
    cache: Dict[str, object] = {}

    def resolve(name: str):
        if name not in cache:
            cache[name] = FoodItem.objects.filter(name=name).first()
        return cache[name]

    report = BenchmarkReport()
    started = time.perf_counter()
    for kcal in kcal_targets:
        for profile in profiles:
            user = _make_client(f"bm{uuid.uuid4().hex[:8]}", *profile)
            planner = RuleBasedPlanner(user)
            t0 = time.perf_counter()
            out = planner.generate(daily_kcal=kcal, duration_days=days, snack_count=1)
            report.plan_seconds += time.perf_counter() - t0

            # The client's OWN policy. Judging a muscle-gain plan against the maintain
            # split makes every meal look broken and is how a first draft of this
            # benchmark reported 62 failures that were not failures.
            policy = load_policy(planner._resolve_goal())
            # Judged against the targets the planner built to, bodyweight included.
            targets = compute_targets(kcal, policy, ["Breakfast", "Lunch", "Dinner"], 1,
                                      weight_kg=getattr(user, "weight", None))
            by_slot = {t.name: t for t in targets.meals}

            for meal in out.plan:
                # The target the meal was BUILT to. Meals compensate each other, so a
                # later slot's target is the split plus the earlier residual; judging
                # against the unshifted split reported 95 of 192 meals off an optimum
                # they were never aimed at.
                built_to = getattr(meal, "target", None)
                target = by_slot.get(meal.meal_type)
                if built_to is None and target is None:
                    continue
                result = judge_meal(meal, dict(built_to) if built_to else target.as_dict(),
                                    policy.tolerance, resolve)
                if result is None:
                    report.skipped_too_large += 1
                else:
                    report.results.append(result)
    report.elapsed_seconds = time.perf_counter() - started
    return report
