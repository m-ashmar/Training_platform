"""The dietary tags a food card shows, read only from what the catalogue holds.

A tag is a machine code; the app draws the words (BACKEND_BUGS B-065). Every tag here is
decided by a column the food already carries, against a threshold named below with the
standard it comes from. None is guessed from a name.

Fibre is stored since the owner's decision of 2026-09-23 (`FoodItem.fiber`, USDA nutrient
1079); a food whose fibre is unknown (NULL) earns no fibre tag, never a guess.

What the catalogue does NOT hold, and so is never tagged: vegetarian / vegan (the owner
decided against a vegetarian flag, 2026-09-23, and a category such as "Proteins" does not
say whether a food is meat), sugar and salt.
"""
from __future__ import annotations

from typing import List

#: The food's cuisine, when it has one. `FoodItem.cuisine` is curated per food;
#: `universal` means it belongs to every table and is not a tag.
CUISINE_TAGS = {"levantine": "levantine", "western": "western"}

#: "High protein": at least 20% of the food's energy comes from protein. The threshold
#: of the EU nutrition-claims regulation (EC 1924/2006, Annex), which is the nearest
#: thing to an agreed meaning; protein is counted at 4 kcal a gram, as the same
#: regulation does.
HIGH_PROTEIN_ENERGY_SHARE = 0.20
PROTEIN_KCAL_PER_GRAM = 4.0

#: "Low fat": no more than 3 g of fat per 100 g (EC 1924/2006, the threshold for
#: solids). The catalogue is quoted per 100 g and does not say which foods are liquids,
#: so the solid threshold is the one that can be applied to every row.
LOW_FAT_MAX_G_PER_100G = 3.0

#: "Low calorie": no more than 40 kcal per 100 g (EC 1924/2006 "low energy", solids).
LOW_CALORIE_MAX_KCAL_PER_100G = 40.0

#: "High fibre": at least 6 g of fibre per 100 g (EC 1924/2006, Annex). The regulation
#: also accepts 3 g per 100 kcal; that arm is not applied, because on a food this light it
#: says nothing a person would call fibrous — it would tag a cucumber (0.5 g per 100 g,
#: 15 kcal). The card claims fibre by weight, as the food is eaten.
HIGH_FIBER_MIN_G_PER_100G = 6.0


def food_tags(food) -> List[str]:
    """The tags this food earns, in a stable order. Reads columns only: no query."""
    tags: List[str] = []
    cuisine = CUISINE_TAGS.get((getattr(food, "cuisine", "") or "").strip().lower())
    if cuisine:
        tags.append(cuisine)
    kcal = float(getattr(food, "calories", 0) or 0)
    protein = float(getattr(food, "protein", 0) or 0)
    fat = float(getattr(food, "fat", 0) or 0)
    if kcal > 0 and protein * PROTEIN_KCAL_PER_GRAM >= HIGH_PROTEIN_ENERGY_SHARE * kcal:
        tags.append("high_protein")
    if fat <= LOW_FAT_MAX_G_PER_100G:
        tags.append("low_fat")
    if kcal <= LOW_CALORIE_MAX_KCAL_PER_100G:
        tags.append("low_calorie")
    # Unknown fibre (None) is not 0 g and not a guess: no tag either way.
    fiber = getattr(food, "fiber", None)
    if fiber is not None and float(fiber) >= HIGH_FIBER_MIN_G_PER_100G:
        tags.append("high_fiber")
    return tags
