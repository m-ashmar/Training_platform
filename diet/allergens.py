"""
Canonical allergen vocabulary and inference.

Why this module exists
----------------------
Allergen safety used to be a substring test against the food's display name. That is
the wrong technique twice over: it cannot see what a dish is MADE OF (`Pad Thai`
contains peanuts and says so nowhere in its name), and it produces both misses and
false alarms on the name itself.

The data model is already ingredient-level — a Meal is composed of MealComponents,
each pointing at a FoodItem. What was missing is allergen data ON the FoodItem. This
module supplies the vocabulary, plus a best-effort inference used to seed existing
rows.

Trust levels
------------
`FoodItem.allergen_source` records where a row's tags came from:

  ``verified``  curated or supplied by a trusted source — authoritative
  ``inferred``  derived from the name by `infer_allergens()` — a hint, NOT proof
  ``unknown``   never populated

**Unknown is not the same as safe.** A checker must report an unverified food as
needing review rather than passing it silently; see `diet/services/meal_validator.py`.
"""

from __future__ import annotations

import re
from typing import Iterable, Set

# The 14 EU / 9 US major allergens, merged. These tags are the ONLY values that may be
# stored in FoodItem.allergens — free text is what caused the original bug.
ALLERGENS: dict[str, str] = {
    "peanut":     "Peanuts",
    "tree_nut":   "Tree nuts",
    "milk":       "Milk / dairy",
    "egg":        "Eggs",
    "fish":       "Fish",
    "shellfish":  "Crustacean shellfish",
    "mollusc":    "Molluscs",
    "soy":        "Soybeans",
    "gluten":     "Cereals containing gluten",
    "sesame":     "Sesame",
    "mustard":    "Mustard",
    "celery":     "Celery",
    "lupin":      "Lupin",
    "sulphite":   "Sulphur dioxide / sulphites",
}

# Words that imply an allergen when they appear in a food or ingredient name.
_MARKERS: dict[str, set[str]] = {
    "peanut":    {"peanut", "groundnut", "arachis", "satay"},
    "tree_nut":  {"almond", "walnut", "cashew", "pecan", "pistachio", "hazelnut",
                  "macadamia", "brazil nut", "praline", "marzipan", "nutella", "pine nut"},
    "milk":      {"milk", "dairy", "cheese", "butter", "yoghurt", "yogurt", "cream",
                  "ghee", "casein", "whey", "lactose", "custard", "labneh", "paneer",
                  "mozzarella", "cheddar", "feta", "ricotta", "parmesan", "caramel",
                  "chocolate", "choclate", "tikka", "latte", "cappuccino", "ice cream",
                  "gelato", "pudding", "alfredo", "gratin", "au gratin", "kefir"},
    "egg":       {"egg", "omelette", "omelet", "mayonnaise", "mayo", "meringue",
                  "albumin", "frittata", "quiche"},
    "fish":      {"fish", "salmon", "tuna", "cod", "anchovy", "sardine", "haddock",
                  "mackerel", "trout", "herring", "tilapia", "bass", "worcestershire",
                  "halibut", "sole", "snapper", "pollock", "catfish", "sushi", "surimi",
                  "caviar", "roe", "ceviche", "kipper"},
    "shellfish": {"shrimp", "prawn", "crab", "lobster", "crayfish", "langoustine",
                  "scampi", "shellfish"},
    "mollusc":   {"mussel", "clam", "oyster", "scallop", "squid", "calamari",
                  "octopus", "snail", "escargot"},
    "soy":       {"soy", "soya", "soybean", "tofu", "edamame", "miso", "tempeh",
                  "inari", "teriyaki", "hoisin", "ponzu"},
    "gluten":    {"wheat", "gluten", "flour", "bread", "pasta", "couscous", "bulgur",
                  "semolina", "seitan", "farina", "barley", "rye", "spelt", "noodle",
                  "cracker", "pastry", "croissant", "pita", "bun", "cake", "biscuit",
                  "bagel", "burger", "muffin", "granola", "cereal", "corn flakes",
                  "farro", "breaded", "crispy", "tender", "nugget", "batter", "toast",
                  "sandwich", "wrap", "pizza", "dough", "cookie", "brownie", "waffle",
                  "pancake", "tortilla", "spaghetti", "macaroni", "lasagna", "aglio",
                  "orzo", "ramen", "udon", "pretzel", "crouton", "malt", "beer"},
    "sesame":    {"sesame", "tahini", "halva", "hummus", "zaatar", "za'atar"},
    "mustard":   {"mustard"},
    "celery":    {"celery", "celeriac"},
    "lupin":     {"lupin"},
    "sulphite":  {"sulphite", "sulfite"},
}

# Free-text a user might type, mapped onto canonical tags.
_USER_SYNONYMS: dict[str, str] = {
    "peanuts": "peanut", "ground nut": "peanut", "groundnuts": "peanut",
    "nuts": "tree_nut", "nut": "tree_nut", "tree nuts": "tree_nut",
    "almonds": "tree_nut", "walnuts": "tree_nut", "cashews": "tree_nut",
    "dairy": "milk", "lactose": "milk", "cheese": "milk", "milk products": "milk",
    "eggs": "egg",
    "seafood": "shellfish", "shrimps": "shellfish", "prawns": "shellfish",
    "crustaceans": "shellfish", "shell fish": "shellfish",
    "molluscs": "mollusc", "mollusks": "mollusc",
    "soya": "soy", "soybeans": "soy", "soy beans": "soy",
    "wheat": "gluten", "celiac": "gluten", "coeliac": "gluten", "gluten free": "gluten",
    "sesame seeds": "sesame", "tahini": "sesame",
    "sulphites": "sulphite", "sulfites": "sulphite",
}

_WORD_RE = re.compile(r"[a-z']+")


def _norm(text: str) -> tuple[set[str], str]:
    """Lowercase word set plus the flattened string, for phrase markers."""
    low = (text or "").lower()
    return set(_WORD_RE.findall(low)), low


def infer_allergens(*texts: str) -> Set[str]:
    """Best-effort allergen tags for a food/ingredient name.

    A HINT, never proof — the result must be stored with
    ``allergen_source='inferred'`` so downstream checks know it is unverified.
    """
    found: Set[str] = set()
    words, low = _norm(" ".join(t for t in texts if t))
    singular = {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
                for w in words}
    for tag, markers in _MARKERS.items():
        for marker in markers:
            if " " in marker:
                if marker in low:
                    found.add(tag); break
            elif marker in words or marker in singular:
                found.add(tag); break
    return found


def parse_user_allergies(raw: str | None) -> Set[str]:
    """Turn a free-text allergy list into canonical tags.

    Anything unrecognised is preserved as a ``free:<term>`` pseudo-tag so it is still
    matched against ingredient text rather than being silently dropped — the original
    implementation dropped every term after the first comma.
    """
    tags: Set[str] = set()
    for chunk in re.split(r"[,;/]| and ", (raw or "").lower()):
        term = " ".join(_WORD_RE.findall(chunk)).strip()
        if not term:
            continue
        if term in ALLERGENS:
            tags.add(term); continue
        if term in _USER_SYNONYMS:
            tags.add(_USER_SYNONYMS[term]); continue
        inferred = infer_allergens(term)
        if inferred:
            tags.update(inferred)
        else:
            tags.add(f"free:{term}")
    return tags


def label(tag: str) -> str:
    """Human-readable name for a tag."""
    if tag.startswith("free:"):
        return tag[5:]
    return ALLERGENS.get(tag, tag)


def validate_tags(tags: Iterable[str]) -> list[str]:
    """Reject anything outside the canonical vocabulary (used by model validation)."""
    return [t for t in tags if t not in ALLERGENS]
