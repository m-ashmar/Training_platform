from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Set, Tuple

from ..allergens import (
    infer_allergens,
    label,
    parse_user_allergies,
)
from ..models import FoodItem


# Verdicts for a single ingredient.
SAFE = "safe"
VIOLATION = "violation"        # a declared allergen is definitely present
UNVERIFIED = "unverified"      # no trustworthy allergen data — needs review, NOT safe


@dataclass
class IngredientVerdict:
    food_name: str
    verdict: str
    matched: List[str] = field(default_factory=list)   # canonical tags that matched
    source: str = "unknown"

    @property
    def human(self) -> str:
        if self.verdict == VIOLATION:
            return f"{self.food_name}: contains {', '.join(label(t) for t in self.matched)}"
        if self.verdict == UNVERIFIED:
            return f"{self.food_name}: allergen data unavailable — needs review"
        return f"{self.food_name}: ok"


@dataclass
class AllergenReport:
    """What the checker found. Callers act on this instead of guessing from silence."""
    verdicts: List[IngredientVerdict] = field(default_factory=list)

    @property
    def violations(self) -> List[IngredientVerdict]:
        return [v for v in self.verdicts if v.verdict == VIOLATION]

    @property
    def unverified(self) -> List[IngredientVerdict]:
        return [v for v in self.verdicts if v.verdict == UNVERIFIED]

    @property
    def is_safe(self) -> bool:
        """True only when every ingredient is positively cleared.

        Deliberately strict: an ingredient with no allergen data is NOT safe, it is
        unknown, and the caller has to decide what to do about that.
        """
        return not self.violations and not self.unverified

    def as_dict(self) -> dict:
        return {
            "is_safe": self.is_safe,
            "violations": [{"food": v.food_name, "allergens": v.matched} for v in self.violations],
            "unverified": [v.food_name for v in self.unverified],
        }


class AllergenChecker:
    """Ingredient-level allergen checking against a user's declared allergies.

    Replaces the old approach of substring-matching the user's free-text allergy list
    against a food's display NAME. That could not see composition at all — `Pad Thai`
    contains peanuts and its name never says so — and it failed on the name too
    (`"peanuts"` did not match `"Peanut butter"`).

    Here the user's list is normalised to canonical tags once, and each ingredient is
    judged on its stored `allergens` (plus its ingredient text), with its trust level
    carried through so unverified data is reported rather than assumed safe.
    """

    def __init__(self, user_allergies: str | None):
        self.user_tags: Set[str] = parse_user_allergies(user_allergies)
        self.canonical = {t for t in self.user_tags if not t.startswith("free:")}
        self.free_terms = {t[5:] for t in self.user_tags if t.startswith("free:")}

    @property
    def active(self) -> bool:
        return bool(self.user_tags)

    def check_food(self, food: FoodItem) -> IngredientVerdict:
        name = getattr(food, "name", "") or ""
        source = getattr(food, "allergen_source", "unknown") or "unknown"
        stored = set(getattr(food, "allergens", None) or [])
        text = f"{name} {getattr(food, 'ingredients_text', '') or ''}"

        if not self.active:
            return IngredientVerdict(name, SAFE, [], source)

        # 1. Curated tags are authoritative in both directions.
        matched = sorted(stored & self.canonical)
        # 2. Free-text terms the vocabulary did not recognise are still matched on text.
        matched += sorted(t for t in self.free_terms if t and t in text.lower())
        # 3. Name/ingredient inference catches rows whose tags are missing or stale.
        matched += sorted(infer_allergens(text) & self.canonical - set(matched))

        if matched:
            return IngredientVerdict(name, VIOLATION, sorted(set(matched)), source)
        if source != "verified":
            # No match found, but the data is not trustworthy enough to call it safe.
            return IngredientVerdict(name, UNVERIFIED, [], source)
        return IngredientVerdict(name, SAFE, [], source)

    def check_foods(self, foods: Iterable[FoodItem]) -> AllergenReport:
        return AllergenReport([self.check_food(f) for f in foods])

    def check_meal(self, meal) -> AllergenReport:
        """Check every MealComponent of a persisted Meal."""
        foods = [c.food for c in meal.components.select_related("food").all()] \
            if hasattr(meal, "components") else []
        return self.check_foods(foods)


class MealValidator:
    """
    Validate meal components against user constraints.
    - Drops ingredients that violate a declared allergy
    - Optionally enforce category pools (strict mode)

    `self.report` holds the full per-ingredient outcome after `validate()` runs, so a
    caller can surface violations and unverified ingredients instead of inferring
    safety from the fact that nothing was filtered.
    """

    def __init__(self, user_allergies: str | None, category_pool: set[str] | None = None,
                 strict: bool = False, drop_unverified: bool = False):
        self.user_allergies = (user_allergies or "").lower()
        self.checker = AllergenChecker(user_allergies)
        self.category_pool = category_pool or set()
        self.strict = strict
        # Off by default: with 346 legacy rows carrying no allergen data, dropping every
        # unverified ingredient would empty the plan. They are REPORTED instead.
        self.drop_unverified = drop_unverified
        self.report = AllergenReport()

    def validate(self, components: Iterable[Tuple[FoodItem, str]]) -> Iterable[Tuple[FoodItem, str]]:
        for food, qty in components:
            verdict = self.checker.check_food(food)
            self.report.verdicts.append(verdict)
            if verdict.verdict == VIOLATION:
                continue
            if verdict.verdict == UNVERIFIED and self.drop_unverified:
                continue
            if self.strict and self.category_pool and food.name not in self.category_pool:
                continue
            yield food, qty
