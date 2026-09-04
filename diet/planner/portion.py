"""How much of a food goes on the plate.

A portion used to be a continuous gram figure produced by filling greedily toward a
macro target and then repairing what that broke. Two things went wrong with it, and
they are the same thing seen twice.

**Amounts nobody would serve.** 350 g of egg white is eleven of them. 370 g of butternut
squash is a plate no one finishes. Both are arithmetically fine and neither is food.

**Minimums that became the answer.** A floor applied after the fill is an attractor: the
algorithm satisfies it as cheaply as it can and stops, so the floor is what you get,
every time. Avocado took exactly two distinct values across twenty-one servings.

Here a portion is a multiple of a unit a person recognises — an egg, a slice, a cup, a
tablespoon — chosen from inside a range the food itself declares. An absurd amount stops
being discouraged and becomes unrepresentable, and a minimum inside the search space
cannot act as an attractor, because the search can see past it.

Foods with no unit fall back to grams under a cap, so an incomplete catalogue degrades
rather than breaks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

#: Portions step by half a unit. Finer than that is not a decision anyone makes when
#: serving: half an avocado, one and a half cups of rice, two eggs.
UNIT_STEP = 0.5

#: When a food carries no unit, cap the gram portion so the fallback cannot reproduce
#: the plates this module exists to prevent.
FALLBACK_MAX_G = 250.0
FALLBACK_MIN_G = 20.0


@dataclass(frozen=True)
class Portion:
    """One food at one amount, and how that amount was arrived at."""

    food: object
    grams: float
    units: Optional[float] = None

    @property
    def described(self) -> str:
        """What a person would say they are eating."""
        unit = getattr(self.food, "household_unit", "") or ""
        if self.units and unit:
            count = int(self.units) if float(self.units).is_integer() else self.units
            plural = "" if count == 1 else "s"
            return f"{count} {unit}{plural}"
        return f"{self.grams:.0f} g"


def unit_levels(food) -> List[float]:
    """Every amount of this food a person might serve, in grams, smallest first.

    An empty list means the food declares no unit and the caller should fall back.
    """
    grams_per_unit = float(getattr(food, "unit_grams", 0) or 0)
    if grams_per_unit <= 0:
        return []
    low = float(getattr(food, "min_units", 0) or UNIT_STEP)
    high = float(getattr(food, "max_units", 0) or 0)
    if high <= 0 or high < low:
        return []

    levels: List[float] = []
    steps = int(round((high - low) / UNIT_STEP))
    for index in range(steps + 1):
        units = round(low + index * UNIT_STEP, 2)
        if units <= 0:
            continue
        levels.append(units)
    return levels


def portions_for(food) -> List[Portion]:
    """Every portion of this food the planner is allowed to choose between."""
    levels = unit_levels(food)
    grams_per_unit = float(getattr(food, "unit_grams", 0) or 0)
    if levels:
        return [Portion(food, round(units * grams_per_unit, 1), units) for units in levels]

    # No unit declared. Offer a coarse gram ladder under a cap rather than a continuum,
    # so the fallback still cannot serve half a kilo of anything.
    return [Portion(food, grams)
            for grams in (25.0, 50.0, 75.0, 100.0, 125.0, 150.0, 200.0, FALLBACK_MAX_G)]


def nearest_portion(food, target_grams: float) -> Portion:
    """The servable amount closest to `target_grams`.

    Closest in either direction, deliberately. Rounding up was one of three mechanisms
    that made every plan land over its calorie target and never under.
    """
    options = portions_for(food)
    if not options:
        capped = max(FALLBACK_MIN_G, min(FALLBACK_MAX_G, float(target_grams)))
        return Portion(food, round(capped, 1))
    return min(options, key=lambda p: abs(p.grams - float(target_grams)))


def macro_of(food, macro: str) -> float:
    """Grams of one macro per gram of this food."""
    attribute = {
        "protein": "protein_per_gram",
        "carb": "carbs_per_gram",
        "fat": "fat_per_gram",
    }.get(macro)
    if not attribute:
        return 0.0
    return float(getattr(food, attribute, 0) or 0)


def kcal_of(food) -> float:
    return float(getattr(food, "calories_per_gram", 0) or 0)


def totals(portions: Sequence[Portion]) -> dict:
    """Calories and macros for a set of portions."""
    result = {"calories": 0.0, "protein": 0.0, "carb": 0.0, "fat": 0.0}
    for portion in portions:
        result["calories"] += kcal_of(portion.food) * portion.grams
        for macro in ("protein", "carb", "fat"):
            result[macro] += macro_of(portion.food, macro) * portion.grams
    return result


def deviation(actual: dict, targets: dict) -> float:
    """How far a meal is from its target, as a single number to minimise.

    Calories carry the most weight because they are the constraint a client feels;
    macros shape the plan around them.
    """
    weights = {"calories": 2.0, "protein": 1.0, "carb": 0.6, "fat": 0.6}
    score = 0.0
    for key, weight in weights.items():
        want = float(targets.get(key, 0) or 0)
        if want <= 0:
            continue
        score += weight * abs(actual.get(key, 0.0) - want) / want
    return score


def solve(foods: Sequence, targets: dict, max_combinations: int = 20000
          ) -> Tuple[List[Portion], float]:
    """Choose one portion of each food so the meal lands as close to target as possible.

    A bounded search, not a fill. With one food per slot and roughly eight servable
    amounts each, a four-slot meal is a few thousand combinations, which is nothing —
    and unlike a greedy fill it cannot produce a meal that violates its own constraints,
    so there is nothing downstream left to repair.

    Falls back to portioning each food independently against its share of the target if
    the space is somehow too large to enumerate.
    """
    foods = [f for f in foods if f is not None]
    if not foods:
        return [], 0.0

    ladders = [portions_for(food) for food in foods]
    space = 1
    for ladder in ladders:
        space *= max(len(ladder), 1)

    if space > max_combinations:
        share = 1.0 / len(foods)
        chosen = []
        for food in foods:
            kcal_per_gram = kcal_of(food) or 0.01
            want_grams = float(targets.get("calories", 0) or 0) * share / kcal_per_gram
            chosen.append(nearest_portion(food, want_grams))
        return chosen, deviation(totals(chosen), targets)

    best: Optional[List[Portion]] = None
    best_score = float("inf")

    def walk(index: int, picked: List[Portion]) -> None:
        nonlocal best, best_score
        if index == len(ladders):
            score = deviation(totals(picked), targets)
            if score < best_score:
                best_score, best = score, list(picked)
            return
        for portion in ladders[index]:
            picked.append(portion)
            walk(index + 1, picked)
            picked.pop()

    walk(0, [])
    return (best or []), best_score
