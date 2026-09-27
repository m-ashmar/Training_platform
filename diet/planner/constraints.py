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

Incomplete allergen tagging (spec B4, 2026-09-27)
-------------------------------------------------
"Until allergen tagging is corrected, exclude any dish with incomplete tags from plans
for users who declared an allergy." A client who declared ANY allergy
(`declares_allergy`, which counts text the vocabulary cannot read, such as Arabic) is
never given a food whose tagging is incomplete (`forbids` -> `food_tagging_gaps`: not
`verified`, tags that are not the vocabulary, or a name / ingredient list implying a tag
the row does not store) nor a dish whose tagging is incomplete (`forbids_dish` ->
`dish_tagging_gaps`: no lines, an incomplete line, or a name, description, method or
line note implying an allergen no line carries). The pool builder, the recipe path, the
template path, the substitution step and the swap offers all ask this object, so this is
the one place the rule lives. A client with no declared allergy is untouched.

Measured on `tests/diet_quality.py measure(PROFILES, days=7)` against cf17d77, the same
seeded catalogue (136 foods, 105 dishes), in two trees. "Allergic" runs the harness with
every client it creates given that `allergies` text (a monkeypatched `_make_client`; the
harness has no allergic profile and none was added). "Stress" first adds, in the test's
transaction, an under-tagged twin of every dish (`<name> with Satay Sauce`, same lines)
and four under-tagged verified foods (one per food gap) — the catalogue the brief fears.

| run                         | code  | dish rate | distinct | max rep | drift worst | chosen share | pref sat | twin meals | bad portions |
|-----------------------------|-------|-----------|----------|---------|-------------|--------------|----------|------------|--------------|
| plain (the gate's)          | old   | 0.8512    | 64       | 8       | 2.181       | 0.2308       | 0.625    | -          | -            |
| plain (the gate's)          | new   | 0.8512    | 64       | 8       | 2.181       | 0.2308       | 0.625    | -          | -            |
| allergic `peanut`           | old   | 0.8333    | 57       | 8       | 2.767       | 0.248        | 0.6875   | -          | -            |
| allergic `peanut`           | new   | 0.8333    | 57       | 8       | 2.767       | 0.248        | 0.6875   | -          | -            |
| allergic `فول سوداني`       | old   | 0.8512    | 64       | 8       | 2.181       | 0.2308       | 0.625    | -          | -            |
| allergic `فول سوداني`       | new   | 0.8512    | 64       | 8       | 2.181       | 0.2308       | 0.625    | -          | -            |
| stress, no allergy          | old   | 0.8512    | 81       | 5       | 2.131       | 0.2222       | 0.625    | 91         | 5            |
| stress, no allergy          | new   | 0.8512    | 81       | 5       | 2.131       | 0.2222       | 0.625    | 91         | 5            |
| stress, `peanut`            | old   | 0.8571    | 85       | 5       | 2.418       | 0.2313       | 0.6875   | **92**     | **3**        |
| stress, `peanut`            | new   | 0.8333    | 57       | 8       | 2.767       | 0.248        | 0.6875   | 0          | 0            |
| stress, `فول سوداني`        | old   | 0.8512    | 81       | 5       | 2.131       | 0.2222       | 0.625    | 91         | 5            |
| stress, `فول سوداني`        | new   | 0.875     | 62       | 9       | 2.181       | 0.2308       | 0.625    | 2          | 0            |

Every other metric (absurd portions 0, days repeating a dish 0, fewest distinct portions
2, twin meals identical 0 of 28, chooser ranked first) is unchanged wherever the dish
columns are. Read it as:

* The shipped catalogue is completely tagged (`tests/test_allergen_safety.py` asserts
  it), so the rule costs nothing on it: every non-stress row is identical, and the plain
  row is `tests/diet_quality_baseline.json` to the digit. No ratchet moved; the baseline
  was NOT re-recorded.
* Under-tagging does not touch a client with no allergy (stress, no allergy: identical).
* The old code served a peanut-allergic client 92 meals of "... with Satay Sauce" whose
  peanut is on no line, and 3 portions of under-tagged foods. The new code serves none,
  and that client's plan is exactly the one they get from the clean catalogue.
* The Arabic declaration: the old code saw no allergy at all (the vocabulary reads only
  `[a-z']`) and served everything. The new code keeps every incomplete row away; the 2
  twin meals left are twins of the two dishes that DO carry peanut, so their tagging is
  complete. That client is still served peanut, because nothing reads `فول سوداني` as
  peanut — a vocabulary bug, reported separately, not this rule's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional


def normalise_cuisine(value) -> str:
    """Map any spelling onto universal / western / levantine.

    `Recipe.cuisine` is free text and holds "Levantine" and "Mediterranean"; the food
    catalogue uses a three-value choice. Mediterranean is the overlap, so it is universal.
    """
    v = (value or "").strip().lower()
    if v in ("levantine", "levant", "syrian", "lebanese", "arabic", "local"):
        return "levantine"
    if v in ("western", "american", "european"):
        return "western"
    return "universal"


@dataclass(frozen=True)
class CuisineChoice:
    """What the client asked for, as a ratio, and what that means for one food.

    `local_ratio` is 0.0 for Western only, 1.0 for Levantine only, and a mix between.
    Universal foods are always eligible and always neutral. At exactly 0 or 1 the other
    cuisine is excluded outright, because "only local" means only; between, the other
    cuisine is weighted down in proportion. The client chooses; both paths honour it.
    """

    local_ratio: float = 0.5

    def allows(self, cuisine) -> bool:
        c = normalise_cuisine(cuisine)
        if c == "universal":
            return True
        if c == "levantine":
            return self.local_ratio > 0.0
        return self.local_ratio < 1.0

    def weight(self, cuisine) -> float:
        """0.0 to 1.0, how well this food matches the client's ratio."""
        c = normalise_cuisine(cuisine)
        if c == "universal":
            return 1.0
        return self.local_ratio if c == "levantine" else 1.0 - self.local_ratio


@dataclass(frozen=True)
class ClientConstraints:
    """Hard limits on what may reach this client's plate.

    Hard means hard: unlike a preference, which ranks, nothing here is traded off
    against fit, variety or anything else. A food that fails this never enters the pool
    and never survives a recipe, at any stage, for any reason.
    """

    disliked_ids: FrozenSet[int] = frozenset()
    allergen_checker: object = None
    cuisine: CuisineChoice = CuisineChoice()
    #: The client wrote an allergy down (`diet.allergens.declares_allergy`) — including
    #: one the vocabulary cannot read, for which `allergen_checker` is inactive.
    declared_allergy: bool = False
    #: (kind, pk) -> incomplete, so a food or dish is audited once per generation, not
    #: once per meal it is considered for. Not part of the value.
    _incomplete_cache: dict = field(default_factory=dict, init=False, compare=False,
                                    hash=False, repr=False)

    @classmethod
    def for_user(cls, user) -> "ClientConstraints":
        """Read a client's allergies and dislikes once."""
        from diet.allergens import declares_allergy
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
        ratio = getattr(pref, "local_ratio", 0.5)
        return cls(
            disliked_ids=frozenset(f.id for f in pref.disliked_foods.all()),
            allergen_checker=AllergenChecker(raw) if raw else None,
            cuisine=CuisineChoice(float(0.5 if ratio is None else ratio)),
            declared_allergy=declares_allergy(raw),
        )

    @property
    def checks_allergens(self) -> bool:
        return bool(self.allergen_checker is not None
                    and getattr(self.allergen_checker, "active", False))

    @property
    def requires_complete_tags(self) -> bool:
        """Spec B4: this client declared an allergy, so nothing whose allergen tagging is
        incomplete may reach them — whichever allergy, because a row that cannot be
        vouched for cannot be cleared for anything."""
        return self.declared_allergy or self.checks_allergens

    @property
    def excludes_a_cuisine(self) -> bool:
        return self.cuisine.local_ratio in (0.0, 1.0)

    @property
    def active(self) -> bool:
        return (bool(self.disliked_ids) or self.checks_allergens
                or self.requires_complete_tags or self.excludes_a_cuisine)

    def _incomplete(self, kind: str, row, audit) -> bool:
        """Whether `audit(row)` found a gap, remembered per row for this generation."""
        pk = getattr(row, "pk", None)
        if pk is None:
            return bool(audit(row))
        key = (kind, pk)
        if key not in self._incomplete_cache:
            self._incomplete_cache[key] = bool(audit(row))
        return self._incomplete_cache[key]

    def incompletely_tagged(self, food) -> bool:
        """This food's allergen tagging is incomplete (`food_tagging_gaps`)."""
        from diet.allergens import food_tagging_gaps

        return self._incomplete("food", food, food_tagging_gaps)

    def forbids_dish(self, recipe) -> bool:
        """True when this DISH, as written, must not be served to this client.

        Only the completeness rule: a client who declared an allergy is never served a
        dish whose tagging is incomplete (`dish_tagging_gaps` — a line that is, or text
        such as `Chicken Satay` that implies an allergen no line carries). The recipe
        path asks this of the dish as authored, before any substitution; its lines are
        then still put through `forbids` one by one, as before.
        """
        if not self.requires_complete_tags:
            return False
        from diet.allergens import dish_tagging_gaps

        return self._incomplete("dish", recipe, dish_tagging_gaps)

    def forbids(self, food) -> bool:
        """True when this food must not be served to this client."""
        if getattr(food, "id", None) in self.disliked_ids:
            return True
        if not self.cuisine.allows(getattr(food, "cuisine", None)):
            return True
        # Before the checker: it refuses what it cannot clear FOR THE DECLARED
        # ALLERGENS, and a verified row whose tags contradict its own name, or are not
        # the vocabulary at all, clears for everything the name does not say. It also
        # covers a declaration the checker cannot read (Arabic), where it is inactive.
        if self.requires_complete_tags and self.incompletely_tagged(food):
            return True
        if self.checks_allergens:
            from diet.services.meal_validator import SAFE

            # Anything that is not positively SAFE is refused. The gate used to block
            # only VIOLATION, which meant a row the checker could not vouch for —
            # `allergen_source='unknown'`, no tags, a name carrying no marker word —
            # came back UNVERIFIED and was served. `Makdous` is eggplant stuffed with
            # walnuts and says so nowhere in its name; under the old rule it reached a
            # nut-allergic client's plate. The catalogue now declares a composition for
            # every row (`diet/data/food_allergens.py`), so failing closed costs a
            # client nothing and an unvouched import cannot reach them.
            return self.allergen_checker.check_food(food).verdict != SAFE
        return False

    def forbids_any(self, foods) -> bool:
        return any(self.forbids(food) for food in foods)

    def reason(self, food) -> Optional[str]:
        """Why this food is refused, for a log or a message to the client."""
        if getattr(food, "id", None) in self.disliked_ids:
            return "you marked this as disliked"
        if not self.cuisine.allows(getattr(food, "cuisine", None)):
            return "outside the cuisine you chose"
        if self.checks_allergens:
            from diet.services.meal_validator import UNVERIFIED, VIOLATION

            verdict = self.allergen_checker.check_food(food)
            if verdict.verdict == VIOLATION:
                return verdict.human
            if verdict.verdict == UNVERIFIED:
                return "we cannot confirm what this contains"
        if self.requires_complete_tags and self.incompletely_tagged(food):
            return "we cannot confirm what this contains"
        return None
