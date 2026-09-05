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

import math

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

#: How finely each unit divides. A single global step was wrong in both directions: a
#: quarter of an egg is not a thing, and half a pot of yogurt is a 75 g quantum, which
#: in a 200 kcal snack is a fifth of the meal — coarse enough that no snack recipe could
#: land inside tolerance and every one of them fell back to assembly. People measure a
#: cup in quarters and an egg in whole eggs, so the step belongs to the unit.
UNIT_STEP = 0.5
UNIT_STEPS = {
    "cup": 0.25, "pot": 0.25, "serving": 0.25,
    "tbsp": 0.5, "slice": 0.5, "palm": 0.5, "fillet": 0.5, "block": 0.5,
    "handful": 0.5, "half": 0.5, "medium": 0.5,
    "egg": 1.0, "egg white": 1.0, "piece": 1.0, "clove": 1.0, "sprig": 1.0,
    "pinch": 1.0, "avocado": 0.25,
}

#: Units that read wrong in the plural. An abbreviation does not take an s, and "medium"
#: is an adjective standing in for the thing it describes.
INVARIANT_UNITS = {"tbsp", "tsp", "medium"}


def step_for(food) -> float:
    """The smallest change in this food a person would actually make."""
    unit = (getattr(food, "household_unit", "") or "").strip().lower()
    return UNIT_STEPS.get(unit, UNIT_STEP)

#: When a food carries no unit, cap the gram portion so the fallback cannot reproduce
#: the plates this module exists to prevent. A single flat ceiling was still too
#: permissive: 250 g is a reasonable plate of fish and an absurd amount of garlic, and
#: the catalogue's uncovered foods are a mix of both. The cap follows what the food IS.
FALLBACK_MAX_G = 250.0
FALLBACK_MIN_G = 20.0
FALLBACK_CAPS_G = {"fat": 60.0, "protein": 250.0, "carb": 200.0,
                   "vegetable": 250.0, "fruit": 200.0}


@dataclass(frozen=True)
class Portion:
    """One food at one amount, and how that amount was arrived at."""

    food: object
    grams: float
    units: Optional[float] = None

    @property
    def described(self) -> str:
        """What a person would say they are eating."""
        return describe(self.food, self.grams)


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

    # Rungs sit on the unit grid, inside [low, high]. Two faults lived in the old
    # `int(round((high - low) / UNIT_STEP))`. It treated the maximum as a step COUNT, so
    # a range that was not a whole number of steps either overshot its own ceiling —
    # 0.75-2.5 palms produced a 2.75-palm rung, 330 g of chicken against a declared
    # 300 g maximum — or could not reach it, as 0.75-4 cups of rice stopped at 3.75.
    # And it counted FROM the minimum, so a minimum off the grid put every rung off it:
    # chicken came out at 0.75, 1.25, 1.75 palms, none of which is an amount anyone
    # says. Counting from the grid gives 1, 1.5, 2, 2.5 — and the ceiling is a bound
    # that is always reachable.
    step = step_for(food)
    first = max(math.ceil(low / step - 1e-9) * step, step)
    levels: List[float] = []
    units = first
    while units <= high + 1e-9:
        levels.append(round(units, 2))
        units = round(units + step, 4)
    if not levels:
        levels.append(round(high, 2))
    return levels


def portions_for(food) -> List[Portion]:
    """Every portion of this food the planner is allowed to choose between."""
    levels = unit_levels(food)
    grams_per_unit = float(getattr(food, "unit_grams", 0) or 0)
    if levels:
        return [Portion(food, round(units * grams_per_unit, 1), units) for units in levels]

    # No unit declared. Offer a coarse ladder under a cap rather than a continuum, so
    # the fallback still cannot serve half a kilo of anything. The rungs are fractions
    # of that food's own cap, not a fixed gram list: a fixed list starting at 25 g put
    # the smallest servable amount of an un-unitised FAT at 220 kcal, which is a floor
    # behaving as an attractor — the exact failure this module exists to remove,
    # reintroduced through the back door of the fallback.
    cap = fallback_cap(food)
    rungs = []
    for share in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0):
        grams = cap * share
        grams = round(grams) if grams < 50 else round(grams / 5) * 5
        if grams > 0 and grams not in rungs:
            rungs.append(float(grams))
    return [Portion(food, g) for g in rungs]


def fallback_cap(food) -> float:
    """The most of an un-unitised food the planner may serve."""
    from .candidates import classify_food

    return FALLBACK_CAPS_G.get(classify_food(food), FALLBACK_MAX_G)


def ceiling_grams(food) -> float:
    """The largest amount of this food the planner is allowed to put on a plate.

    The one definition of "too much". The engine enforces it and the quality harness
    measures against it; when those were two separate tables they disagreed, and 53
    portions above a food's own declared ceiling passed a green gate.
    """
    options = portions_for(food)
    return max(p.grams for p in options) if options else FALLBACK_MAX_G


def describe(food, grams: float) -> str:
    """What a person would say they are eating, given a food and an amount.

    A module function rather than a property of `Portion`, because a portion object
    exists only inside the planner: by the time a meal reaches the client it is a
    `MealComponent` holding a float. Serving sizes were computed all the way through
    phase 4 and then dropped at the boundary, so the client still read "285 g yogurt".
    """
    unit = (getattr(food, "household_unit", "") or "").strip()
    per_unit = float(getattr(food, "unit_grams", 0) or 0)
    grams = float(grams or 0)
    if not unit or per_unit <= 0 or grams <= 0:
        return f"{grams:.0f} g"
    step = step_for(food)
    units = round(grams / per_unit / step) * step
    if units <= 0:
        return f"{grams:.0f} g"
    return f"{spell(units)} {unit}{plural_suffix(unit, units)}"


#: A quarter written as a quarter. "1.25 cups" is a spreadsheet; "1¼ cups" is a recipe.
FRACTIONS = {0.25: "\u00bc", 0.5: "\u00bd", 0.75: "\u00be"}


def spell(units: float) -> str:
    """A count of units the way it is written on a card."""
    whole = int(units)
    part = round(units - whole, 2)
    glyph = FRACTIONS.get(part)
    if glyph is None:
        return f"{units:g}"
    if whole == 0:
        return glyph
    return f"{whole}{glyph}"


def plural_suffix(unit: str, count) -> str:
    """English plural for a serving unit. "2 pinches", not "2 pinchs"."""
    if count <= 1 or unit.lower() in INVARIANT_UNITS:
        return ""
    return "es" if unit.endswith(("ch", "sh", "s", "x", "z")) else "s"


def toward(food, current_grams: float, wanted_grams: float) -> float:
    """The servable amount nearest `wanted_grams`, never on the wrong side of `current`.

    This is what makes the ladder an invariant rather than a suggestion. Every stage
    that adjusts a portion goes through here, so an adjustment can move a food between
    the amounts a person serves and can never invent one in between or beyond.

    When the ladder has nothing left in the requested direction the food is already at
    its ceiling or its floor, and the answer is the nearest rung to where it is — which
    also repairs a portion that arrived off the ladder.
    """
    options = [p.grams for p in portions_for(food)]
    if not options:
        capped = max(FALLBACK_MIN_G, min(fallback_cap(food), float(wanted_grams)))
        return round(capped, 1)

    current = float(current_grams or 0)
    wanted = float(wanted_grams or 0)
    if wanted > current:
        side = [g for g in options if g > current + 1e-6]
    elif wanted < current:
        side = [g for g in options if g < current - 1e-6]
    else:
        side = []
    if not side:
        return min(options, key=lambda g: abs(g - current))
    return min(side, key=lambda g: abs(g - wanted))


def nearest_portion(food, target_grams: float) -> Portion:
    """The servable amount closest to `target_grams`.

    Closest in either direction, deliberately. Rounding up was one of three mechanisms
    that made every plan land over its calorie target and never under.
    """
    options = portions_for(food)
    return min(options, key=lambda p: abs(p.grams - float(target_grams)))


def macro_of(food, macro: str) -> float:
    """Grams of one macro per gram of this food.

    Derived from the per-hundred-gram columns, which are what the seeds populate and
    what every other stage adds up. `protein_per_gram` and friends are separate stored
    columns reconciled with them only when one side is zero, so reading them here made
    this module capable of disagreeing with the rest of the engine about the same food.
    """
    attribute = {"protein": "protein", "carb": "carbs", "fat": "fat"}.get(macro)
    if not attribute:
        return 0.0
    return float(getattr(food, attribute, 0) or 0) / 100.0


def kcal_of(food) -> float:
    return float(getattr(food, "calories", 0) or 0) / 100.0


def totals(portions: Sequence[Portion]) -> dict:
    """Calories and macros for a set of portions."""
    from .report import totals_of

    return totals_of([(p.food, p.grams) for p in portions])


def solve(foods: Sequence, targets: dict, max_combinations: int = 20000
          ) -> Tuple[List[Portion], float]:
    """Choose one portion of each food so the meal lands as close to target as possible.

    A bounded search, not a fill. With one food per slot and roughly eight servable
    amounts each, a four-slot meal is a few thousand combinations, which is nothing —
    and unlike a greedy fill it cannot produce a meal that violates its own constraints,
    so there is nothing downstream left to repair.

    Falls back to portioning each food independently against its share of the target if
    the space is somehow too large to enumerate.

    Scored by the same objective as everything else. This module used to carry its own —
    calories weighted 2.0, protein 1.0, carbohydrate and fat 0.6 — against the engine's
    protein, carbohydrate and fat at 1.0 with calories at 0.5. So the template path
    optimised one thing while the optimiser that ran after it judged another, and six
    meals in every three hundred came out provably off the optimum for that reason
    alone. This engine has made the two-measures mistake once before, with a duplicated
    macro-ratio table; two answers to "which meal is better" will always drift apart.
    """
    from .report import deviation_of

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
        return chosen, deviation_of(totals(chosen), targets).magnitude

    best: Optional[List[Portion]] = None
    best_score = float("inf")

    def walk(index: int, picked: List[Portion]) -> None:
        nonlocal best, best_score
        if index == len(ladders):
            score = deviation_of(totals(picked), targets).magnitude
            if score < best_score:
                best_score, best = score, list(picked)
            return
        for portion in ladders[index]:
            picked.append(portion)
            walk(index + 1, picked)
            picked.pop()

    walk(0, [])
    return (best or []), best_score
