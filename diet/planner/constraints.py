"""What a client may not eat.

There was no single answer to that question. It was decided in three places, each
knowing about a different half of it:

* `build_pool` filtered allergens **and** dislikes, inline.
* `find_recipe` checked allergens and nothing else, so a dish made of a food the client
  had explicitly rejected was served anyway — and with three quarters of meals now
  coming from the recipe library, a dislike was honoured on the quarter of meals the
  engine assembled itself.
* Persistence checked dislikes and nothing else, at the very end, by refusing the whole
  plan. A client who disliked chicken did not get a plan without chicken. They got a
  `ConstraintViolationError` and no plan at all.

Three partial copies of one rule is how a rule ends up enforced nowhere in particular.
This is the rule, in one object, built once per generation and consulted by every path
that chooses a food.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class ClientConstraints:
    """Hard limits on what may reach this client's plate.

    Hard means hard: unlike a preference, which ranks, nothing here is traded off
    against fit, variety or anything else. A food that fails this never enters the pool
    and never survives a recipe, at any stage, for any reason.
    """

    disliked_ids: FrozenSet[int] = frozenset()
    allergen_checker: object = None

    @classmethod
    def for_user(cls, user) -> "ClientConstraints":
        """Read a client's allergies and dislikes once."""
        from diet.models import UserFoodPreference
        from diet.services.meal_validator import AllergenChecker

        if user is None or not getattr(user, "pk", None):
            return cls()

        pref = (UserFoodPreference.objects
                .filter(user=user)
                .prefetch_related("disliked_foods")
                .first())
        if pref is None:
            return cls()

        raw = getattr(pref, "allergies", None)
        return cls(
            disliked_ids=frozenset(f.id for f in pref.disliked_foods.all()),
            allergen_checker=AllergenChecker(raw) if raw else None,
        )

    @property
    def checks_allergens(self) -> bool:
        return bool(self.allergen_checker is not None
                    and getattr(self.allergen_checker, "active", False))

    @property
    def active(self) -> bool:
        return bool(self.disliked_ids) or self.checks_allergens

    def forbids(self, food) -> bool:
        """True when this food must not be served to this client."""
        if getattr(food, "id", None) in self.disliked_ids:
            return True
        if self.checks_allergens:
            from diet.services.meal_validator import VIOLATION

            return self.allergen_checker.check_food(food).verdict == VIOLATION
        return False

    def forbids_any(self, foods) -> bool:
        return any(self.forbids(food) for food in foods)

    def reason(self, food) -> Optional[str]:
        """Why this food is refused, for a log or a message to the client."""
        if getattr(food, "id", None) in self.disliked_ids:
            return "you marked this as disliked"
        if self.checks_allergens:
            from diet.services.meal_validator import VIOLATION

            verdict = self.allergen_checker.check_food(food)
            if verdict.verdict == VIOLATION:
                return verdict.human
        return None
