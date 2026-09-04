"""Candidate pool construction.

**Root A.** The old `_build_allowed_foods_map()` treated `UserFoodCategoryPreference` as
the ONLY source of candidates, so a user who had not completed food preferences got an
empty pool and a 233 kcal plan (-90% of target), stored silently as if it were normal.
`unique_together=(user, food)` made it worse: each food could occupy exactly one
(meal, macro) slot, leaving 5 of 20 cells empty even for a fully-configured user.

Preferences are a *ranking signal*, not a gate. The pool is always the catalogue; hard
constraints remove what is unsafe, and preference decides the order. An empty pool is
now only possible if the catalogue itself is empty.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from .policy import PlannerPolicy

logger = logging.getLogger(__name__)

MACROS = ("protein", "carb", "fat", "vegetable", "fruit")
MEALS = ("Breakfast", "Lunch", "Dinner", "Snack")

# Ranking weights. Higher wins.
W_MEAL_MACRO_PREF = 100.0   # user put this food in exactly this meal+macro slot
W_MACRO_CHOICE = 50.0       # user listed it as one of their proteins/carbs/fats
W_LIKED = 25.0              # user liked it
W_LEARNED = 20.0            # smart_score_weight from actual consumption
W_DENSITY = 10.0            # macro density per kcal — the original heuristic


@dataclass
class CandidatePool:
    """Ranked candidates per (meal, macro), plus why the ranking came out that way."""

    by_slot: Dict[str, Dict[str, List]] 
    source_counts: Dict[str, int]

    def get(self, meal: str, macro: str) -> List:
        return self.by_slot.get(meal, {}).get(macro, [])

    @property
    def empty_slots(self) -> List[tuple]:
        return [(m, mac) for m, macros in self.by_slot.items()
                for mac, foods in macros.items() if not foods]


# Category names that settle the classification without looking at macros.
_CATEGORY_HINTS = {
    "fruit": "fruit", "fruits": "fruit",
    "vegetable": "vegetable", "vegetables": "vegetable", "veg": "vegetable",
    "protein": "protein", "meat": "protein", "poultry": "protein", "fish": "protein",
    "seafood": "protein", "dairy": "protein", "eggs": "protein", "legume": "protein",
    "grain": "carb", "grains": "carb", "carb": "carb", "carbs": "carb",
    "starch": "carb", "bread": "carb", "cereal": "carb",
    "fat": "fat", "fats": "fat", "oil": "fat", "oils": "fat", "nuts": "fat",
}

# A protein source needs a meaningful amount AND has to be protein-led. A flat
# "protein >= 15 g" threshold filed Oats (17 g protein, 66 g carbohydrate) as a PROTEIN,
# so its carbohydrate was invisible to the optimiser and every plan ran ~40% over on
# carbs with no way to correct it.
PROTEIN_MIN_G = 10.0
# Below this energy density a plant food is produce, not a carb source.
PRODUCE_MAX_KCAL = 70.0

# Macros cannot separate fruit from vegetable — broccoli and apple have almost the same
# carbohydrate share of calories, and sugar is not modelled. Name is the only signal
# available until the ingredient model lands, so these mirror the lists already used in
# diet/services/diet_persistence.py.
_FRUIT_WORDS = {
    "apple", "banana", "orange", "strawberry", "strawberries", "blueberry", "blueberries",
    "raspberry", "blackberry", "mango", "pineapple", "grape", "grapes", "watermelon",
    "melon", "kiwi", "peach", "pear", "plum", "cherry", "cherries", "apricot", "fig",
    "date", "dates", "papaya", "guava", "pomegranate", "tangerine", "clementine",
    "grapefruit", "lemon", "lime", "cantaloupe", "honeydew", "nectarine", "lychee",
}
#: Anything drunk rather than eaten. Checked before the produce lists because those
#: match any token in the name, and a soft drink carrying a fruit in its branding was
#: being served as that fruit.
_BEVERAGE_WORDS = {
    "cola", "soda", "pepsi", "coke", "drink", "juice", "lemonade", "squash-drink",
    "beverage", "tea", "coffee", "latte", "cappuccino", "smoothie", "shake",
}

_VEGETABLE_WORDS = {
    "broccoli", "spinach", "kale", "lettuce", "romaine", "cabbage", "cauliflower",
    "carrot", "cucumber", "tomato", "tomatoes", "pepper", "zucchini", "courgette",
    "asparagus", "celery", "onion", "garlic", "mushroom", "eggplant", "aubergine",
    "beet", "radish", "turnip", "leek", "chard", "arugula", "sprout", "sprouts",
    "okra", "pumpkin", "squash", "bean", "beans", "pea", "peas", "corn",
}


def classify_food(food) -> str:
    """Which slot this food belongs in.

    Order matters, and getting it wrong was visible three times:
      1. category, when it says something useful
      2. produce by NAME — banana (89 kcal) sits above any sensible energy cut-off, so
         a density test filed it as a carbohydrate
      3. protein, but only when protein LEADS: a flat ">= 15 g" test filed oats
         (17 g protein, 66 g carbohydrate) as a protein, hiding its carbs from the
         optimiser and leaving every plan ~40% over on carbohydrate
      4. calorie share, as the fallback
    """
    name = (getattr(food, "name", "") or "").lower().replace(",", " ")
    words = set(name.split())

    category = (getattr(getattr(food, "category", None), "name", "") or "").strip().lower()
    for token, slot in _CATEGORY_HINTS.items():
        if token in category:
            return slot

    # A drink is not produce, whatever fruit is printed on the label. The name test
    # below matches any token, so "Diet Pepsi Drink Wild Cherry" was filed as fruit on
    # the strength of "cherry", and the low-calorie fallback further down filed plain
    # Cola as a vegetable because it is under 70 kcal with some carbohydrate and no fat.
    # Both then ranked at the top of a slot, because density per kcal rewards exactly
    # this: almost pure macro and nothing else.
    if words & _BEVERAGE_WORDS:
        # MACROS has no condiment bucket; role is what excludes these from selection.
        # Filing them as a vegetable keeps them out of protein, carb and fat, which is
        # where a near-pure macro does the damage.
        return "vegetable"

    if words & _VEGETABLE_WORDS:
        return "vegetable"
    if words & _FRUIT_WORDS:
        return "fruit"

    protein = float(getattr(food, "protein", 0) or 0)
    carbs = float(getattr(food, "carbs", 0) or 0)
    fat = float(getattr(food, "fat", 0) or 0)
    kcal = float(getattr(food, "calories", 0) or 0)

    if protein >= PROTEIN_MIN_G and protein > carbs and protein > fat:
        return "protein"

    if kcal <= PRODUCE_MAX_KCAL and carbs > 0 and fat < 3.0:
        # Unknown low-calorie plant food. Vegetable is the safe default: over-supplying
        # vegetables is harmless, over-supplying fruit adds sugar the model cannot see.
        return "vegetable"

    p, c, f = protein * 4, carbs * 4, fat * 9
    if max(p, c, f) <= 0:
        return "vegetable"
    return max((p, "protein"), (c, "carb"), (f, "fat"))[1]


def _dominant_macro(food) -> str:
    return classify_food(food)


def _density(food, macro: str) -> float:
    """Grams of the target macro per kcal — the original selection heuristic, kept."""
    kcal_pg = float(getattr(food, "calories_per_gram", 0) or 0)
    if kcal_pg <= 0:
        return 0.0
    attr = {"protein": "protein_per_gram", "carb": "carbs_per_gram", "fat": "fat_per_gram"}.get(macro)
    if not attr:
        return 0.0
    return float(getattr(food, attr, 0) or 0) / kcal_pg


def build_pool(user, policy: PlannerPolicy, catalogue: Optional[Sequence] = None,
               allergen_checker=None) -> CandidatePool:
    """Rank the whole catalogue into (meal, macro) slots for this user."""
    from diet.models import FoodItem, UserFoodCategoryPreference, UserFoodPreference

    foods = list(catalogue) if catalogue is not None else list(
        # A row whose nutrition failed its own sanity check must not be portioned from,
        # and a condiment is not something a meal is built on. Ranking by grams of macro
        # per kcal is maximised by foods that are almost pure macro and nothing else, so
        # BBQ sauce and mint jelly topped the carbohydrate lists ahead of rice. Excluding
        # by role fixes that at the source; tuning the density weight would only have
        # moved them down a place.
        FoodItem.objects.select_related("category")
        .filter(needs_review=False)
        .exclude(role=FoodItem.ROLE_CONDIMENT)
    )

    # ---- hard constraints: allergens and explicit dislikes only -------------
    pref = UserFoodPreference.objects.filter(user=user).prefetch_related(
        "liked_foods", "disliked_foods", "protein_choices", "carb_choices",
        "fat_choices", "vegetable_choices", "fruit_choices",
    ).first()

    disliked = {f.id for f in pref.disliked_foods.all()} if pref else set()
    liked = {f.id for f in pref.liked_foods.all()} if pref else set()

    if allergen_checker is not None and getattr(allergen_checker, "active", False):
        from diet.services.meal_validator import VIOLATION
        safe = [f for f in foods
                if allergen_checker.check_food(f).verdict != VIOLATION and f.id not in disliked]
    else:
        safe = [f for f in foods if f.id not in disliked]

    # ---- ranking signals ---------------------------------------------------
    slot_pref: Dict[tuple, set] = {}
    for rec in UserFoodCategoryPreference.objects.filter(user=user).select_related("food"):
        slot_pref.setdefault((rec.meal, rec.macro), set()).add(rec.food_id)

    macro_choice: Dict[str, set] = {}
    if pref:
        macro_choice = {
            "protein": {f.id for f in pref.protein_choices.all()},
            "carb": {f.id for f in pref.carb_choices.all()},
            "fat": {f.id for f in pref.fat_choices.all()},
            "vegetable": {f.id for f in pref.vegetable_choices.all()},
            "fruit": {f.id for f in pref.fruit_choices.all()},
        }

    def score(food, meal: str, macro: str) -> float:
        s = 0.0
        if food.id in slot_pref.get((meal, macro), ()):
            s += W_MEAL_MACRO_PREF
        if food.id in macro_choice.get(macro, ()):
            s += W_MACRO_CHOICE
        if food.id in liked:
            s += W_LIKED
        # Learned weight defaults to 1.0, so this is neutral until learning runs.
        s += W_LEARNED * (float(getattr(food, "smart_score_weight", 1.0) or 1.0) - 1.0)
        s += W_DENSITY * _density(food, macro)
        return s

    by_slot: Dict[str, Dict[str, List]] = {m: {mac: [] for mac in MACROS} for m in MEALS}
    for food in safe:
        dom = _dominant_macro(food)
        for meal in MEALS:
            by_slot[meal][dom].append(food)
    for meal in MEALS:
        for macro in MACROS:
            by_slot[meal][macro].sort(key=lambda f: score(f, meal, macro), reverse=True)

    counts = {
        "catalogue": len(foods),
        "after_hard_filters": len(safe),
        "slot_preferences": sum(len(v) for v in slot_pref.values()),
        "liked": len(liked),
        "disliked": len(disliked),
    }
    pool = CandidatePool(by_slot=by_slot, source_counts=counts)
    if pool.empty_slots:
        logger.warning(
            "Candidate pool has empty slots for user %s: %s (catalogue=%d, safe=%d)",
            getattr(user, "id", "?"), pool.empty_slots, len(foods), len(safe),
        )
    return pool
