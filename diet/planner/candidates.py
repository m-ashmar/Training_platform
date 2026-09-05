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
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from .policy import PlannerPolicy

logger = logging.getLogger(__name__)

MACROS = ("protein", "carb", "fat", "vegetable", "fruit")
MEALS = ("Breakfast", "Lunch", "Dinner", "Snack")

# Ranking weights. Higher wins.
W_MEAL_MACRO_PREF = 100.0   # user put this food in exactly this meal+macro slot
#: The user named this food for this meal but filed it under a different macro than the
#: engine classifies it as. That disagreement is not the user being wrong: someone who
#: says chickpeas are their lunch protein is describing how they eat, while the
#: classifier reads the macro that leads by calories and files them as a carbohydrate.
#: Requiring both to agree meant the preference was dropped in silence and the client
#: was served exactly what someone who had chosen nothing would get.
W_MEAL_PREF = 80.0
W_MACRO_CHOICE = 50.0       # user listed it as one of their proteins/carbs/fats
W_LIKED = 25.0              # user liked it
W_LEARNED = 20.0            # smart_score_weight from actual consumption
W_DENSITY = 10.0            # macro density per kcal — the original heuristic
#: Something a meal is built on outranks something served alongside it. Excluding
#: condiments stopped BBQ sauce leading the carbohydrates; it did nothing about oil
#: leading the fats, because oil is a real fat and density per kcal is maximised by
#: whatever is closest to pure. So a snack came out as an orange and a spoon of coconut
#: oil while nuts, avocado and nut butter sat below it. Role decides who anchors a slot.
W_ROLE_STAPLE = 40.0
#: Whether the recipe library serves this food at this meal. The default ordering knew
#: nothing about the time of day, so a client who had chosen nothing was offered a
#: breakfast of grilled chicken, white rice and olive oil — each of them the top-ranked
#: staple in its slot, and together a lunch. Ranked below the client's own choice, so
#: someone who says they want chicken for breakfast still gets it.
W_MEAL_LIBRARY = 35.0
#: How well the food matches the cuisine ratio the client chose, 0 to 1, scaled. Below a
#: chosen food and above density, so a client who asks for mostly local food gets it and
#: a client who names a Western food still gets that food.
W_CUISINE = 30.0


@dataclass
class CandidatePool:
    """Ranked candidates per (meal, macro), plus why the ranking came out that way."""

    by_slot: Dict[str, Dict[str, List]]
    source_counts: Dict[str, int]
    #: meal -> macro -> food id -> the score that put it where it is. Rank position
    #: alone says which food is preferred and not by how much, and the difference is
    #: the whole of the personalisation signal: a food the client explicitly asked for
    #: sits a hundred points above the next, while two foods separated by macro density
    #: differ by one or two. A consumer that reads only the order treats those the same.
    scores: Dict[str, Dict[str, Dict[int, float]]] = field(default_factory=dict)

    def get(self, meal: str, macro: str) -> List:
        return self.by_slot.get(meal, {}).get(macro, [])

    def weights(self, meal: str, macro: str) -> Dict[int, float]:
        return self.scores.get(meal, {}).get(macro, {})

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
    "okra", "pumpkin", "squash",
}
# Legumes and corn are deliberately absent. Naming them here filed Black Beans (132 kcal,
# 24 g carbohydrate) and Corn (96 kcal) as vegetables, which hid their carbohydrate from
# the optimiser exactly as a flat protein threshold once hid the carbohydrate in oats.
# The energy test below separates them correctly without a list: green beans are 31 kcal
# and land as produce, black beans are not.


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
               allergen_checker=None, constraints=None) -> CandidatePool:
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

    # ---- hard constraints -------------------------------------------------
    # Asked once, of the one object that knows the answer. This used to be decided here
    # inline, which is why the recipe path could not consult the same rule.
    from .constraints import ClientConstraints

    pref = UserFoodPreference.objects.filter(user=user).prefetch_related(
        "liked_foods", "disliked_foods", "protein_choices", "carb_choices",
        "fat_choices", "vegetable_choices", "fruit_choices",
    ).first()

    liked = {f.id for f in pref.liked_foods.all()} if pref else set()

    if constraints is None:
        constraints = ClientConstraints(
            disliked_ids=frozenset(f.id for f in pref.disliked_foods.all()) if pref
            else frozenset(),
            allergen_checker=allergen_checker,
        )
    safe = [f for f in foods if not constraints.forbids(f)]

    # ---- ranking signals ---------------------------------------------------
    slot_pref: Dict[tuple, set] = {}
    meal_pref: Dict[str, set] = {}
    for rec in UserFoodCategoryPreference.objects.filter(user=user).only(
            "meal", "macro", "food_id"):
        slot_pref.setdefault((rec.meal, rec.macro), set()).add(rec.food_id)
        meal_pref.setdefault(rec.meal, set()).add(rec.food_id)

    macro_choice: Dict[str, set] = {}
    if pref:
        macro_choice = {
            "protein": {f.id for f in pref.protein_choices.all()},
            "carb": {f.id for f in pref.carb_choices.all()},
            "fat": {f.id for f in pref.fat_choices.all()},
            "vegetable": {f.id for f in pref.vegetable_choices.all()},
            "fruit": {f.id for f in pref.fruit_choices.all()},
        }

    from .templates import meal_foods
    from diet.models import UserFoodWeight

    served_at = meal_foods()
    learned = (dict(UserFoodWeight.objects.filter(user=user).values_list("food_id", "weight"))
               if getattr(user, "pk", None) else {})

    def score(food, meal: str, macro: str) -> float:
        s = W_ROLE_STAPLE if getattr(food, "role", "") == FoodItem.ROLE_STAPLE else 0.0
        s += W_CUISINE * constraints.cuisine.weight(getattr(food, "cuisine", None))
        if food.id in served_at.get(meal, ()):
            s += W_MEAL_LIBRARY
        if food.id in slot_pref.get((meal, macro), ()):
            s += W_MEAL_MACRO_PREF
        elif food.id in meal_pref.get(meal, ()):
            s += W_MEAL_PREF
        if food.id in macro_choice.get(macro, ()):
            s += W_MACRO_CHOICE
        if food.id in liked:
            s += W_LIKED
        # This client's learned weight, neutral at 1.0 until they have eaten something.
        s += W_LEARNED * (float(learned.get(food.id, 1.0)) - 1.0)
        s += W_DENSITY * _density(food, macro)
        return s

    by_slot: Dict[str, Dict[str, List]] = {m: {mac: [] for mac in MACROS} for m in MEALS}
    for food in safe:
        dom = _dominant_macro(food)
        for meal in MEALS:
            by_slot[meal][dom].append(food)
    scores: Dict[str, Dict[str, Dict[int, float]]] = {m: {} for m in MEALS}
    for meal in MEALS:
        for macro in MACROS:
            table = {f.id: score(f, meal, macro) for f in by_slot[meal][macro]}
            by_slot[meal][macro].sort(key=lambda f: table[f.id], reverse=True)
            scores[meal][macro] = table

    counts = {
        "catalogue": len(foods),
        "after_hard_filters": len(safe),
        "slot_preferences": sum(len(v) for v in slot_pref.values()),
        "liked": len(liked),
        "disliked": len(constraints.disliked_ids),
    }
    pool = CandidatePool(by_slot=by_slot, source_counts=counts, scores=scores)
    if pool.empty_slots:
        logger.warning(
            "Candidate pool has empty slots for user %s: %s (catalogue=%d, safe=%d)",
            getattr(user, "id", "?"), pool.empty_slots, len(foods), len(safe),
        )
    return pool
