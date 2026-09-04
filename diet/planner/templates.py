"""Meal shapes, so a meal can be built rather than looked up.

The engine had two ways to produce a meal and nothing in between. A recipe is a fixed
combination someone wrote down: coherent, and unable to express a client it was not
written for. Component assembly is free: it hits the macro target and produces a pile.

A template is the middle. It is the *shape* of a meal — protein, starch, vegetable, fat,
one of each — filled from the client's own chosen foods. Ten shapes against a client's
pool is a month of meals that are theirs, where sixty recipes is sixty fixed
combinations that are nobody's in particular.

Nothing here is hand-authored. The shapes are read off the recipe library, which turns
out to be strikingly consistent: six of sixteen recipes are protein + carb + vegetable +
fat, two more are protein + carb + fruit. The pairings are read off the same library —
every pair of ingredients inside a recipe is an edge someone verified when they wrote
it, so salmon pulls rice ahead of oats without anyone maintaining a table.

Consumption would be the better source for both eventually. There is almost none: 5,129
meal components exist and 17 carry an amount actually eaten. Learn from recipes now,
from plates later.
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

#: Slots a meal is built from, in the order they are filled. Protein first because it is
#: the hardest to hit and the most expensive to get wrong; fat last because it is the
#: easiest to adjust without changing what the meal is.
FILL_ORDER = ("protein", "carb", "vegetable", "fruit", "fat")

#: A meal needs a shape, not just a macro total. Without these a pairwise affinity graph
#: would happily serve salmon with rice and oats and potato, because each of those edges
#: is individually fine.
MAX_PER_SLOT = {"protein": 1, "carb": 1, "vegetable": 1, "fruit": 1, "fat": 1}

#: Lunch and dinner get something green whether or not the shape asked for it.
VEGETABLE_MEALS = ("Lunch", "Dinner")

W_RANK = 1.0        # position in the client's own ranked pool
W_PAIRING = 1.5     # how often this food appears beside what is already on the plate
RECENCY_PENALTY = 0.1


@dataclass(frozen=True)
class MealTemplate:
    """A shape a meal can take, and which meals it suits."""

    slots: Tuple[str, ...]
    meals: Tuple[str, ...]
    seen: int
    example: str

    @property
    def name(self) -> str:
        return " + ".join(self.slots)

    def suits(self, meal_name: str) -> bool:
        return not self.meals or meal_name in self.meals


def derive_templates(recipes: Optional[Sequence] = None) -> List[MealTemplate]:
    """Read the shapes the recipe library already uses.

    A shape is the multiset of slots its ingredients classify into. Every shape is kept,
    including the ones seen once: with a small library, discarding singletons would
    throw away most of the vocabulary. `seen` carries the frequency so the caller can
    prefer a shape the kitchen actually repeats.
    """
    from diet.models import Recipe

    from .candidates import classify_food

    if recipes is None:
        recipes = list(
            Recipe.objects.filter(is_active=True).prefetch_related("ingredients__food"))

    shapes: Dict[Tuple[str, ...], Dict] = {}
    for recipe in recipes:
        slots = tuple(sorted(classify_food(line.food) for line in recipe.ingredients.all()))
        if not slots:
            continue
        entry = shapes.setdefault(
            slots, {"count": 0, "meals": set(), "example": recipe.name})
        entry["count"] += 1
        entry["meals"].update(getattr(recipe, "meal_types", None) or [])

    templates = [
        MealTemplate(slots=slots, meals=tuple(sorted(entry["meals"])),
                     seen=entry["count"], example=entry["example"])
        for slots, entry in shapes.items()
    ]
    templates.sort(key=lambda t: (-t.seen, t.slots))
    return templates


def pairing_edges(recipes: Optional[Sequence] = None) -> Dict[int, collections.Counter]:
    """How often each pair of foods appears in the same dish.

    Undirected, stored both ways so a lookup is one dictionary hit. This is the whole of
    the affinity model: it costs nothing to maintain, it grows every time someone writes
    a recipe, and it is verified by construction because a person put those foods in a
    bowl together.
    """
    from diet.models import Recipe

    if recipes is None:
        recipes = list(
            Recipe.objects.filter(is_active=True).prefetch_related("ingredients__food"))

    edges: Dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for recipe in recipes:
        ids = [line.food_id for line in recipe.ingredients.all()]
        for i, left in enumerate(ids):
            for right in ids[i + 1:]:
                if left == right:
                    continue
                edges[left][right] += 1
                edges[right][left] += 1
    return edges


def _affinity(candidate_id: int, chosen_ids: Sequence[int],
              edges: Dict[int, collections.Counter]) -> float:
    """How well this food sits beside what is already on the plate, 0.0 upward."""
    if not chosen_ids:
        return 0.0
    neighbours = edges.get(candidate_id)
    if not neighbours:
        return 0.0
    return sum(neighbours.get(other, 0) for other in chosen_ids) / len(chosen_ids)


def _pick(candidates: Sequence, chosen_ids: List[int],
          edges: Dict[int, collections.Counter], recent: Set[int], rng) -> Optional[object]:
    """One filler for one slot.

    The pool arrives already ranked for this client — `build_pool` puts what they chose
    for this meal and macro at the top, at the highest weight it has. Position in that
    ranking is therefore the preference signal; affinity decides between foods the
    client is equally happy with. Sampled rather than maximised, because taking the top
    of a ranked list every time is exactly what served one dish 48 times.
    """
    shortlist = list(candidates)[:12]
    if not shortlist:
        return None

    weights = []
    for index, food in enumerate(shortlist):
        rank_score = W_RANK * (1.0 / (1.0 + index))
        pair_score = W_PAIRING * _affinity(food.id, chosen_ids, edges)
        weight = rank_score + pair_score
        if food.id in recent:
            weight *= RECENCY_PENALTY
        weights.append(max(weight, 1e-6))

    if rng is None:
        return max(zip(shortlist, weights), key=lambda pair: pair[1])[0]
    return rng.choices(shortlist, weights=weights, k=1)[0]


def build_meal(meal_name: str, template: MealTemplate, pool, targets: Dict[str, float],
               edges: Dict[int, collections.Counter], recent: Optional[Set[int]] = None,
               rng=None):
    """Fill a template from this client's pool and portion it.

    Returns `(portions, deviation)`. An empty list means the pool could not supply the
    shape, and the caller should try another template.
    """
    from .portion import solve

    recent = recent or set()
    wanted = list(template.slots)
    if meal_name in VEGETABLE_MEALS and "vegetable" not in wanted:
        wanted.append("vegetable")

    chosen: List[object] = []
    chosen_ids: List[int] = []
    used_per_slot: Dict[str, int] = collections.defaultdict(int)

    for slot in FILL_ORDER:
        for _ in range(wanted.count(slot)):
            if used_per_slot[slot] >= MAX_PER_SLOT.get(slot, 1):
                continue
            available = [f for f in pool.get(meal_name, slot) if f.id not in chosen_ids]
            food = _pick(available, chosen_ids, edges, recent, rng)
            if food is None:
                continue
            chosen.append(food)
            chosen_ids.append(food.id)
            used_per_slot[slot] += 1

    if not chosen:
        return [], float("inf")

    portions, score = solve(chosen, targets)
    return portions, score


def plan_meal(meal_name: str, pool, targets: Dict[str, float],
              templates: Optional[Sequence[MealTemplate]] = None,
              edges: Optional[Dict[int, collections.Counter]] = None,
              recent: Optional[Set[int]] = None, rng=None):
    """Best meal this client's pool can make for this target, across every shape.

    Returns `(portions, deviation, template)`, or `([], inf, None)` when nothing fits.
    """
    templates = templates if templates is not None else derive_templates()
    edges = edges if edges is not None else pairing_edges()

    usable = [t for t in templates if t.suits(meal_name)] or list(templates)
    best_portions: List = []
    best_score = float("inf")
    best_template: Optional[MealTemplate] = None

    for template in usable:
        portions, score = build_meal(
            meal_name, template, pool, targets, edges, recent=recent, rng=rng)
        if portions and score < best_score:
            best_portions, best_score, best_template = portions, score, template

    return best_portions, best_score, best_template
