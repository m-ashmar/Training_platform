"""A food's stored allergen tags are the vocabulary on write, and read whole on read
(SIDE_BUGS S-215).

`validate_tags` was defined and never called, so free text (`["nuts"]`) was saved from
the console unrefused; `food_allergen_tags` then dropped what it could not read, so a
verified row tagged `nuts` read allergen-free to every reader outside the planner — the
catalogue row, the trainer picker's marks, the trainer write's `declared_allergen`
refusal. A JSON number in `allergens` raised TypeError in the middle of a read, and
`Recipe.allergen_tags()` read its lines' stored lists alone, disagreeing with the
checker about what a dish contains.

Every test fails on 554c604.
"""
import pytest

pytestmark = pytest.mark.django_db


def _food(name, *, allergens, source="verified", save=True):
    from diet.models import FoodItem

    food = FoodItem(name=name, name_en=name, name_ar=f"{name} ar", calories=400,
                    protein=20, carbs=50, fat=13, serving_size="100g",
                    serving_size_grams=100, allergens=allergens, allergen_source=source)
    if save:
        food.save()
    return food


def _console_forms(make_user):
    """The food form of each console, as its change page builds it."""
    from django.contrib import admin
    from django.test import RequestFactory

    from admin_dashboard.admin import admin_site
    from diet.models import FoodItem

    request = RequestFactory().get("/")
    request.user = make_user("s215_op", user_type="admin")
    return [site._registry[FoodItem].get_form(request) for site in (admin.site, admin_site)]


def _allergens_error(form_class, allergens_text):
    """The form's answer about `allergens` when an operator edits a stored food and
    types `allergens_text` into that box (the other boxes' own answers are not read)."""
    food = _food("S215 Console Bar", allergens=[])
    form = form_class(data={"allergens": allergens_text}, instance=food)
    form.is_valid()
    return form.errors.get("allergens")


@pytest.mark.parametrize("allergens_text", ['["nuts"]', '["peanut", "Dairy"]',
                                            '"peanut"', "5", "[5]", "null"])
def test_a_food_whose_tags_are_not_the_vocabulary_is_refused_on_write(make_user,
                                                                      allergens_text):
    """Both consoles (`/admin/`, `/dj-admin/`) edit a food through one form; free text
    in `allergens` is refused there."""
    for form_class in _console_forms(make_user):
        assert _allergens_error(form_class, allergens_text), (form_class, allergens_text)


@pytest.mark.parametrize("allergens_text", ["[]", '["peanut"]',
                                            '["milk", "gluten", "sesame"]'])
def test_the_vocabulary_is_accepted(make_user, allergens_text):
    for form_class in _console_forms(make_user):
        assert _allergens_error(form_class, allergens_text) is None, allergens_text


def test_a_row_stored_malformed_can_still_be_saved(seeded_catalogue):
    """The rule is on the form, not in `FoodItem.clean` (which every save runs): a row
    stored malformed before the rule must stay writable, the review tick included."""
    bar = _food("S215 Old Bar", allergens=["nuts"])
    bar.needs_review = True
    bar.save()
    bar.refresh_from_db()
    assert bar.needs_review and bar.allergens == ["nuts"]


def test_a_free_text_tag_already_stored_is_read_not_dropped():
    """A row saved before the write was refused: `nuts` is read as a client's "nuts"
    is — peanut and tree nut — so the checker refuses it to a peanut-allergic client
    instead of calling a verified row safe."""
    from diet.allergens import food_allergen_tags, food_tagging_gaps
    from diet.services.meal_validator import VIOLATION, AllergenChecker

    bar = _food("S215 Harvest Seed Bar", allergens=["nuts"])
    assert food_allergen_tags(bar) == {"peanut", "tree_nut"}
    verdict = AllergenChecker("peanut").check_food(bar)
    assert verdict.verdict == VIOLATION and verdict.matched == ["peanut"]
    # It is still not a curation anyone can vouch for.
    assert food_tagging_gaps(bar) == ["malformed_tags"]

    bare = _food("S215 Toasted Chips", allergens="soy")
    assert food_allergen_tags(bare) == {"soy"}


def test_a_number_in_allergens_does_not_break_a_read():
    """`food_allergen_tags` iterated the stored value: a JSON number raised TypeError
    in the catalogue read, the checker and the trainer picker alike."""
    from diet.allergens import food_allergen_tags, food_tagging_gaps
    from diet.services.meal_validator import VIOLATION, AllergenChecker

    crunch = _food("S215 Peanut Crunch", allergens=5)
    assert food_allergen_tags(crunch) == {"peanut"}
    assert AllergenChecker("peanut").check_food(crunch).verdict == VIOLATION
    assert "malformed_tags" in food_tagging_gaps(crunch)


def test_a_dish_reads_its_allergens_as_the_checker_does(seeded_catalogue):
    """`Recipe.allergen_tags()` is what the dish carries by the one reading every other
    reader makes: each line through `food_allergen_tags`, and the dish's own text."""
    from diet.allergens import dish_text, food_allergen_tags, infer_allergens
    from diet.models import FoodItem, Recipe, RecipeIngredient

    chicken = FoodItem.objects.get(name_en="Chicken Breast (Grilled)")
    bites = _food("S215 Crispy Chickpea Bites", allergens=[])      # "crispy": gluten
    bar = _food("S215 Seed Bar", allergens=["nuts"])
    satay = Recipe.objects.create(name="S215 Chicken Satay Bowl",
                                  name_en="S215 Chicken Satay Bowl",
                                  name_ar="S215 ar", meal_types=["Lunch"],
                                  cuisine="Levantine")
    for food, grams in ((chicken, 150), (bites, 60), (bar, 30)):
        RecipeIngredient.objects.create(recipe=satay, food=food, grams=grams)

    expected = set().union(*(food_allergen_tags(f) for f in (chicken, bites, bar)))
    expected |= infer_allergens(dish_text(satay))
    assert satay.allergen_tags() == sorted(expected)
    assert {"gluten", "peanut", "tree_nut"} <= set(satay.allergen_tags())
