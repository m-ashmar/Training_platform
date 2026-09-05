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
from typing import Dict, List, Optional, Sequence, Tuple

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
#:
#: Deliberately NOT the foods the engine would serve anyway. The first version of this
#: list was chicken, rice, oats, eggs and yogurt — plausible, and identical to what the
#: recipe library already ranks first at each meal, so the measurement asked whether
#: choosing what you would be given changes what you are given. The answer was no and
#: the number looked like a personalisation failure. These are foods a Levantine client
#: might well pick and the default ordering does not: bulgur rather than rice, cottage
#: cheese rather than yogurt, chickpeas rather than a chicken breast.
SLOT_CHOICES: Sequence[Tuple[str, str, Sequence[str]]] = (
    ("Breakfast", "protein", ("Cottage Cheese", "Turkey Breast")),
    ("Breakfast", "carb", ("Pita Bread", "Whole Wheat Bread")),
    ("Lunch", "protein", ("Tuna (Fresh)", "Chickpeas")),
    ("Lunch", "carb", ("Bulgur", "Barley")),
    ("Dinner", "protein", ("Shrimp", "Cod Fillet")),
    ("Dinner", "carb", ("Quinoa", "Lentils")),
    ("Snack", "fat", ("Walnuts", "Pumpkin Seeds")),
    ("Snack", "fruit", ("Orange", "Grapes")),
)


#: Fallback ceilings, in grams, for a food the catalogue has not given a serving unit.
#: This table used to be the ONLY definition of "too much", maintained here beside a
#: second definition inside the engine, and the two disagreed: 53 portions above a
#: food's own declared maximum passed a green gate because this table did not know
#: about `max_units`. A measurement must not re-implement the thing it measures, so
#: `_portion_ceiling` now asks the engine and falls back to these only for the handful
#: of foods that carry no unit at all.
SANE_MAX_G: Dict[str, int] = {
    "oil": 30, "butter": 40, "almond butter": 45, "seed": 40, "nuts": 60, "almond": 60,
    "salt": 5, "sauce": 60, "jelly": 40, "syrup": 40, "honey": 40, "seasoning": 10,
    "soy sauce": 30, "vinegar": 30,
    "oat": 120, "rice": 400, "freekeh": 400, "bread": 150, "pasta": 350, "potato": 400,
    "lentil": 400, "bean": 400, "chickpea": 400,
    "egg white": 200, "egg": 200, "cheese": 120, "yogurt": 400, "labneh": 150,
    "chicken": 300, "salmon": 300, "cod": 300, "beef": 300, "lamb": 300, "fish": 300,
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
    #: Repeats and variety per slot. A single global figure treats a slot with one
    #: usable recipe and a slot with four as the same situation, so it cannot say
    #: whether the planner repeated a dish or simply had nothing else to serve.
    max_repeats_by_slot: Dict[str, int] = field(default_factory=dict)
    distinct_dishes_by_slot: Dict[str, int] = field(default_factory=dict)
    meals_by_slot: Dict[str, int] = field(default_factory=dict)
    #: Days on which one dish was served twice. Nothing about the library forces this;
    #: it is the planner failing to notice what it had already served that morning.
    days_repeating_a_dish: int = 0
    days_measured: int = 0
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
    #: Of those, the ones the engine assembled itself. See the note where these are set.
    twin_identical_built: int = 0
    twin_built_meals: int = 0
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
            f"days with a repeated dish {self.days_repeating_a_dish} of {self.days_measured}",
            f"fewest distinct portions  {self.min_distinct_portions_per_food}",
            f"drift worst               {self.drift_worst_abs:+.1f}%",
            f"drift one-sided           {self.drift_all_one_sided}",
            f"twin meals identical      {self.twin_identical_meals} of {self.twin_total_meals}",
            f"  of the meals we built     {self.twin_identical_built} of {self.twin_built_meals}",
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
            "max_repeats_by_slot": dict(self.max_repeats_by_slot),
            "distinct_dishes_by_slot": dict(self.distinct_dishes_by_slot),
            "days_repeating_a_dish": self.days_repeating_a_dish,
            "portions_measured": self.portions_measured,
            "min_distinct_portions_per_food": self.min_distinct_portions_per_food,
            "drift_by_target": {str(k): round(v, 3) for k, v in self.drift_by_target.items()},
            "drift_all_one_sided": self.drift_all_one_sided,
            "drift_worst_abs": round(self.drift_worst_abs, 3),
            "twin_identical_meals": self.twin_identical_meals,
            "twin_total_meals": self.twin_total_meals,
            "twin_identical_built": self.twin_identical_built,
            "twin_built_meals": self.twin_built_meals,
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


def _declared_ceiling(food) -> Optional[float]:
    """The most of this food the engine will serve, in grams, if it declares a unit.

    Asks the engine rather than recomputing it. `unit_grams * max_units` looks like the
    same number and is not: the ladder is built from rungs, and whether the declared
    maximum is one of them is a property of how the ladder is generated. Multiplying it
    out here meant the gate measured a bound the planner had never agreed to, and
    53 portions above a food's own maximum passed.
    """
    from diet.planner.portion import ceiling_grams, unit_levels

    return ceiling_grams(food) if unit_levels(food) else None


def _portion_ceiling(food_name: str):
    """Fallback ceiling for a food that declares no serving unit.

    Matched on word boundaries, not substrings: "Butternut Squash" is squash, not
    butter, and a plain substring test filed it at a 40 g ceiling and reported every
    serving as absurd. This is the same fault as `classify_food` reading the token
    "cherry" out of "Diet Pepsi Drink Wild Cherry", so the tool that measures it should
    not repeat it. Longest key wins, so "almond butter" beats "butter".
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


def _generate(user, kcal: float, days: int, salt: str):
    """One plan, or None if the engine refused. Refusals are a finding, not a crash.

    `salt` replaces the client's row id as the seed for the planner's per-day generator.
    A measurement creates a new client every run, so the id changes, so the seed changes,
    so the plan changes: drift read 1.2% and 9.2% on consecutive runs of identical code,
    and the baseline file recorded one draw as though it were the value. The salt names
    what is being measured — the profile, the calorie target, the client who chose —
    so the same measurement plans the same days every time.
    """
    from diet.services.rule_based_planner import RuleBasedPlanner

    try:
        return RuleBasedPlanner(user, seed_salt=salt).generate(
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
    # A fresh username per run only keeps the reused test database from colliding; the
    # planner seeds each day's generator from the client's row id, so a new row means a
    # new seed and a different plan. Every meal below is planned under a FIXED salt so
    # two runs of the same code produce the same numbers and a change is attributable.
    tag = uuid.uuid4().hex[:6]
    dishes = _recipe_names()

    # ---- dish rate, variety, portions -------------------------------------
    slot_counts: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    dish_tally: collections.Counter = collections.Counter()
    portions_by_food: Dict[str, List[float]] = collections.defaultdict(list)
    absurd = 0
    per_slot_tally: Dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter)
    total_portions = 0

    for index, (goals, h, w, a, g, act) in enumerate(profiles):
        user = _make_client(f"q{index}{tag}", goals, h, w, a, g, act)
        plan = _generate(user, user.calculate_daily_calories(), days, f"profile:{index}")
        if plan is None:
            report.catalogue_gaps.append(f"generation failed for profile {index}")
            continue
        for offset in range(0, len(plan.plan), 4):
            report.days_measured += 1
            served = [m.meal_name for m in plan.plan[offset:offset + 4]
                      if m.meal_name in dishes]
            if len(served) != len(set(served)):
                report.days_repeating_a_dish += 1
        for meal in plan.plan:
            slot = meal.meal_type or "?"
            is_dish = meal.meal_name in dishes
            slot_counts[slot][0 if is_dish else 1] += 1
            report.meals_measured += 1
            if is_dish:
                dish_tally[meal.meal_name] += 1
                per_slot_tally[slot][meal.meal_name] += 1
            for ingredient in meal.ingredients:
                grams = _grams(ingredient.quantity)
                total_portions += 1
                portions_by_food[ingredient.name].append(grams)
                food = _resolve_food(ingredient.name)
                ceiling = _declared_ceiling(food) if food else None
                if ceiling is None:
                    ceiling = _portion_ceiling(ingredient.name)
                if ceiling is not None and grams > ceiling + 0.5:
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
    report.max_repeats_by_slot = {
        slot: max(tally.values()) for slot, tally in per_slot_tally.items() if tally}
    report.distinct_dishes_by_slot = {
        slot: len(tally) for slot, tally in per_slot_tally.items()}
    report.meals_by_slot = {slot: sum(counts) for slot, counts in slot_counts.items()}
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
        plan = _generate(user, target, 1, f"drift:{target}")
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
    # One client, measured before and after they choose. Two different clients would
    # not isolate anything: the planner seeds its variety generator from the user id,
    # so half the difference between two people is which random draws each got. Same
    # id, same seed, one variable — whether they picked their food.
    chooser = _make_client(f"qc{tag}", ["Maintain"], 175, 78, 28, "Male", "Moderate")
    kcal_before = chooser.calculate_daily_calories()
    before_plan = _generate(chooser, kcal_before, days, "twin")

    chosen_names = set()
    resolved = {}
    chosen_ids: set = set()
    for meal_slot, macro, names in SLOT_CHOICES:
        for name in names:
            food = _resolve_food(name)
            if food is None:
                report.catalogue_gaps.append(f"missing food: {name}")
                continue
            resolved[name] = food
            chosen_ids.add(food.id)
            UserFoodCategoryPreference.objects.create(
                user=chooser, food=food, meal=meal_slot, macro=macro)
            chosen_names.add(food.name)

    # Look for the food in whichever slot it actually occupies, not the one the client
    # filed it under. A client may call chickpeas their lunch protein while the engine
    # classifies them by the macro that leads on calories and files them as a carb; the
    # question this measures is whether the choice reached the top of a list, not
    # whether the two agree about what a chickpea is.
    pool = build_pool(chooser, load_policy("maintain"))
    ranked_first = True
    for meal_slot, macro, names in SLOT_CHOICES:
        for name in names:
            food = resolved.get(name)
            if food is None:
                continue
            lists = [lst for lst in pool.by_slot.get(meal_slot, {}).values()
                     if any(f.id == food.id for f in lst)]
            if not lists:
                ranked_first = False
                continue
            # Ahead of everything the client did NOT choose, rather than inside the
            # first N: three chosen foods can land in one slot, and then the third of
            # them is fourth in the list and the property still holds.
            if not all(
                all(other.id in chosen_ids or i > lst.index(food)
                    for i, other in enumerate(lst[:lst.index(food)]))
                for lst in lists
            ):
                ranked_first = False
    report.chooser_pool_ranked_first = ranked_first

    after_plan = _generate(chooser, kcal_before, days, "twin")
    if before_plan and after_plan:
        # Compared by what is on the plate, not by what the meal is called. A meal built
        # from a template is named after its slot — "Breakfast" — so two plates made of
        # entirely different food carry the same name, and comparing names reported no
        # change at all while the ingredients were visibly moving.
        def signature(meal):
            return (meal.meal_name, tuple(sorted(i.name for i in meal.ingredients)))

        a = [signature(m) for m in before_plan.plan]
        b = [signature(m) for m in after_plan.plan]
        report.twin_total_meals = min(len(a), len(b))
        report.twin_identical_meals = sum(1 for x, y in zip(a, b) if x == y)

        # The same count, restricted to meals the engine BUILT rather than looked up.
        # A recipe is a fixed combination someone wrote down: when none of the dishes
        # that fit a slot contains what the client asked for, the honest answer is that
        # the library does not cover their tastes, and no amount of ranking changes the
        # plate. Mixing the two makes a library gap read as a personalisation failure —
        # and hid the reverse too, because the first version of SLOT_CHOICES named the
        # library's own staples and the metric could not have moved either way.
        built = [i for i, m in enumerate(after_plan.plan)
                 if m.meal_name not in dishes and i < report.twin_total_meals]
        report.twin_built_meals = len(built)
        report.twin_identical_built = sum(1 for i in built if a[i] == b[i])

        chosen_hits = seen = 0
        for meal in after_plan.plan:
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
