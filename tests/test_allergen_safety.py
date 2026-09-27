"""Spec B4, second half: "until allergen tagging is corrected, exclude any dish with
incomplete tags from plans for users who declared an allergy".

What "incomplete" means is defined once, in `diet/allergens.py`
(`food_tagging_gaps`, `dish_tagging_gaps`), and enforced once, in
`diet/planner/constraints.py ClientConstraints` — the object the pool builder, the recipe
path, the template path, the substitution step and the swap offers all consult. What
"declared an allergy" means is `declares_allergy`: anything the client wrote in
`UserFoodPreference.allergies` that names an allergy, including text the English
vocabulary cannot read.

The injected rows below are each something cf17d77 served to an allergic client:

* a VERIFIED food whose own name implies an allergen its stored tags lack
  (`Crispy ...` implies gluten), to a client allergic to something else — the curation
  demonstrably missed one allergen, so nothing vouches for the rest;
* a VERIFIED food whose ingredient text names an allergen its tags lack;
* a VERIFIED food whose stored tags are not the vocabulary (`["nuts"]`, or a bare string)
  — `food_allergen_tags` drops them, so the row read as carrying nothing;
* a dish whose name says what its ingredient lines do not (`... Satay`: peanut sauce that
  no line carries), served to a PEANUT-allergic client because the recipe path checked
  the lines and never the dish.

Every test fails on cf17d77: the predicates did not exist, and the planner served each
of those rows.
"""
import random

import pytest

pytestmark = pytest.mark.django_db

TOKEN = "B4q"


# ----------------------------------------------------------------------- the world
def _food(name, *, kcal, protein, carbs, fat, allergens=(), source="verified",
          ingredients=""):
    from diet.models import FoodItem

    return FoodItem.objects.create(
        name=name, name_en=name, name_ar=f"{name} ar", calories=kcal, protein=protein,
        carbs=carbs, fat=fat, serving_size="100g", serving_size_grams=100,
        allergens=allergens if not isinstance(allergens, tuple) else list(allergens),
        allergen_source=source, ingredients_text=ingredients)


@pytest.fixture
def under_tagged(seeded_catalogue, db):
    """Four VERIFIED rows whose tagging is incomplete, one per way it can be."""
    return {
        # "crispy" is a gluten marker; the curator stored no tags at all.
        "implied": _food(f"{TOKEN} Crispy Chickpea Bites", kcal=380, protein=15,
                         carbs=50, fat=13),
        # The ingredient list names peanuts; the tags do not.
        "ingredients": _food(f"{TOKEN} Puffed Rice Squares", kcal=400, protein=8,
                             carbs=80, fat=5, ingredients="puffed rice, peanuts, honey"),
        # Free text is not the vocabulary: dropped on read, so it claimed nothing.
        "free_text": _food(f"{TOKEN} Harvest Seed Bar", kcal=450, protein=12, carbs=45,
                           fat=25, allergens=["nuts"]),
        # A bare string, not a list: iterated as characters, every one of them dropped.
        "not_a_list": _food(f"{TOKEN} Toasted Corn Chips", kcal=500, protein=7,
                            carbs=60, fat=26, allergens="soy"),
    }


def _client(username, allergies=None):
    from diet.models import UserFoodPreference
    from tests.diet_quality import _make_client

    user = _make_client(username, ["Maintain"], 175, 78, 28, "Male", "Moderate")
    # A new client already has a preference row (`users/signals.py`). None stands for a
    # client who has none at all.
    if allergies is None:
        UserFoodPreference.objects.filter(user=user).delete()
    else:
        UserFoodPreference.objects.update_or_create(
            user=user, defaults={"allergies": allergies})
    return user


def _pool_ids(user, constraints=None):
    from diet.planner.candidates import build_pool
    from diet.planner.constraints import ClientConstraints
    from diet.planner.policy import load_policy

    if constraints is None:
        constraints = ClientConstraints.for_user(user)
    pool = build_pool(user, load_policy("maintain"), constraints=constraints)
    return {meal: {macro: [f.id for f in foods] for macro, foods in macros.items()}
            for meal, macros in pool.by_slot.items()}


def _flat(pool_ids):
    return {fid for macros in pool_ids.values() for ids in macros.values() for fid in ids}


def _recipe(name, lines, meal_types=("Breakfast", "Lunch", "Dinner", "Snack"),
            description="", instructions="", notes=None):
    from diet.models import Recipe, RecipeIngredient

    recipe = Recipe.objects.create(
        name=name, name_en=name, name_ar=f"{name} ar", description=description,
        description_en=description, instructions=instructions,
        meal_types=list(meal_types), cuisine="Levantine")
    for i, (food, grams) in enumerate(lines):
        RecipeIngredient.objects.create(recipe=recipe, food=food, grams=grams,
                                        note=(notes or {}).get(i, ""))
    return recipe


def _catalogue_food(name):
    from diet.models import FoodItem

    return FoodItem.objects.get(name_en=name)


# ------------------------------------------------------------- what incomplete means
def test_a_food_says_every_way_its_tagging_is_incomplete(under_tagged):
    """The definition, one reason per way. A verified row whose tags are the vocabulary
    and cover everything its own name and ingredient list imply is complete; nothing
    else is."""
    from diet.allergens import food_tagging_gaps

    assert food_tagging_gaps(under_tagged["implied"]) == ["implied_not_tagged"]
    assert food_tagging_gaps(under_tagged["ingredients"]) == ["implied_not_tagged"]
    assert food_tagging_gaps(under_tagged["free_text"]) == ["malformed_tags"]
    assert food_tagging_gaps(under_tagged["not_a_list"]) == ["malformed_tags"]

    inferred = _food(f"{TOKEN} Mystery Stew", kcal=120, protein=10, carbs=10, fat=4,
                     source="inferred")
    unknown = _food(f"{TOKEN} Mystery Plate", kcal=120, protein=10, carbs=10, fat=4,
                    source="unknown")
    assert food_tagging_gaps(inferred) == ["unverified_source"]
    assert food_tagging_gaps(unknown) == ["unverified_source"]

    # Complete: verified, the vocabulary, and nothing implied that is not stored.
    assert food_tagging_gaps(_catalogue_food("Chicken Breast (Grilled)")) == []
    assert food_tagging_gaps(_catalogue_food("Feta Cheese")) == []
    tagged = _food(f"{TOKEN} Crispy Flatbread", kcal=300, protein=9, carbs=55, fat=5,
                   allergens=["gluten"])
    assert food_tagging_gaps(tagged) == []


def test_a_dish_is_incomplete_when_its_own_text_says_more_than_its_lines(
        seeded_catalogue):
    """A dish is its lines. When its name, description, instructions or a line's note
    implies an allergen that no line carries, the recipe was written down without it —
    `Chicken Satay` with no peanut sauce on the list — and when any line is itself
    incomplete, so is the dish."""
    from diet.allergens import dish_tagging_gaps

    chicken = _catalogue_food("Chicken Breast (Grilled)")
    rice = _catalogue_food("White Rice (Cooked)")
    cucumber = _catalogue_food("Cucumber")
    peanut_butter = _catalogue_food("Peanut Butter (Natural)")

    plain = _recipe(f"{TOKEN} Chicken and Rice Plate", [(chicken, 150), (rice, 180)])
    assert dish_tagging_gaps(plain) == []

    satay = _recipe(f"{TOKEN} Chicken Satay Bowl",
                    [(chicken, 150), (rice, 180), (cucumber, 80)])
    assert dish_tagging_gaps(satay) == ["implied_not_tagged"]

    # The peanut is on the list: the name is covered.
    honest = _recipe(f"{TOKEN} Chicken Satay with Peanut",
                     [(chicken, 150), (rice, 180), (peanut_butter, 20)])
    assert dish_tagging_gaps(honest) == []

    told = _recipe(f"{TOKEN} Chicken Rice Plate B", [(chicken, 150), (rice, 180)],
                   instructions="Drizzle with tahini before serving.")
    assert dish_tagging_gaps(told) == ["implied_not_tagged"]
    described = _recipe(f"{TOKEN} Chicken Rice Plate C", [(chicken, 150), (rice, 180)],
                        description="Finished with a knob of butter")
    assert dish_tagging_gaps(described) == ["implied_not_tagged"]
    noted = _recipe(f"{TOKEN} Chicken Rice Plate D", [(chicken, 150), (rice, 180)],
                    notes={1: "cooked in ghee"})
    assert dish_tagging_gaps(noted) == ["implied_not_tagged"]

    bites = _food(f"{TOKEN} Crispy Chicken Bites", kcal=250, protein=20, carbs=15,
                  fat=12)
    with_bad_line = _recipe(f"{TOKEN} Bites and Rice", [(bites, 120), (rice, 150)])
    assert dish_tagging_gaps(with_bad_line) == ["ingredient_incomplete"]

    empty = _recipe(f"{TOKEN} Nothing At All", [])
    assert dish_tagging_gaps(empty) == ["no_ingredients"]


def test_the_shipped_catalogue_is_completely_tagged(seeded_catalogue):
    """The rule costs the catalogue a fresh install gets nothing: every servable food and
    every active dish is completely tagged. This is what the before/after measurement in
    `diet/planner/constraints.py` rests on; if it stops holding, the trade-off changed
    and has to be measured again."""
    from diet.allergens import dish_tagging_gaps, food_tagging_gaps
    from diet.models import FoodItem, Recipe

    foods = {f.name_en: food_tagging_gaps(f)
             for f in FoodItem.objects.filter(needs_review=False)}
    assert foods and not {n: g for n, g in foods.items() if g}
    dishes = {r.name_en: dish_tagging_gaps(r)
              for r in Recipe.objects.filter(is_active=True)
              .prefetch_related("ingredients__food")}
    assert dishes and not {n: g for n, g in dishes.items() if g}


@pytest.mark.parametrize("raw, declared", [
    ("peanut", True),
    ("nuts, dairy", True),
    ("فول سوداني", True),          # Arabic: the vocabulary cannot read it; still declared
    ("حساسية من الحليب", True),
    ("", False),
    ("   ", False),
    (None, False),
    ("no allergies", False),
])
def test_what_counts_as_declaring_an_allergy(raw, declared):
    from diet.allergens import declares_allergy

    assert declares_allergy(raw) is declared


# ------------------------------------------------------------------ the pool, food
@pytest.mark.parametrize("allergies", ["milk", "sesame", "فول سوداني"])
def test_an_allergic_clients_pool_holds_no_incompletely_tagged_food(
        under_tagged, allergies):
    """The pool builder is where a food reaches a plate, the template path and the swap
    offers draw from it. For a client who declared ANY allergy, none of the four is in
    it — whichever allergy, because a row whose tagging is incomplete cannot be cleared
    for anything. The Arabic declaration is the case the vocabulary cannot read at all:
    cf17d77 treated that client as having no allergy and served everything."""
    from diet.allergens import food_tagging_gaps
    from diet.models import FoodItem

    user = _client(f"b4a{len(allergies)}", allergies)
    served = _flat(_pool_ids(user))
    assert served, "the pool is empty; nothing was measured"
    leaked = {k: f.name_en for k, f in under_tagged.items() if f.id in served}
    assert not leaked, f"incompletely tagged food in an allergic client's pool: {leaked}"
    incomplete = [f.name_en for f in FoodItem.objects.filter(id__in=served)
                  if food_tagging_gaps(f)]
    assert not incomplete


def test_the_swap_path_refuses_what_the_pool_refuses(under_tagged):
    """`swap_food` filters its offers and its target through the same object, so an
    allergic client is neither offered nor given an incompletely tagged food; its
    `food_not_allowed` sentence is the one it already used for an unverified row."""
    from diet.planner.constraints import ClientConstraints

    limits = ClientConstraints.for_user(_client("b4swap", "sesame"))
    for food in under_tagged.values():
        assert limits.forbids(food)
        assert limits.reason(food) == "we cannot confirm what this contains"
    assert not limits.forbids(_catalogue_food("Chicken Breast (Grilled)"))


@pytest.mark.parametrize("allergies", [None, "", "no allergies"])
def test_a_non_allergic_clients_pool_is_unchanged(under_tagged, allergies):
    """No declared allergy, no change: the incompletely tagged rows stay in the pool, and
    the pool is exactly the one built with no constraints at all."""
    from diet.allergens import food_tagging_gaps
    from diet.planner.constraints import ClientConstraints

    # The premise: these rows ARE incomplete, so their presence below means something.
    assert all(food_tagging_gaps(f) for f in under_tagged.values())

    user = _client(f"b4n{len(allergies or '')}{allergies is None}", allergies)
    pool = _pool_ids(user)
    assert {f.id for f in under_tagged.values()} <= _flat(pool)
    assert pool == _pool_ids(user, constraints=ClientConstraints())


# ------------------------------------------------------------------ the dish path
def test_find_recipe_never_offers_an_allergic_client_an_under_tagged_dish(
        seeded_catalogue):
    """The recipe path checked a dish's LINES and never the dish. `Chicken Satay Bowl`
    whose peanut sauce is on no line was served to a peanut-allergic client."""
    from diet.planner.constraints import ClientConstraints
    from diet.planner.policy import load_policy
    from diet.planner.recipes import find_recipe

    satay = _recipe(f"{TOKEN} Chicken Satay Bowl", [
        (_catalogue_food("Chicken Breast (Grilled)"), 150),
        (_catalogue_food("White Rice (Cooked)"), 180),
        (_catalogue_food("Cucumber"), 80)])
    satay = type(satay).objects.prefetch_related("ingredients__food__category").get(
        pk=satay.pk)
    n = satay.nutrition()
    targets = {"calories": n["calories"], "protein": n["protein"], "carb": n["carbs"],
               "fat": n["fat"]}

    def offer(user):
        return find_recipe("Lunch", targets, load_policy("maintain"),
                           constraints=ClientConstraints.for_user(user),
                           recipes=[satay], rng=random.Random(7))

    plain = offer(_client("b4dish0"))
    assert plain is not None and plain.recipe.pk == satay.pk
    assert offer(_client("b4dish1", "peanut")) is None
    assert offer(_client("b4dish2", "sesame")) is None


def _under_tagged_twins():
    """A copy of every active dish whose name adds what no line carries.

    The library as the brief fears it is: the same food, under-tagged. Half of what the
    planner may choose from is then a dish whose tagging is incomplete, so a plan that
    serves none of them is a filter, not luck — and the non-allergic control proves the
    twins are chosen when nothing forbids them.
    """
    from diet.models import Recipe, RecipeIngredient

    twins = []
    for recipe in list(Recipe.objects.filter(is_active=True)
                       .prefetch_related("ingredients")):
        name = f"{recipe.name_en} with Satay Sauce"
        twin = Recipe.objects.create(
            name=name, name_en=name, name_ar=f"{recipe.name_ar or recipe.name_en} ساتاي",
            description=recipe.description, description_en=recipe.description_en,
            meal_types=list(recipe.meal_types or []), cuisine=recipe.cuisine,
            prep_minutes=recipe.prep_minutes)
        RecipeIngredient.objects.bulk_create([
            RecipeIngredient(recipe=twin, food_id=line.food_id, grams=line.grams,
                             scalable=line.scalable, swap_group=line.swap_group)
            for line in recipe.ingredients.all()])
        twins.append(twin.pk)
    return set(twins)


def test_an_allergic_clients_generated_plan_serves_no_incompletely_tagged_dish(
        seeded_catalogue):
    """End to end, persisted, as the client receives it: a peanut-allergic client's plan
    holds no under-tagged dish and no incompletely tagged food, while a client with no
    allergy, same profile, same seed, is served the twins."""
    from diet.allergens import dish_tagging_gaps, food_tagging_gaps
    from tests.diet_quality import _generate, _meals_of

    twins = _under_tagged_twins()
    assert twins

    control = _client("b4plan0")
    control_plan = _generate(control, control.calculate_daily_calories(), 2, "b4:plan")
    assert control_plan is not None
    served_twins = [m.name for m in _meals_of(control_plan) if m.recipe_id in twins]
    assert served_twins, "the twins were never chosen; the allergic check below is empty"

    allergic = _client("b4plan1", "peanut")
    plan = _generate(allergic, allergic.calculate_daily_calories(), 2, "b4:plan")
    assert plan is not None, "the allergic client got no plan at all"
    meals = _meals_of(plan)
    assert meals
    leaked = [m.name for m in meals if m.recipe_id in twins]
    assert not leaked, f"under-tagged dishes served to a peanut-allergic client: {leaked}"
    for meal in meals:
        if meal.recipe_id is not None:
            assert dish_tagging_gaps(meal.recipe) == [], meal.name
        for component in meal.components.all():
            assert food_tagging_gaps(component.food) == [], component.food.name_en
