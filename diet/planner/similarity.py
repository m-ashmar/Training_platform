"""Food similarity from features, not training.

The honest form of "vectors" for a catalogue with no consumption data: a feature vector
per food built from what the row already says — macro profile per 100 g, category,
role, meal slots, cuisine, cooked or dry — and cosine similarity over it. No model, no
corpus, no cold start, and every neighbour is explainable by pointing at the features.
Chicken lands beside turkey and tilapia; rice beside bulgur and couscous; labneh beside
Greek yogurt.

Four things read it. A recipe line with no explicit `swap_group` accepts any food above
the threshold in the same slot. When a client's chosen food is in no fitting dish, it is
substituted into the nearest line of the nearest dish, so preference reaches a named
dish without a recipe author. "Swap this food" offers the top neighbours. And in the
pool, a food similar to a chosen one is nudged up, so a client who chose chicken sees
turkey rise without anyone telling the engine that.

Language-model embeddings are deliberately not used here: they encode real culinary
association but cannot be audited and drift with the model version.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

CATEGORIES = ("Proteins", "Carbs", "Fats", "Vegetables", "Fruits", "Other")
ROLES = ("staple", "accompaniment", "condiment")
SLOTS = ("Breakfast", "Lunch", "Dinner", "Snack")
CUISINES = ("universal", "western", "levantine")

#: How much each block of the vector counts. Macros describe what a food does on the
#: plate; the rest describe where it belongs. Together they say "same kind of thing".
W_MACRO, W_CATEGORY, W_ROLE, W_SLOT, W_CUISINE, W_STATE = 1.0, 1.2, 0.8, 0.6, 0.3, 0.5


def _onehot(value, choices, weight) -> List[float]:
    return [weight if value == c else 0.0 for c in choices]


def features(food) -> List[float]:
    """The vector for one food. Pure; depends only on the row."""
    kcal = max(float(getattr(food, "calories", 0) or 0), 1.0)
    p = float(getattr(food, "protein", 0) or 0) * 4 / kcal
    c = float(getattr(food, "carbs", 0) or 0) * 4 / kcal
    f = float(getattr(food, "fat", 0) or 0) * 9 / kcal
    density = min(kcal / 900.0, 1.0)  # 0 for water, 1 for pure fat
    vec = [W_MACRO * p, W_MACRO * c, W_MACRO * f, W_MACRO * density]

    category = (getattr(getattr(food, "category", None), "name", "") or "Other")
    vec += _onehot(category, CATEGORIES, W_CATEGORY)
    vec += _onehot(getattr(food, "role", "staple") or "staple", ROLES, W_ROLE)
    slots = set(getattr(food, "meal_slots", None) or ())
    vec += [W_SLOT if (not slots or s in slots) else 0.0 for s in SLOTS]
    vec += _onehot(getattr(food, "cuisine", "universal") or "universal", CUISINES, W_CUISINE)
    name = (getattr(food, "name", "") or "").lower()
    vec += [W_STATE if "cooked" in name else 0.0, W_STATE if "dry" in name or "raw" in name else 0.0]
    return vec


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class SimilarityIndex:
    """Cosine neighbours over a catalogue, built once per generation."""

    def __init__(self, foods: Sequence):
        self.foods = {f.id: f for f in foods}
        self.vectors = {f.id: features(f) for f in foods}

    def similarity(self, a_id: int, b_id: int) -> float:
        if a_id == b_id:
            return 1.0
        va, vb = self.vectors.get(a_id), self.vectors.get(b_id)
        if va is None or vb is None:
            return 0.0
        return cosine(va, vb)

    def neighbours(self, food_id: int, k: int = 5, threshold: float = 0.0,
                   same_slot: Optional[str] = None) -> List[Tuple[object, float]]:
        """The k most similar foods, optionally restricted to one classified slot."""
        from .candidates import classify_food

        anchor = self.foods.get(food_id)
        if anchor is None:
            return []
        want_slot = same_slot or classify_food(anchor)
        scored = []
        for other_id, other in self.foods.items():
            if other_id == food_id:
                continue
            if classify_food(other) != want_slot:
                continue
            sim = self.similarity(food_id, other_id)
            if sim >= threshold:
                scored.append((other, sim))
        scored.sort(key=lambda pair: -pair[1])
        return scored[:k]

    def nearest_of(self, food_id: int, candidate_ids: Sequence[int]) -> Tuple[Optional[int], float]:
        """Among `candidate_ids`, the one most like `food_id`, with its similarity."""
        best, best_sim = None, -1.0
        for cid in candidate_ids:
            sim = self.similarity(food_id, cid)
            if sim > best_sim:
                best, best_sim = cid, sim
        return best, best_sim
