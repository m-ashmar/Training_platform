"""Dish assembly.

Turns a macro target into a recognisable meal instead of a list of foods that happens to
add up. The planner's component fill stays as the fallback, because a recipe library
will never cover every target — but when a dish fits, a dish is what the user gets.
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .optimize import Components, _better, refine, totals_of
from .policy import PlannerPolicy
from .report import NEGLIGIBLE, deviation_of
# One temperature for both paths. templates does not import this module, so it is safe.
from .templates import SOFTMAX_T

logger = logging.getLogger(__name__)


@dataclass
class RecipeMatch:
    recipe: object
    components: Components
    servings: float
    deviation: object
    #: Foods added from the client's pool to bring an under-target dish up to its
    #: slot. The dish keeps its name and says what came with it.
    extras: tuple = ()
    #: A food the client chose, substituted for the line it most resembles.
    substituted: object = None
    #: The line it replaced. Without it the name read as an addition: "Pomegranate and
    #: Yogurt (with Orange)" had no pomegranate in it.
    replaced: object = None

    @property
    def name(self) -> str:
        base = getattr(self.recipe, "name", "meal")
        if self.substituted is not None:
            new = plain_name(self.substituted)
            old = plain_name(self.replaced) if self.replaced is not None else None
            base = f"{base} ({new} instead of {old})" if old else f"{base} (with {new})"
        if not self.extras:
            return base
        return f"{base} + {' + '.join(plain_name(f) for f in self.extras)}"

    @property
    def name_ar(self) -> str:
        """The same name for an Arabic client: the recipe's own translation, the
        substituted and added foods by their Arabic names."""
        def ar(obj, fallback="اختيارك"):
            return getattr(obj, "name_ar", None) or getattr(obj, "name", None) or fallback
        base = getattr(self.recipe, "name_ar", None) or getattr(self.recipe, "name", "وجبة")
        if self.substituted is not None:
            new = ar(self.substituted)
            old = ar(self.replaced, None) if self.replaced is not None else None
            base = f"{base} ({new} بدلاً من {old})" if old else f"{base} (مع {new})"
        if not self.extras:
            return base
        return f"{base} + {' + '.join(ar(f) for f in self.extras)}"


def plain_name(food) -> str:
    """A food's name without its catalogue qualifier.

    The catalogue distinguishes "Pita Bread (White)" from "Pita Bread (Whole Wheat)"
    and that belongs in a food list. Inside a meal title it nests parentheses inside
    parentheses — "Labneh with Honey (Pita Bread (White) instead of Pita Bread (Whole
    Wheat))" — which no one would write down. The Arabic names never carried them.
    """
    # The English column when one exists. `plain_name` feeds `_names_the_dish`, which
    # decides whether a line may be substituted — a decision, not a display. Under an
    # Arabic request both the food and the recipe returned Arabic, the ASCII word match
    # found nothing, and the "never replace the ingredient the dish is named after"
    # rule was silently off for every dish.
    name = getattr(food, "name_en", None) or getattr(food, "name", None) or str(food)
    return name.split(" (")[0].strip() or name


def _recipe_lines(recipe) -> List[Tuple[object, float, bool]]:
    """A recipe's ingredients as plain data, read from the prefetched rows.

    `recipe.ingredients.select_related("food")` looked like an optimisation and was the
    opposite: adding `select_related` to an already-prefetched manager discards the
    prefetch and issues a fresh query per recipe, per meal. Measured at 8 extra queries
    for a single lunch against a 16-recipe library, growing linearly with it.
    """
    return [(line.food, float(line.grams or 0), bool(line.scalable))
            for line in recipe.ingredients.all()]


def _portion_recipe(lines: Sequence[Tuple[object, float, bool]],
                    servings: float, ladders: Dict[int, List[float]]) -> Components:
    """Scale a recipe, holding the parts that should not scale, and land on the ladder.

    Doubling a dish doubles the rice; it does not double the pinch of salt or the
    teaspoon of oil that makes it work. `scalable=False` lines stay put.

    Every amount then snaps to a portion of that food a person would serve — a whole
    number of eggs, half a cup of oats. Scaling by calories alone is how 210 g of oats
    reached a plate from a recipe that asked for 60: the macros were inside tolerance
    the whole way, because tolerance is about the total and says nothing about any one
    ingredient. Snapping also removes a bias, because the old rounding step could only
    push an amount up, so a scaled recipe missed high and never low.
    """
    out: Components = []
    for food, grams, scalable in lines:
        wanted = grams * servings if scalable else grams
        rungs = ladders[id(food)]
        out.append((food, min(rungs, key=lambda g: abs(g - wanted))))
    return out


#: Servings are searched, not computed. Dividing the target by the recipe's own energy
#: gives the scale that would be right if portions were continuous; once every line
#: snaps to a rung, the arithmetic answer is frequently not the best one available.
SERVINGS_LO, SERVINGS_HI, SERVINGS_STEP = 0.5, 2.0, 0.05




#: How hard a dish served inside the no-repeat window is pushed down. A penalty, not a
#: ban: with sixteen recipes across four meals, excluding outright exhausts the library
#: within a week and the planner falls back to assembling components, which trades a
#: repeated dish for a pile of ingredients. That is the wrong trade. Once the library
#: grows the penalty alone is enough to stop repeats.
RECENCY_PENALTY = 0.05


def chosen_food_ids(user, meal_name: str) -> frozenset:
    """The foods this client picked for this meal, across every macro slot.

    `UserFoodCategoryPreference` is the model behind "choose your breakfast, lunch and
    dinner items". It was already captured, already ranked first by `build_pool`, and
    never consulted here, because `find_recipe` had no user parameter — so a client who
    filled it in received the same plan as one who ignored it.
    """
    from diet.models import UserFoodCategoryPreference

    if user is None or not getattr(user, "pk", None):
        return frozenset()
    return frozenset(
        UserFoodCategoryPreference.objects
        .filter(user=user, meal=meal_name)
        .values_list("food_id", flat=True)
    )



def recipe_pool_score(lines: Sequence[Tuple[object, float, bool]], pool, meal_name: str,
                      edges: Optional[Dict[int, "collections.Counter"]] = None) -> float:
    """A recipe scored exactly as a built plate is. See `templates.meal_pool_score`."""
    from .templates import meal_pool_score

    return meal_pool_score([(f, g) for f, g, _s in lines], pool, meal_name, edges)


def softmax_weights(scores: Sequence[float], temperature: float) -> List[float]:
    """The template path's sampling rule, exposed so the recipe path can use the same one."""
    import math
    if not scores:
        return []
    top = max(scores)
    return [max(math.exp((v - top) / temperature), 1e-9) for v in scores]


#: Which slot fills which shortfall, by meal. A short carbohydrate at breakfast or a
#: snack is fruit; at a main meal it is a starch. Protein and fat are what they are.
_FILL_SLOT = {
    ("Breakfast", "carb"): "fruit", ("Snack", "carb"): "fruit",
    ("Lunch", "carb"): "carb", ("Dinner", "carb"): "carb",
}


#: Whether to enumerate every portion of every food jointly when the space is under
#: `solve`'s cap. OFF. It was measured on: the optimiser gate rose to 305 of 306 at the
#: proven optimum, and in the same run chosen-ingredient share fell 0.229 to 0.210 and
#: calorie drift rose 4.0% to 5.3%, while the pair search moved all four product metrics
#: the right way. The exhaustive optimum is optimal for a blended objective that weights
#: calories at 0.5, so it sits further from the calorie target than the pair search
#: lands. That is a question about the objective, which the plan holds open, not about
#: the search. Flip this after the CALORIE_WEIGHT decision, and re-measure.
def _best_portioning(components, targets, policy, movable):
    """Search a dish's portions. A non-scalable line pins the search to the lines the
    recipe's author allows to move, which `refine` honours.

    There was an alternative branch here behind `EXHAUSTIVE_PORTIONING = False` — a
    constant, never set anywhere, so the branch had been unreachable since it was
    written. `refine` now does single, pair and three-at-once moves and is measured
    against the exhaustive optimum by `tests/optimiser_benchmark.py`, which is the
    honest version of what that branch was reaching for.
    """
    return refine(components, targets, policy.tolerance, movable)


def _augment(match: RecipeMatch, lines, targets, policy, pool, meal_name, edges, rng, recent):
    """One food from the client's pool, chosen for the largest macro shortfall, portioned
    jointly with the dish. Returns None when the pool has nothing to add."""
    from .templates import _pick

    dev = match.deviation
    shortfalls = {m: -getattr(dev, m) for m in ("protein", "carb", "fat") if getattr(dev, m) < 0}
    if not shortfalls:
        return None
    macro = max(shortfalls, key=shortfalls.get)
    slot = _FILL_SLOT.get((meal_name, macro), macro)
    already = {getattr(f, "id", None) for f, _g, _s in lines}
    candidates = [f for f in pool.get(meal_name, slot) if f.id not in already]
    if not candidates:
        return None
    food = _pick(candidates, [f.id for f, _g, _s in lines], edges or {}, set(recent), rng,
                 pool.weights(meal_name, slot))
    if food is None:
        return None
    combined = list(match.components) + [(food, 0.0)]
    movable = [i for i, (_f, _g, scalable) in enumerate(lines) if scalable] + [len(lines)]
    tuned, tuned_dev = _best_portioning(combined, targets, policy, movable)
    added = next((g for f, g in tuned if f is food), 0.0)
    if added <= 0:
        return None
    return RecipeMatch(match.recipe, tuned, match.servings, tuned_dev, extras=(food,),
                       substituted=match.substituted, replaced=match.replaced)


#: Words that describe a food rather than name it. They appear in catalogue names as
#: qualifiers ("White Rice", "Greek Yogurt (Non-Fat)") and in dish titles as ordinary
#: English, so matching on them protects lines the dish is not actually named after.
_QUALIFIER_WORDS = frozenset({
    "with", "and", "the", "for", "raw", "dry", "low", "non", "fat", "free", "lean",
    "whole", "half", "white", "red", "green", "brown", "black", "fresh", "plain",
    "cooked", "baked", "grilled", "roasted", "boiled", "canned", "prepared", "dried",
    "ground", "sweet", "medium", "large", "small", "extra", "virgin", "light",
    "style", "mix", "mixed", "natural", "salted", "unsalted",
})


def _names_the_dish(food_name: str, title: str) -> bool:
    """Whether the dish is named after this food, by whole words."""
    import re

    def stem(word: str) -> str:
        """Enough of a word to match its plural, and no more."""
        for suffix in ("ies", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                return word[: -len(suffix)]
        return word

    # Words long enough to identify a food. `any()` over every word of three letters
    # or more blocked far too much — "oat" protected any title containing it — and
    # cost the client's chosen foods a sixth of the dishes they used to reach.
    # A word identifies a food unless it is a QUALIFIER. Length was the wrong test in
    # both directions: it dropped "egg" and "oat", which name dishes, and it kept
    # "white" and "lean", which do not. "Egg" is three letters and "Ful with Eggs and
    # Bread" is named after it.
    words = [w for w in re.findall(r"[a-z]+", food_name.lower())
             if len(w) >= 3 and w not in _QUALIFIER_WORDS]
    if not words:
        return False
    # ANY significant word, stemmed. `all()` over exact words failed twice over: the
    # dish "Lentil Soup" did not match the food "Lentils" because of one letter, and
    # "White Rice" did not match "Rice, Lentils and Chickpeas" because "white" is
    # absent — so both lines were substitutable and a lentil soup could be served with
    # chickpeas instead of lentils. Matching any stem over-blocks slightly and never
    # under-blocks, which is the right direction for a rule about a dish's identity.
    return any(re.search(rf"\b{re.escape(stem(w))}(?:s|es|ies)?\b", title) for w in words)


def _substitute_chosen(lines, pool, meal_name, policy, index, chosen_ids, recipe_name=""):
    """Put a food the client chose for this meal into the dish, in place of the line
    it most resembles, when that line is a substitute for it.

    Preference used to reach a named dish only if a recipe author had written that
    exact food in. With 16 recipes over 133 foods, a client who chose bulgur for lunch
    was served rice in every rice dish and bulgur in none. Similarity in the same slot decides. One substitution per dish, of the strongest choice, so the dish stays the
    dish.
    """
    from .candidates import classify_food

    if not chosen_ids or index is None:
        return lines, None, None
    # A food the client once chose and has since disliked, or that their allergens or
    # cuisine forbid, must not be swapped in: the dish was then rejected wholesale by
    # the constraint check below and the library silently shrank.
    constraints = getattr(pool, 'constraints', None)
    if constraints is not None and getattr(constraints, 'active', False):
        chosen_ids = frozenset(
            cid for cid in chosen_ids
            if not constraints.forbids(index.foods.get(cid))
        ) if hasattr(constraints, 'forbids') else chosen_ids
    present = {getattr(f, "id", None) for f, _g, _s in lines}
    title = (recipe_name or "").lower()
    best = None  # (similarity, line_index, chosen_food)
    for i, (food, grams, scalable) in enumerate(lines):
        if not scalable:
            continue
        # Never the ingredient the dish is named after. "Lentil Soup with Greens
        # (Chickpeas instead of Lentils)" is a lentil soup with no lentils in it, and
        # that is not the client's choice reaching a dish — it is a different dish
        # wearing its name.
        # Whole words, not a substring: "Egg" matched "Eggplant Casserole" and "Oat"
        # matched "Goat Cheese Salad", so those lines became permanently
        # unsubstitutable and a client's chosen food could not reach dishes it fits.
        if title and _names_the_dish(plain_name(food), title):
            continue
        slot = classify_food(food)
        # Sorted: a frozenset iterates in id-hash order, and an exact tie in
        # similarity then resolved differently across database rebuilds.
        for cid in sorted(chosen_ids):
            if cid in present:
                continue
            chosen = index.foods.get(cid)
            if chosen is None or classify_food(chosen) != slot:
                continue
            # Two qualifiers of one food are the same food to a reader: swapping
            # "Pita Bread (Whole Wheat)" for "Pita Bread (White)" announces a change
            # nobody asked for and nobody can see.
            if plain_name(chosen).lower() == plain_name(food).lower():
                continue
            sim = index.similarity(cid, food.id)
            if sim >= policy.similarity_threshold:
                if best is None or sim > best[0]:
                    best = (sim, i, chosen)
    if best is None:
        return lines, None, None
    _sim, i, chosen = best
    out = list(lines)
    food, grams, scalable = out[i]
    # Same energy, so the dish's macro shape survives the swap.
    k_old = float(getattr(food, "calories", 0) or 0)
    k_new = float(getattr(chosen, "calories", 0) or 0)
    grams_new = grams * (k_old / k_new) if k_old > 0 and k_new > 0 else grams
    out[i] = (chosen, grams_new, scalable)
    return out, chosen, food


def find_recipe(meal_name: str, targets: Dict[str, float], policy: PlannerPolicy,
                allergen_checker=None, constraints=None,
                exclude_ids: Sequence[int] = (),
                recipes: Optional[Sequence] = None, user=None, rng=None,
                recent_ids: Sequence[int] = (),
                ladders: Optional[Dict[int, List[float]]] = None,
                pool=None, edges: Optional[Dict[int, "collections.Counter"]] = None,
                index=None, chosen_ids: Optional[frozenset] = None,
                served_counts: Optional[Dict[int, int]] = None,
                max_servings: Optional[int] = None) -> Optional[RecipeMatch]:
    """A dish for this meal's macro target, or None if nothing fits.

    Every serving size between half and double is tried, each one snapped to the
    amounts the foods declare, and the closest is kept. That is the whole of the fit
    calculation now: the dish is scaled and portioned in one search rather than scaled
    by arithmetic and then repaired by a separate optimiser that knew nothing about
    what a serving is.

    Two things decide which dish, in this order. Fit is a filter: anything outside
    `policy.tolerance` cannot be served whatever else is true of it. Among the dishes
    that pass, the choice is weighted by how much of each one the client actually
    picked for this meal, and sampled rather than maximised.

    Sampling matters as much as the preference term. Returning the single best fit is
    deterministic, so the same client and the same target produced the same dish every
    day: five recipes out of sixteen were ever served and one appeared 48 times in a
    week of measurements. `rng` is the planner's per-user, per-day seeded generator, so
    the result is varied but reproducible.
    """
    from diet.models import Recipe

    from .constraints import ClientConstraints
    from .portion import portions_for

    if constraints is None:
        constraints = (ClientConstraints(allergen_checker=allergen_checker)
                       if allergen_checker is not None else ClientConstraints.for_user(user))

    if recipes is None:
        recipes = list(
            Recipe.objects.filter(is_active=True)
            .exclude(id__in=list(exclude_ids))
            .prefetch_related("ingredients__food__category")
        )

    # Exclusions apply to the list we were GIVEN as well as the one we would build.
    # They were applied only inside the query above, so the moment the planner began
    # passing a cached library, same-day exclusion silently stopped: a dish was served
    # twice in one day on 23 of 42 measured days, recipes fit every slot, and the
    # template path never got a turn.
    if exclude_ids:
        banned = set(exclude_ids)
        recipes = [r for r in recipes if getattr(r, "id", None) not in banned]

    target_kcal = float(targets.get("calories", 0) or 0)
    if target_kcal <= 0 or not recipes:
        return None

    recent = set(recent_ids or ())
    # Read once. This was a query inside the recipe loop — one per recipe, per meal,
    # per day: 76 x 28 rows of the same answer for a week-long plan.
    if chosen_ids is None:
        chosen_ids = chosen_food_ids(user, meal_name) if (index is not None and pool is not None) else frozenset()
    steps = int(round((SERVINGS_HI - SERVINGS_LO) / SERVINGS_STEP)) + 1
    augment_reach = 0.0
    if pool is not None:
        if ladders is None:
            ladders = {}
        for foods in pool.by_slot.get(meal_name, {}).values():
            for f in foods:
                key = getattr(f, "id", None) or id(f)
                if key not in ladders:
                    ladders[key] = [p.grams for p in portions_for(f)]
                augment_reach = max(augment_reach,
                                    max(ladders[key]) * float(getattr(f, "calories", 0) or 0) / 100.0)
    within: List[Tuple[RecipeMatch, float]] = []
    best: Optional[RecipeMatch] = None
    for recipe in recipes:
        suits = getattr(recipe, "meal_types", None) or []
        if suits and meal_name not in suits:
            continue

        lines = _recipe_lines(recipe)
        if not lines:
            continue
        substituted = replaced = None
        if index is not None and pool is not None:
            lines, substituted, replaced = _substitute_chosen(
                lines, pool, meal_name, policy, index, chosen_ids,
                recipe_name=(getattr(recipe, "name_en", None)
                             or getattr(recipe, "name", "") or ""))

        # Every hard constraint, not just allergens. This checked allergies and nothing
        # else, so a dish built on a food the client had explicitly rejected was served
        # to them — and persistence then refused the finished plan outright rather than
        # producing one without it, so the client received an error instead of dinner.
        if constraints is not None and constraints.active:
            if constraints.forbids_any(food for food, _g, _s in lines):
                continue
            if not constraints.cuisine.allows(getattr(recipe, "cuisine", None)):
                continue

        # Ladders are a pure function of the food; cached per generation when the
        # caller provides a dict, recomputed otherwise.
        if ladders is None:
            ladders = {}
        line_ladders = {}
        for food, _g, _s in lines:
            key = getattr(food, "id", None) or id(food)
            if key not in ladders:
                ladders[key] = [p.grams for p in portions_for(food)]
            line_ladders[id(food)] = ladders[key]

        # A dish that cannot REACH the target is not a candidate, and the servings search
        # need not run to find that out. The bound is the dish's real reach — every line
        # at its ladder floor, and every line at its ladder ceiling — which is exactly the
        # space the search and refinement can produce, so it can never reject a fit. A
        # slack multiplier on the base serving could: refinement moves single lines to
        # their ceilings and took Mujadara from a 450 kcal base to a 1,120 kcal lunch,
        # which a 2.5x window discarded and the day landed 6.9% under. At a thousand
        # recipes this is the difference between scanning a thousand and scanning tens.
        reach_lo = sum(min(line_ladders[id(f)]) * float(getattr(f, "calories", 0) or 0) / 100.0
                       for f, _g, _s in lines)
        reach_hi = sum(max(line_ladders[id(f)]) * float(getattr(f, "calories", 0) or 0) / 100.0
                       for f, _g, _s in lines)
        tol = float(policy.tolerance.get("calories", 0.10))
        # The upper bound includes what augmentation can add: one food from the pool
        # at its ladder ceiling. Without it the filter discarded exactly the dishes
        # augmentation exists for, and the library collapsed onto the largest ones.
        # The lower bound allows the negligible floor, so a 230 kcal snack against a
        # 200 kcal target is not skipped when `within` would have accepted it.
        hi_bound = reach_hi * (1.0 + tol) + (augment_reach if pool is not None else 0.0)
        lo_bound = reach_lo * (1.0 - tol) - NEGLIGIBLE.get("calories", 0.0)
        if reach_hi <= 0 or not (lo_bound <= target_kcal <= hi_bound):
            continue

        # Prefer a serving that is inside tolerance over the one with the smallest
        # magnitude. They are not the same point and the difference decides whether the
        # dish can be served at all: magnitude averages the macros, `within` requires
        # every one of them to be inside its own bound, so the lowest-magnitude serving
        # is regularly one that fails on a single macro while a neighbouring serving
        # passes on all four.
        match: Optional[RecipeMatch] = None
        closest: Optional[RecipeMatch] = None
        # `step`, not `index`: that name is the similarity index parameter, and the
        # loop overwrote it with an int. From the second recipe on, every substitution
        # call raised, the planner's fail-open turned the exception into "no recipe",
        # and the recipe path was dead for the whole of P12's measurement while the
        # gate stayed green on templates alone.
        for step in range(steps):
            servings = SERVINGS_LO + step * SERVINGS_STEP
            components = _portion_recipe(lines, servings, line_ladders)
            dev = deviation_of(totals_of(components), targets)
            candidate = RecipeMatch(recipe, components, servings, dev,
                                    substituted=substituted, replaced=replaced)
            if closest is None or dev.magnitude < closest.deviation.magnitude:
                closest = candidate
            if dev.within(policy.tolerance):
                if match is None or dev.magnitude < match.deviation.magnitude:
                    match = candidate
        match = match or closest
        if match is None:
            continue

        # A dish whose macro ratio does not match the target cannot be fixed by serving
        # more of all of it, so after the best serving is chosen the scalable lines are
        # adjusted individually — on their own ladders, so the dish stays servable.
        #
        # Unconditionally. This used to run only when the serving was OUTSIDE tolerance,
        # which is an early exit on first-acceptable rather than best-available: a dish
        # that scraped inside the band was served as it landed while a better portioning
        # of the same dish sat one move away. Against exhaustive search that accounted
        # for most of the meals the engine served that it could have beaten.
        movable = [i for i, (_f, _g, scalable) in enumerate(lines) if scalable]
        # A dish that cannot reach the target on its own is not refined on its own:
        # augmentation below re-solves every line jointly with the added food, and
        # refining first was a second pair search per dish that the widened reach
        # filter multiplied by forty.
        alone_can_reach = reach_hi * (1.0 + tol) >= target_kcal
        if movable and alone_can_reach:
            tuned, dev = _best_portioning(match.components, targets, policy, movable)
            if _better(dev, match.deviation, policy.tolerance):
                match = RecipeMatch(recipe, tuned, match.servings, dev,
                                    substituted=match.substituted, replaced=match.replaced)

        # Augment before rejecting. A dish under its slot by more than tolerance used
        # to be discarded: overnight oats is 500 kcal, an 870 kcal breakfast wanted
        # more, and the library collapsed onto the few dishes that happened to be big
        # enough. Fill the largest shortfall with ONE food from the client's own pool,
        # then re-solve every portion jointly. The dish keeps its name and says what
        # came with it. This is the root fix for the concentration onto seven dishes,
        # and it multiplies the reach of every recipe in the library.
        if (pool is not None and not match.deviation.within(policy.tolerance)
                and match.deviation.calories < -float(policy.tolerance.get("calories", 0.10))):
            augmented = _augment(match, lines, targets, policy, pool, meal_name, edges, rng, recent)
            if augmented is not None and _better(augmented.deviation, match.deviation, policy.tolerance):
                match = augmented

        if best is None or match.deviation.magnitude < best.deviation.magnitude:
            best = match

        if match.deviation.within(policy.tolerance):
            # Fit is a FILTER — inside tolerance or not — never a weight. Among the
            # dishes that fit, the dish is scored on the pool's own scale, the same
            # scale the template path samples on: structure by calorie share,
            # preference by presence and role, pairing bounded. One scorer for both
            # paths, so "which path served this meal" can be a judged decision rather
            # than an accident of ordering. The two paths used to weight preference
            # at 1.7:1 and 148:1 respectively.
            #
            # One scorer, two INPUTS, and deliberately so. Here it reads the dish as
            # WRITTEN — its base grams, no augmentation — because augmentation happens
            # after a dish is chosen and cannot be known while choosing between them.
            # The judge in `rule_based_planner._plan_meal` reads the plate as SERVED,
            # augmented food included, because by then it exists. A dish is therefore
            # sampled slightly under the value it will have once the client's own food
            # is added to it, which biases the choice AWAY from dishes that need
            # augmenting — the conservative direction, and the only one available
            # without scoring a plate that does not exist yet.
            score = (recipe_pool_score(lines, pool, meal_name, edges) if pool is not None
                     else -float(match.deviation.magnitude))
            within.append((match, score, recipe))

    if within:
        # A dish already served `max_servings` times this week is not sampled while
        # any other fitting dish exists. The multiplicative penalty (about 60 points
        # a serving) could not outweigh a 100-point chosen-food bonus, so a chooser's
        # favourite dish came back a fourth time in a week.
        if max_servings is not None and served_counts:
            fresh = [t for t in within if int(served_counts.get(t[2].id, 0)) < max_servings]
            if fresh:
                within = fresh
        if rng is None or len(within) == 1:
            return max(within, key=lambda triple: triple[1])[0]
        matches = [m for m, _s, _r in within]
        weights = softmax_weights([s for _m, s, _r in within], SOFTMAX_T)
        for i, (_m, _s, r) in enumerate(within):
            if constraints is not None:
                # A dish from the cuisine the client asked for less of is still
                # servable at a mixed ratio; it is simply chosen less often.
                weights[i] *= max(0.1, constraints.cuisine.weight(getattr(r, "cuisine", None)))
            # Variety is an objective over the week: each earlier serving of this dish
            # in the window multiplies its weight by the penalty again. The 3-day
            # flag stays as the floor for callers that pass only that.
            n = int((served_counts or {}).get(r.id, 0))
            if n == 0 and r.id in recent:
                n = 1
            if n:
                weights[i] *= RECENCY_PENALTY ** n
        return rng.choices(matches, weights=weights, k=1)[0]

    if best is not None:
        logger.debug("Best recipe for %s is outside tolerance (%s); caller may fall back",
                     meal_name, best.deviation.human())
    return best
