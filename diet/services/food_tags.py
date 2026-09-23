"""The dietary tags a food card shows, read only from what the catalogue holds.

A tag is a machine code; the app draws the words (BACKEND_BUGS B-065). Every tag here is
decided by a column the food already carries, against a threshold named below with the
standard it comes from. None is guessed from a name.

What the catalogue does NOT hold, and so is never tagged: fibre (no column — `fetch_usda`
reads USDA nutrient 1079 and nothing stores it), vegetarian / vegan (no flag, and a
category such as "Proteins" does not say whether a food is meat), sugar and salt. Adding
any of them is a data decision for the owner, not a rule to write here.
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
    return tags
