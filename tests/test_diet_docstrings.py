"""The diet views' docstrings say what the wire does, across routes (BACKEND_BUGS B-142,
B-143; the fix run's wave 2, 2026-11-21).

The docstring of a view is its contract (CLAUDE.md): the app sessions read it, not the
code. Two splits were each documented only from one side, so a reader of one route
could not know the other existed:

- B-142: `plan_superseded` (a newer plan replaced this one) is 400 on a trainer's meal
  write, 403 on the trainer's plan reads and the review, and 409 on the client's meal
  completion. Every one of those statuses is shipped, so none changes: each docstring
  now names all three, and says to branch on the code.
- B-143: the food picker's `for_client.allergen_verdict` is ONE food's verdict
  (`safe` / `unverified` / `violation`); the review's `check.allergens.verdict` is the
  whole PLAN's (`nothing_declared` / `clear` / `unverified` / `violation`). A client who
  declared nothing reads `safe` on every picker row, an uncurated food included, and
  `nothing_declared` on the review; neither docstring said so.

The docstring tests fail on ccd5da0 (each docstring named only its own status and set).
The behaviour pins pass there too: they hold the wire to what the docstrings now say.
"""
import inspect
import re
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_regression_gate import _grant_diet_access

JSON = "application/json"
CREATE = "/api/diet/v1/trainer/diet-plans/"
MEALS = "/api/diet/v1/trainer/meals/"
LIST = "/api/diet/v1/food/list/"
COMPLETE = "client/meals/<meal_id>/complete/"


def _chunks(doc):
    """A docstring's paragraphs, each bullet of a list its own chunk."""
    chunks = []
    for paragraph in re.split(r"\n\s*\n", doc):
        chunks += [c for c in re.split(r"\n(?=\s*- )", paragraph) if c.strip()]
    return chunks


def _says(doc, *needles):
    """True when ONE chunk of `doc` that names `plan_superseded` holds every needle; a
    bare status is matched as a whole number."""
    def holds(chunk, needle):
        if needle.isdigit():
            return re.search(rf"(?<!\d){needle}(?!\d)", chunk) is not None
        return needle in chunk

    return any(all(holds(chunk, n) for n in needles)
               for chunk in _chunks(doc) if "`plan_superseded`" in chunk)


# ===================================================================== B-142
def test_plan_superseded_names_its_status_on_every_route():
    """Each route that answers `plan_superseded` names, beside its own status, the
    other two and where they are answered, and says to branch on the code."""
    from diet import trainer_plan_views
    from diet.views import (
        DietPlanMealsWithIngredientsView,
        DietPlanNutritionView,
        MealComponentsView,
        MealCompletionView,
        MealUndoView,
        TrainerMealView,
    )

    statuses = ("400", "403", "409", "branch on")
    expected = {
        # 400 here: the reads and the review are 403, the client's completion 409.
        TrainerMealView: ("nutrition/plan/", "meals/<meal_id>/components/",
                          "meals-with-ingredients/", "review/", COMPLETE),
        # 403 on these: the write is 400, the client's completion 409.
        DietPlanNutritionView: ("trainer/meals/", COMPLETE),
        MealComponentsView: ("trainer/meals/", COMPLETE),
        DietPlanMealsWithIngredientsView: ("trainer/meals/", COMPLETE),
        trainer_plan_views.TrainerPlanReviewView: ("trainer/meals/", COMPLETE),
        # 409 on the completion: the trainer's reads are 403, his writes 400.
        MealCompletionView: ("plan reads", "trainer/meals/"),
        # 409 on its undo too (Road to 10 S3.5), named the same way.
        MealUndoView: ("plan reads", "trainer/meals/"),
    }
    silent = [view.__name__ for view, routes in expected.items()
              if not _says(inspect.getdoc(view) or "", *statuses, *routes)]
    assert not silent, f"plan_superseded's three statuses are not named by: {silent}"


def _food(tag, *, source="verified"):
    from diet.models import FoodItem

    return FoodItem.objects.create(
        api_id=f"fxw2-{tag}", name=f"FXW2 {tag}", name_en=f"FXW2 {tag}",
        name_ar=f"طعام {tag}", calories=200, protein=20, carbs=20, fat=5,
        serving_size="100 g", serving_size_grams=100, allergens=[],
        allergen_source=source)


def _pair(make_user, tag):
    """A verified trainer and a client who approved him, both with diet access; no paid
    month, so the coaching refusal has nothing to refuse (free coaching)."""
    from users.models import TrainerClientRelation

    trainer = make_user(f"fxw2_t_{tag}", user_type="trainer", trainer_is_verified=True)
    client = make_user(f"fxw2_c_{tag}", height=175, weight=78, age=28, gender="Male",
                       activity_level="Moderate")
    for user in (trainer, client):
        _grant_diet_access(user)
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    return trainer, client


def _send(t, client, start, food):
    """A trainer plan SENT whole (BACKEND_BUGS B-132): one lunch on its first day."""
    from diet.models import DietPlan, DietPlanTemplate

    template = DietPlanTemplate.objects.first() or DietPlanTemplate.objects.create(
        name="fxw2 template", meals_per_day=3, snacks_per_day=1)
    response = t.post(CREATE, {
        "client_id": client.id, "template_id": template.id, "start_date": str(start),
        "goal": "Maintain", "daily_calories": 2000, "duration_weeks": 1,
        "meals": [{"meal_type": "Lunch", "target_date": str(start),
                   "food_items": [{"food_id": food.id, "quantity": 150}]}],
    }, content_type=JSON)
    assert response.status_code == 201, response.content[:400]
    return DietPlan.objects.get(pk=response.json()["diet_plan"]["id"])


@pytest.mark.django_db
def test_plan_superseded_answers_the_statuses_the_docstrings_name(make_user, api):
    """The real flow: the trainer sends a plan, then a second over the same window,
    which replaces the first. On the first, the code is the same everywhere and the
    status is each route's own, exactly as the docstrings say."""
    trainer, client = _pair(make_user, "b142")
    food = _food("b142")
    start = timezone.localdate() + timedelta(days=1)
    t = api(trainer)
    first = _send(t, client, start, food)
    second = _send(t, client, start, food)
    first.refresh_from_db()
    assert (first.is_active, first.is_draft, second.is_active) == (False, False, True)
    meal = first.meals.get()

    def refused(response):
        return response.status_code, response.json().get("code")

    superseded = (400, "plan_superseded")
    assert refused(t.post(MEALS, {
        "diet_plan_id": first.id, "meal_type": "Dinner", "target_date": str(start),
        "food_items": [{"food_id": food.id, "quantity": 100}]},
        content_type=JSON)) == superseded
    assert refused(t.post(MEALS, [{
        "diet_plan_id": first.id, "meal_type": "Dinner", "target_date": str(start),
        "food_items": [{"food_id": food.id, "quantity": 100}]}],
        content_type=JSON)) == superseded
    assert refused(t.put(f"{MEALS}{meal.id}/", {"description": "later"},
                         content_type=JSON)) == superseded
    assert refused(t.delete(f"{MEALS}{meal.id}/")) == superseded

    for read in (f"/api/diet/v1/nutrition/plan/{first.id}/",
                 f"/api/diet/v1/meals/{meal.id}/components/",
                 f"/api/diet/v1/plan/{first.id}/meals-with-ingredients/",
                 f"/api/diet/v1/trainer/diet-plans/{first.id}/review/"):
        assert refused(t.get(read)) == (403, "plan_superseded"), read

    completion = api(client).post(f"/api/diet/v1/client/meals/{meal.id}/complete/", {},
                                  content_type=JSON)
    assert refused(completion) == (409, "plan_superseded")


# ===================================================================== B-143
def test_the_picker_verdict_documents_nothing_declared():
    """The picker's row names the review's set and the case where the two meet (a
    client who declared nothing); the review names the row's set."""
    from diet.trainer_plan_views import _REVIEW_CONTRACT
    from diet.views import FoodListView

    picker = [c for c in _chunks(inspect.getdoc(FoodListView))
              if "`allergen_verdict` is" in c]
    assert picker, "FoodListView no longer defines allergen_verdict"
    row = picker[0]
    for needle in ("`safe`", "`unverified`", "`violation`", "`nothing_declared`",
                   "`clear`", "declared nothing", "every row", "review"):
        assert needle in row, f"FoodListView's allergen_verdict does not name {needle}"

    review = [c for c in _chunks(inspect.cleandoc(_REVIEW_CONTRACT))
              if "`allergens.verdict` is" in c]
    assert review, "_REVIEW_CONTRACT no longer defines allergens.verdict"
    for needle in ("`nothing_declared`", "`clear`", "`safe`", "`for_client.allergen_verdict`",
                   "food/list/", "declared nothing"):
        assert needle in review[0], f"the review contract does not name {needle}"


@pytest.mark.django_db
def test_a_client_who_declared_nothing_reads_safe_on_every_picker_row(make_user, api):
    """What the docstring now says, held: with nothing declared, an UNCURATED food reads
    `safe` on the picker (there is nothing to check it against) and the review of a plan
    holding it reads `nothing_declared`; once the client declares an allergy the same
    food reads `unverified` on both sides."""
    from diet.models import UserFoodPreference

    trainer, client = _pair(make_user, "b143")
    uncurated = _food("b143 uncurated", source="unknown")
    t = api(trainer)
    UserFoodPreference.objects.update_or_create(user=client, defaults={"allergies": ""})

    def picker_verdict():
        response = t.get(LIST, {"search": "FXW2 b143", "for_client": client.pk})
        assert response.status_code == 200, response.content[:400]
        (row,) = [r for r in response.json()["results"] if r["id"] == uncurated.id]
        return row["for_client"]["allergen_verdict"]

    plan = _send(t, client, timezone.localdate() + timedelta(days=1), uncurated)

    def review_verdict():
        response = t.get(f"/api/diet/v1/trainer/diet-plans/{plan.id}/review/")
        assert response.status_code == 200, response.content[:400]
        return response.json()["check"]["allergens"]["verdict"]

    assert (picker_verdict(), review_verdict()) == ("safe", "nothing_declared")

    UserFoodPreference.objects.filter(user=client).update(allergies="sesame")
    assert (picker_verdict(), review_verdict()) == ("unverified", "unverified")
