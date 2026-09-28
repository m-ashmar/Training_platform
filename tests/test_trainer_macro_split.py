"""A trainer may set a plan's macro split (BACKEND_BUGS B-137; the owner, 2026-09-28).

The trainer's plan create took `daily_calories` and `goal` only, and the split was
derived on the server from the goal: the design's TrainerPlanForm draws a draggable
split («توزيع المغذيات»), and on 2026-09-28 the form showed 156 / 446 / 110 g for
3,400 kcal while the stored plan read 148 / 515 / 83 g — the trainer sent one split and
the client was held to another.

The owner's rule: optional `target_protein`, `target_carbs`, `target_fat` (grams, all
three or none) on the create and on the draft review; the energy 4P + 4C + 9F within
±10% of `daily_calories`; protein within the engine's bounds for the goal; set while the
plan is written (there is no route that edits a sent plan); out of bounds is 400
`bad_macro_split`. Without them the split is derived exactly as before.

The split tests fail on 554c604: the fields were ignored and the plan stored the
derived grams (140.4 / 520.4 / 84.1 for this client), and nothing was refused.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_trainer_plans_0924 import (  # noqa: F401  (`foods` is a fixture)
    CREATE, DRAFT_REVIEW, JSON, MEALS, _body, _meal, _pair, _review, foods)

pytestmark = pytest.mark.django_db

SPLIT = {"target_protein": 156, "target_carbs": 446, "target_fat": 110}


def _plan_body(client, start, foods, **extra):
    body = _body(client, start, goal="Gain", daily_calories=3400,
                 meals=[_meal(start, "Breakfast", (foods["base"], 200)),
                        _meal(start, "Lunch", (foods["rice"], 300))])
    body.update(extra)
    return body


def test_the_trainer_split_is_stored_and_reviewed(make_user, api, foods):
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "split")
    start = timezone.localdate() + timedelta(days=1)
    t = api(trainer)

    reviewed = t.post(DRAFT_REVIEW, _plan_body(client, start, foods, **SPLIT),
                      content_type=JSON)
    assert reviewed.status_code == 200, reviewed.content
    assert reviewed.json()["target"] == {"calories": 3400.0, "protein": 156.0,
                                         "carbs": 446.0, "fat": 110.0}

    sent = t.post(CREATE, _plan_body(client, start, foods, **SPLIT), content_type=JSON)
    assert sent.status_code == 201, sent.content
    plan = DietPlan.objects.get(pk=sent.json()["diet_plan"]["id"])
    assert (plan.target_protein, plan.target_carbs, plan.target_fat) == (156, 446, 110)
    # The create now answers the split the plan holds, so the app can draw it.
    assert sent.json()["diet_plan"]["daily_targets"] == {
        "calories": 3400.0, "protein": 156.0, "carbs": 446.0, "fat": 110.0}

    # Every read of the plan's target is the trainer's split, not a re-derivation.
    saved = t.get(_review(plan.id))
    assert saved.status_code == 200, saved.content
    assert saved.json()["target"]["protein"] == 156.0
    assert saved.json()["target"]["carbs"] == 446.0
    assert saved.json()["target"]["fat"] == 110.0
    read = api(client).get(f"/api/diet/v1/plan/{plan.id}/meals-with-ingredients/")
    assert read.status_code == 200, read.content
    targets = read.json()["plan"]["daily_targets"]
    assert (targets["protein"], targets["carbs"], targets["fat"]) == (156.0, 446.0, 110.0)


def test_a_draft_keeps_the_split_it_was_created_with_when_it_is_sent(make_user, api, foods):
    """The old two calls: the draft is created with the split, and its first meals send
    it; the split is the draft's own, set while it is written."""
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "splitdraft")
    start = timezone.localdate() + timedelta(days=1)
    t = api(trainer)
    body = _plan_body(client, start, foods, **SPLIT)
    body.pop("meals")
    created = t.post(CREATE, body, content_type=JSON)
    assert created.status_code == 200, created.content
    plan_id = created.json()["diet_plan"]["id"]

    meal = _meal(start, "Breakfast", (foods["base"], 200))
    added = t.post(MEALS, {"diet_plan_id": plan_id, **meal}, content_type=JSON)
    assert added.status_code in (200, 201), added.content
    plan = DietPlan.objects.get(pk=plan_id)
    assert not plan.is_draft
    assert (plan.target_protein, plan.target_carbs, plan.target_fat) == (156, 446, 110)


@pytest.mark.parametrize("split", [
    # 4*156 + 4*446 + 9*160 = 3848 kcal, 13% over 3400.
    {"target_protein": 156, "target_carbs": 446, "target_fat": 160},
    # 4*100 + 4*300 + 9*80 = 2320 kcal, 32% under.
    {"target_protein": 100, "target_carbs": 300, "target_fat": 80},
    # Protein under the engine's floor (60 g), the energy inside the band: 40/650/89 =
    # 3561 kcal, 4.7% over.
    {"target_protein": 40, "target_carbs": 650, "target_fat": 89},
    # Protein over its ceiling (250 g): 300/380/100 = 3620, inside the band.
    {"target_protein": 300, "target_carbs": 380, "target_fat": 100},
    # Not all three.
    {"target_protein": 156},
    {"target_protein": 156, "target_carbs": 446},
    # Not positive numbers of grams.
    {"target_protein": 156, "target_carbs": 446, "target_fat": -1},
    {"target_protein": 156, "target_carbs": 446, "target_fat": 0},
    {"target_protein": "lots", "target_carbs": 446, "target_fat": 110},
    {"target_protein": True, "target_carbs": 446, "target_fat": 110},
    {"target_protein": "NaN", "target_carbs": 446, "target_fat": 110},
])
def test_a_split_out_of_bounds_is_refused_and_nothing_is_written(make_user, api, foods,
                                                                 split):
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "splitbad")
    start = timezone.localdate() + timedelta(days=1)
    t = api(trainer)
    before = DietPlan.objects.filter(user=client).count()

    for route in (CREATE, DRAFT_REVIEW):
        refused = t.post(route, _plan_body(client, start, foods, **split), content_type=JSON)
        assert refused.status_code == 400, (route, refused.content)
        assert refused.json()["code"] == "bad_macro_split", (route, refused.content)
    assert DietPlan.objects.filter(user=client).count() == before


def test_a_split_inside_the_band_is_taken_as_sent(make_user, api, foods):
    """10% either way is the owner's band: 3,682 kcal of macros on a 3,400 kcal plan
    (8.3% over) is the trainer's call to make, and it is kept to the gram."""
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "splitband")
    start = timezone.localdate() + timedelta(days=1)
    split = {"target_protein": 180, "target_carbs": 470.5, "target_fat": 120}
    sent = api(trainer).post(CREATE, _plan_body(client, start, foods, **split),
                             content_type=JSON)
    assert sent.status_code == 201, sent.content
    plan = DietPlan.objects.get(pk=sent.json()["diet_plan"]["id"])
    assert (plan.target_protein, plan.target_carbs, plan.target_fat) == (180, 470.5, 120)


def test_without_a_split_the_plan_is_derived_as_before(make_user, api, foods):
    """The control: no split fields, and the grams are the engine's for the goal."""
    from diet.models import DietPlan
    from diet.planner.policy import load_policy
    from diet.planner.targets import day_macro_grams

    trainer, client = _pair(make_user, "splitnone")
    start = timezone.localdate() + timedelta(days=1)
    sent = api(trainer).post(CREATE, _plan_body(client, start, foods), content_type=JSON)
    assert sent.status_code == 201, sent.content
    plan = DietPlan.objects.get(pk=sent.json()["diet_plan"]["id"])
    derived = day_macro_grams(3400.0, load_policy("Gain"), client.weight)
    assert plan.target_protein == pytest.approx(derived["protein"])
    assert plan.target_carbs == pytest.approx(derived["carb"])
    assert plan.target_fat == pytest.approx(derived["fat"])
