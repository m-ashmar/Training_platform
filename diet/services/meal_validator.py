from __future__ import annotations

from typing import Iterable, Tuple, Set
from ..models import FoodItem


class MealValidator:
    """
    Validate meal components against user constraints.
    - Ensures no allergens present
    - Optionally enforce category pools (strict mode)
    """

    def __init__(self, user_allergies: str | None, category_pool: set[str] | None = None, strict: bool = False):
        self.user_allergies = (user_allergies or "").lower()
        self.category_pool = category_pool or set()
        self.strict = strict

    def validate(self, components: Iterable[Tuple[FoodItem, str]]) -> Iterable[Tuple[FoodItem, str]]:
        for food, qty in components:
            name_l = (food.name or "").lower()
            if self._violates_allergy(name_l):
                continue
            if self.strict and self.category_pool and food.name not in self.category_pool:
                # skip items not in strict category pool
                continue
            yield food, qty

    def _violates_allergy(self, food_name_l: str) -> bool:
        if not self.user_allergies:
            return False
        # naive substring check; can be enhanced with taxonomy
        return any(token in food_name_l for token in self.user_allergies.split(",") if token.strip())


