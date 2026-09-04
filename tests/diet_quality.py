"""Measure whether a generated diet plan is any good.

The gate already proves the engine does not crash and that its macros add up. Neither
tells you whether a client would eat what comes out, and that failure mode is silent:
a worse plan is still a plan and raises nothing. Everything the rebuild claims has to
move one of the numbers below, measured on the same fixed profiles every time.

Six metrics, each answering a question a person would actually ask:

* **dish rate** — is this a meal, or a pile of ingredients that happens to hit macros?
* **variety** — will tomorrow look different from today?
* **portion sanity** — would a person serve this much of it?
* **drift** — does the plan land on the calorie target, and does it miss in both directions?
* **personalisation** — does choosing your own items change what you are served?
* **pool sanity** — is a condiment being offered as a meal's carbohydrate?

Usage::

    from tests.diet_quality import measure, PROFILES
    report = measure(PROFILES, days=7)
    print(report.summary())
"""
from __future__ import annotations

import collections
import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# Fixed inputs. Every measurement uses these, so two runs are comparable.
# ---------------------------------------------------------------------------

#: (goals, height cm, weight kg, age, gender, activity). Spans the goal range and both
#: sexes, because the calorie target and the macro split both move with them.
PROFILES: Sequence[tuple] = (
    (["Weight Loss"], 160, 55, 45, "Female", "Sedentary"),
    (["Weight Loss"], 175, 95, 30, "Male", "Light"),
    (["Maintain"], 175, 78, 28, "Male", "Moderate"),
    (["Maintain"], 168, 62, 35, "Female", "Active"),
    (["Muscle Gain"], 182, 70, 22, "Male", "Active"),
    (["Muscle Gain"], 170, 58, 26, "Female", "VeryActive"),
)

#: Calorie targets for the drift measurement, spanning the engine's accepted range.
DRIFT_TARGETS: Sequence[int] = (1400, 1800, 2200, 2600, 3000)

#: What one client says they want, slot by slot, for the personalisation measurement.
#: Foods a Levantine catalogue should plausibly contain; missing ones are skipped and
#: reported, so a thin catalogue shows up as a caveat rather than a silent zero.
SLOT_CHOICES: Sequence[Tuple[str, str, Sequence[str]]] = (
    ("Breakfast", "protein", ("Egg White", "Greek Yogurt")),
    ("Breakfast", "carb", ("Oats",)),
    ("Lunch", "protein", ("Chicken Breast",)),
    ("Lunch", "carb", ("White Rice",)),
    ("Dinner", "protein", ("Salmon",)),
    ("Dinner", "carb", ("Sweet Potato",)),
)

#: Provisional ceiling per food, in grams, for "would a person serve this much".
#: Keyed by a substring of the food name because the catalogue has no unit data yet.
#: Phase 1.2 adds `FoodItem.max_units`; once it is populated, `_portion_ceiling` reads
#: that instead and this table goes away.
SANE_MAX_G: Dict[str, int] = {
    "oil": 30, "butter": 40, "almond butter": 45, "seed": 40, "nuts": 60, "almond": 60,
    "salt": 5, "sauce": 60, "jelly": 40, "syrup": 40, "honey": 40, "seasoning": 10,
    "soy sauce": 30, "vinegar": 30,
    "oat": 120, "rice": 250, "freekeh": 250, "bread": 150, "pasta": 250, "potato": 350,
    "lentil": 300, "bean": 300, "chickpea": 300,
    "egg white": 200, "egg": 200, "cheese": 120, "yogurt": 400, "labneh": 150,
    "chicken": 300, "salmon": 250, "cod": 250, "beef": 300, "lamb": 300, "fish": 250,
    "avocado": 200, "squash": 250, "spinach": 200, "broccoli": 250,
}

#: Words that mark a food as something you add by the spoon, not build a meal from.
CONDIMENT_WORDS = ("sauce", "jelly", "syrup", "seasoning", "dressing", "ketchup",
                   "mayo", "mustard", "vinegar", "cola", "soda", "pepsi", "drink")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class QualityReport:
    """Everything one measurement run learned. All fractions are 0.0 to 1.0."""

    dish_rate_overall: float = 0.0
    dish_rate_by_slot: Dict[str, float] = field(default_factory=dict)
    meals_measured: int = 0

    distinct_dishes: int = 0
    max_repeats_of_one_dish: int = 0
    dishes_served: Dict[str, int] = field(default_factory=dict)

    absurd_portion_rate: float = 0.0
    absurd_examples: List[str] = field(default_factory=list)
    portions_measured: int = 0
    min_distinct_portions_per_food: int = 0

    drift_by_target: Dict[int, float] = field(default_factory=dict)
    drift_all_one_sided: bool = True
    drift_worst_abs: float = 0.0

    twin_identical_meals: int = 0
    twin_total_meals: int = 0
    chosen_ingredient_share: float = 0.0
    chooser_pool_ranked_first: bool = False

    condiment_slots: List[str] = field(default_factory=list)
    catalogue_gaps: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"meals measured            {self.meals_measured}",
            f"dish rate overall         {self.dish_rate_overall:.0%}",
        ]
        for slot in sorted(self.dish_rate_by_slot):
            lines.append(f"  {slot:<23} {self.dish_rate_by_slot[slot]:.0%}")
        lines += [
            f"distinct dishes           {self.distinct_dishes}",
            f"most repeats of one dish  {self.max_repeats_of_one_dish}",
            f"portions measured         {self.portions_measured}",
            f"absurd portions           {self.absurd_portion_rate:.0%}",
            f"fewest distinct portions  {self.min_distinct_portions_per_food}",
            f"drift worst               {self.drift_worst_abs:+.1f}%",
            f"drift one-sided           {self.drift_all_one_sided}",
            f"twin meals identical      {self.twin_identical_meals} of {self.twin_total_meals}",
            f"chosen ingredient share   {self.chosen_ingredient_share:.0%}",
            f"chooser ranked first      {self.chooser_pool_ranked_first}",
            f"condiment-topped slots    {len(self.condiment_slots)}",
        ]
        if self.catalogue_gaps:
            lines.append(f"catalogue gaps            {', '.join(self.catalogue_gaps)}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "dish_rate_overall": round(self.dish_rate_overall, 4),
            "dish_rate_by_slot": {k: round(v, 4) for k, v in self.dish_rate_by_slot.items()},
            "meals_measured": self.meals_measured,
            "distinct_dishes": self.distinct_dishes,
            "max_repeats_of_one_dish": self.max_repeats_of_one_dish,
            "absurd_portion_rate": round(self.absurd_portion_rate, 4),
            "portions_measured": self.portions_measured,
            "min_distinct_portions_per_food": self.min_distinct_portions_per_food,
            "drift_by_target": {str(k): round(v, 3) for k, v in self.drift_by_target.items()},
            "drift_all_one_sided": self.drift_all_one_sided,
            "drift_worst_abs": round(self.drift_worst_abs, 3),
            "twin_identical_meals": self.twin_identical_meals,
            "twin_total_meals": self.twin_total_meals,
            "chosen_ingredient_share": round(self.chosen_ingredient_share, 4),
            "chooser_pool_ranked_first": self.chooser_pool_ranked_first,
            "condiment_slots": sorted(self.condiment_slots),
            "catalogue_gaps": sorted(self.catalogue_gaps),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grams(quantity) -> float:
    return float(re.sub(r"[^0-9.]", "", str(quantity)) or 0)


def _portion_ceiling(food_name: str):
    """Grams above which a person would not serve this food, or None if unknown.

    Matched on word boundaries, not substrings: "Butternut Squash" is squash, not
    butter, and a plain substring test filed it at a 40 g ceiling and reported every
    serving as absurd. This is the same fault as `classify_food` reading the token
    "cherry" out of "Diet Pepsi Drink Wild Cherry", so the tool that measures it should
    not repeat it.

    Longest key wins, so "almond butter" beats "butter" and "soy sauce" beats "sauce".
    """
    low = (food_name or "").lower()
    best = None
    for key, cap in SANE_MAX_G.items():
        if re.search(r"\b" + re.escape(key) + r"\b", low):
            if best is None or len(key) > len(best[0]):
                best = (key, cap)
    return best[1] if best else None


def _is_condiment(food_name: str) -> bool:
    low = (food_name or "").lower()
    return any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in CONDIMENT_WORDS)


def _make_client(username: str, goals, height, weight, age, gender, activity):
    from users.models import CustomUser

    user = CustomUser.objects.create_user(
        email=f"{username}@quality.test", username=username, password="Xx!23456")
    user.user_type = "client"
    user.is_active = True
    user.client_goals = list(goals)
    user.height, user.weight, user.age, user.gender = height, weight, age, gender
    user.activity_level = activity
    user.save()
    return user


def _recipe_names() -> set:
    from diet.models import Recipe
    return set(Recipe.objects.values_list("name", flat=True))


def _resolve_food(name: str):
    """Find a food the way `seed_recipes` does: exact, then the shortest containing name.

    The catalogue writes "Chicken Breast (Grilled)" and "Greek Yogurt (Non-Fat)" where a
    recipe asks for "Chicken Breast" and "Greek Yogurt". A harness that only matched
    exactly reported six of its own eight choice foods as missing and measured
    personalisation against two, which made the number meaningless rather than merely
    imprecise.
    """
    from diet.models import FoodItem

    exact = FoodItem.objects.filter(name__iexact=name, needs_review=False).first()
    if exact is not None:
        return exact
    candidates = sorted(
        FoodItem.objects.filter(name__icontains=name, needs_review=False),
        key=lambda f: (len(f.name), f.name),
    )
    return candidates[0] if candidates else None


def _generate(user, kcal: float, days: int):
    """One plan, or None if the engine refused. Refusals are a finding, not a crash."""
    from diet.services.rule_based_planner import RuleBasedPlanner

    try:
        return RuleBasedPlanner(user).generate(
            daily_kcal=float(kcal), meal_count=3, snack_count=1, duration_days=days)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------

def measure(profiles: Sequence[tuple] = PROFILES, days: int = 7,
            drift_targets: Sequence[int] = DRIFT_TARGETS) -> QualityReport:
    """Generate plans for `profiles` and report the six quality metrics."""
    from diet.models import FoodItem, UserFoodCategoryPreference
    from diet.planner.candidates import build_pool
    from diet.planner.policy import load_policy

    report = QualityReport()
    tag = uuid.uuid4().hex[:6]
    dishes = _recipe_names()

    # ---- dish rate, variety, portions -------------------------------------
    slot_counts: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    dish_tally: collections.Counter = collections.Counter()
    portions_by_food: Dict[str, List[float]] = collections.defaultdict(list)
    absurd = 0
    total_portions = 0

    for index, (goals, h, w, a, g, act) in enumerate(profiles):
        user = _make_client(f"q{index}{tag}", goals, h, w, a, g, act)
        plan = _generate(user, user.calculate_daily_calories(), days)
        if plan is None:
            report.catalogue_gaps.append(f"generation failed for profile {index}")
            continue
        for meal in plan.plan:
            slot = meal.meal_type or "?"
            is_dish = meal.meal_name in dishes
            slot_counts[slot][0 if is_dish else 1] += 1
            report.meals_measured += 1
            if is_dish:
                dish_tally[meal.meal_name] += 1
            for ingredient in meal.ingredients:
                grams = _grams(ingredient.quantity)
                total_portions += 1
                portions_by_food[ingredient.name].append(grams)
                ceiling = _portion_ceiling(ingredient.name)
                if ceiling is not None and grams > ceiling:
                    absurd += 1
                    if len(report.absurd_examples) < 12:
                        report.absurd_examples.append(
                            f"{grams:.0f}g {ingredient.name} in {slot} (max ~{ceiling}g)")

    for slot, (d, p) in slot_counts.items():
        report.dish_rate_by_slot[slot] = d / (d + p) if (d + p) else 0.0
    served = sum(d for d, _ in slot_counts.values())
    report.dish_rate_overall = served / report.meals_measured if report.meals_measured else 0.0
    report.dishes_served = dict(dish_tally)
    report.distinct_dishes = len(dish_tally)
    report.max_repeats_of_one_dish = max(dish_tally.values()) if dish_tally else 0
    report.portions_measured = total_portions
    report.absurd_portion_rate = absurd / total_portions if total_portions else 0.0

    # A food served often but at one or two amounts is a floor acting as an attractor.
    repeated = [vals for vals in portions_by_food.values() if len(vals) >= 4]
    report.min_distinct_portions_per_food = (
        min(len({round(v) for v in vals}) for vals in repeated) if repeated else 0)

    # ---- drift -------------------------------------------------------------
    signs = set()
    for target in drift_targets:
        user = _make_client(f"qd{target}{tag}", ["Maintain"], 175, 78, 28, "Male", "Moderate")
        plan = _generate(user, target, 1)
        if plan is None:
            report.catalogue_gaps.append(f"generation failed at {target} kcal")
            continue
        produced = sum(float(m.total_nutrition.get("calories", 0) or 0) for m in plan.plan)
        pct = (produced - target) / target * 100 if target else 0.0
        report.drift_by_target[target] = pct
        signs.add(pct >= 0)
    report.drift_all_one_sided = len(signs) <= 1
    report.drift_worst_abs = max((abs(v) for v in report.drift_by_target.values()), default=0.0)

    # ---- personalisation ---------------------------------------------------
    chooser = _make_client(f"qc{tag}", ["Maintain"], 175, 78, 28, "Male", "Moderate")
    twin = _make_client(f"qt{tag}", ["Maintain"], 175, 78, 28, "Male", "Moderate")

    chosen_names = set()
    resolved = {}
    for meal_slot, macro, names in SLOT_CHOICES:
        for name in names:
            food = _resolve_food(name)
            if food is None:
                report.catalogue_gaps.append(f"missing food: {name}")
                continue
            resolved[name] = food
            UserFoodCategoryPreference.objects.create(
                user=chooser, food=food, meal=meal_slot, macro=macro)
            chosen_names.add(food.name)

    pool = build_pool(chooser, load_policy("maintain"))
    ranked_first = True
    for meal_slot, macro, names in SLOT_CHOICES:
        ranked = pool.by_slot.get(meal_slot, {}).get(macro, [])
        head = {f.name.lower() for f in ranked[:len(names)]}
        for name in names:
            food = resolved.get(name)
            if food is not None and food.name.lower() not in head:
                ranked_first = False
    report.chooser_pool_ranked_first = ranked_first

    kcal = chooser.calculate_daily_calories()
    chooser_plan = _generate(chooser, kcal, days)
    twin_plan = _generate(twin, kcal, days)
    if chooser_plan and twin_plan:
        a_names = [m.meal_name for m in chooser_plan.plan]
        b_names = [m.meal_name for m in twin_plan.plan]
        report.twin_total_meals = min(len(a_names), len(b_names))
        report.twin_identical_meals = sum(
            1 for x, y in zip(a_names, b_names) if x == y)

        chosen_hits = seen = 0
        for meal in chooser_plan.plan:
            for ingredient in meal.ingredients:
                seen += 1
                if ingredient.name in chosen_names:
                    chosen_hits += 1
        report.chosen_ingredient_share = chosen_hits / seen if seen else 0.0

    # ---- pool sanity -------------------------------------------------------
    plain = _make_client(f"qp{tag}", ["Maintain"], 175, 78, 28, "Male", "Moderate")
    plain_pool = build_pool(plain, load_policy("maintain"))
    for meal_slot, macros in plain_pool.by_slot.items():
        for macro, foods in macros.items():
            for food in foods[:5]:
                if _is_condiment(getattr(food, "name", "")):
                    report.condiment_slots.append(f"{meal_slot}/{macro}")
                    break

    return report
