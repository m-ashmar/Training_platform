"""A trainer's nutrition plan: sent whole, sized in days, reviewed, and read back
(BACKEND_BUGS B-131..B-134, found wiring the design's 14-trainer-nutrition screens).

- B-132 (P1): creating a plan retired the client's plan (his automatic one included)
  before a single meal existed, and the meals arrived in later requests: a refused meal
  list or a dropped phone left the client with an empty active plan. A plan is now sent
  WITH its meals, all or nothing; the old create makes a draft that retires nothing until
  its first meals land.
- B-133: a plan length in days (the design's 30), beside weeks.
- B-134: a trainer's plan row says why it is not active, and its adherence.
- B-131: the review — per day against the plan's target with the engine's own tolerance,
  the averages, and the allergen / dislike verdict — for the approved trainer only.

Every test fails on 7831839 (the routes did not exist, the fields were not there, or the
create retired the client's plan at once).
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_regression_gate import _grant_diet_access

pytestmark = pytest.mark.django_db

JSON = "application/json"
CREATE = "/api/diet/v1/trainer/diet-plans/"
MEALS = "/api/diet/v1/trainer/meals/"
DRAFT_REVIEW = "/api/diet/v1/trainer/diet-plans/review/"


def _review(plan_id):
    return f"/api/diet/v1/trainer/diet-plans/{plan_id}/review/"


# ------------------------------------------------------------------------ helpers
def _food(tag, *, kcal, protein, carbs, fat, allergens=(), name_ar=None):
    """A catalogue row with known numbers per 100 g, so the review's arithmetic can be
    checked by hand."""
    from diet.models import FoodItem

    return FoodItem.objects.create(
        api_id=f"tp0924-{tag}", name=f"TP {tag}", name_ar=name_ar or f"طعام {tag}",
        calories=kcal, protein=protein, carbs=carbs, fat=fat, serving_size="100 g",
        allergens=list(allergens), allergen_source="verified")


@pytest.fixture
def foods(db):
    return {
        # 100 g: 200 kcal, 20 P, 20 C, 5 F (a round food for round sums)
        "base": _food("base", kcal=200, protein=20, carbs=20, fat=5),
        "rice": _food("rice", kcal=130, protein=2.7, carbs=28, fat=0.3),
        "peanut": _food("peanut", kcal=588, protein=25, carbs=20, fat=50,
                        allergens=["peanut"]),
        "liver": _food("liver", kcal=135, protein=20, carbs=4, fat=4),
    }


def _pair(make_user, tag):
    """A trainer and a client with an approved relation and diet access, no paid month
    (free coaching: nothing to freeze)."""
    from users.models import TrainerClientRelation

    trainer = make_user(f"tp_t_{tag}", user_type="trainer", trainer_is_verified=True)
    client = make_user(f"tp_c_{tag}", height=175, weight=78, age=28, gender="Male",
                       activity_level="Moderate")
    for user in (trainer, client):
        _grant_diet_access(user)
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    return trainer, client


def _automatic_plan(client, start, days=14):
    """The client's own engine plan, active over the window."""
    from diet.models import DietPlan

    return DietPlan.objects.create(
        user=client, goal="Maintain", daily_calories=2300, start_date=start,
        end_date=start + timedelta(days=days - 1), generation_strategy=DietPlan.ENGINE,
        is_active=True)


def _body(client, start, **extra):
    from diet.models import DietPlanTemplate

    template = DietPlanTemplate.objects.first() or DietPlanTemplate.objects.create(
        name="tp0924 template", meals_per_day=3, snacks_per_day=1)
    body = {"client_id": client.id, "template_id": template.id, "start_date": str(start),
            "goal": "Maintain", "daily_calories": 2000, "duration_weeks": 1}
    body.update(extra)
    return body


def _declare(client, allergies):
    """The client's food preferences: one row per client, which may already exist."""
    from diet.models import UserFoodPreference

    preference, _created = UserFoodPreference.objects.update_or_create(
        user=client, defaults={"allergies": allergies})
    return preference


def _meal(day, meal_type, *items, time=None):
    row = {"meal_type": meal_type, "target_date": str(day),
           "food_items": [{"food_id": f.id, "quantity": g} for f, g in items]}
    if time:
        row["scheduled_time"] = time
    return row


# ============================================================ B-132 — sent whole
def test_b132_the_old_create_retires_nothing_until_its_meals_land(make_user, api, foods):
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "legacy")
    today = timezone.localdate()
    automatic = _automatic_plan(client, today)
    t = api(trainer)

    created = t.post(CREATE, _body(client, today + timedelta(days=1)), content_type=JSON)
    assert created.status_code == 200, created.content
    body = created.json()
    plan = DietPlan.objects.get(pk=body["diet_plan"]["id"])
    # Nothing retired yet: the client still eats from his automatic plan.
    assert body["superseded_plan_ids"] == []
    assert body["will_replace_plan_ids"] == [automatic.id]
    assert body["diet_plan"]["status"] == "draft"
    automatic.refresh_from_db()
    assert automatic.is_active, "the create retired the client's plan before any meal existed"
    assert plan.is_draft and not plan.is_active

    # A refused meal list (a declared allergen) changes nothing either.
    _declare(client, "peanuts")
    refused = t.post(MEALS, [dict(_meal(plan.start_date, "Lunch", (foods["base"], 150)),
                                  diet_plan_id=plan.id),
                             dict(_meal(plan.start_date, "Dinner", (foods["peanut"], 30)),
                                  diet_plan_id=plan.id)], content_type=JSON)
    assert (refused.status_code, refused.json()["code"], refused.json()["index"]) == \
        (400, "declared_allergen", 1)
    automatic.refresh_from_db()
    plan.refresh_from_db()
    assert automatic.is_active and plan.is_draft and not plan.meals.exists()

    # The first list that lands sends the plan: it retires the automatic plan now, and
    # says so.
    landed = t.post(MEALS, [dict(_meal(plan.start_date, "Lunch", (foods["base"], 150)),
                                 diet_plan_id=plan.id),
                            dict(_meal(plan.start_date, "Dinner", (foods["rice"], 200)),
                                 diet_plan_id=plan.id)], content_type=JSON)
    assert landed.status_code == 201, landed.content
    assert landed.json()["published"] == [{"diet_plan_id": plan.id,
                                           "superseded_plan_ids": [automatic.id]}]
    automatic.refresh_from_db()
    plan.refresh_from_db()
    assert not automatic.is_active and plan.is_active and not plan.is_draft

    # A later list on the sent plan publishes nothing again.
    more = t.post(MEALS, dict(_meal(plan.start_date + timedelta(days=1), "Lunch",
                                    (foods["base"], 100)), diet_plan_id=plan.id),
                  content_type=JSON)
    assert more.status_code == 200, more.content
    assert more.json()["published"] == []


def test_b132_one_meal_sends_a_draft_too(make_user, api, foods):
    """The single-meal POST, not only the list."""
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "single")
    today = timezone.localdate()
    automatic = _automatic_plan(client, today)
    t = api(trainer)
    plan_id = t.post(CREATE, _body(client, today), content_type=JSON).json()["diet_plan"]["id"]

    sent = t.post(MEALS, dict(_meal(today, "Lunch", (foods["base"], 150)), diet_plan_id=plan_id),
                  content_type=JSON)
    assert sent.status_code == 200, sent.content
    assert sent.json()["published"] == [{"diet_plan_id": plan_id,
                                         "superseded_plan_ids": [automatic.id]}]
    assert DietPlan.objects.get(pk=plan_id).is_active
    automatic.refresh_from_db()
    assert not automatic.is_active


def test_b132_a_plan_sent_with_its_meals_is_all_or_nothing(make_user, api, foods):
    from diet.models import DietPlan, Meal

    trainer, client = _pair(make_user, "whole")
    today = timezone.localdate()
    start = today + timedelta(days=1)
    automatic = _automatic_plan(client, today)
    t = api(trainer)
    before = DietPlan.objects.count()

    meals = [_meal(start, "Breakfast", (foods["base"], 100)),
             {"meal_type": "Lunch", "target_date": str(start),
              "food_items": [{"food_id": 99999999, "quantity": 100}]}]
    refused = t.post(CREATE, _body(client, start, meals=meals), content_type=JSON)
    assert (refused.status_code, refused.json()["code"], refused.json()["index"]) == \
        (400, "unknown_food", 1)
    assert DietPlan.objects.count() == before, "a refused plan left a row behind"
    automatic.refresh_from_db()
    assert automatic.is_active, "a refused plan retired the client's plan"

    # The same slot twice in one plan is the slot conflict the meal route answers.
    twice = t.post(CREATE, _body(client, start, meals=[
        _meal(start, "Lunch", (foods["base"], 100)),
        _meal(start, "Lunch", (foods["rice"], 100))]), content_type=JSON)
    assert (twice.status_code, twice.json()["code"], twice.json()["index"]) == \
        (409, "duplicate_meal", 1)
    # A meal outside the plan's days, and a malformed item, name their index too.
    outside = t.post(CREATE, _body(client, start, meals=[
        _meal(start + timedelta(days=7), "Lunch", (foods["base"], 100))]), content_type=JSON)
    assert (outside.status_code, outside.json()["code"], outside.json()["index"]) == \
        (400, "date_outside_plan", 0)
    bad_date = t.post(CREATE, _body(client, start, meals=[
        {"meal_type": "Lunch", "target_date": "tomorrow",
         "food_items": [{"food_id": foods["base"].id, "quantity": 100}]}]), content_type=JSON)
    assert (bad_date.status_code, bad_date.json()["code"], bad_date.json()["index"]) == \
        (400, "bad_date", 0)
    for shape in ([], "lunch", {"meal_type": "Lunch"}):
        wrong = t.post(CREATE, _body(client, start, meals=shape), content_type=JSON)
        assert (wrong.status_code, wrong.json()["code"]) == (400, "bad_meals"), shape
    assert DietPlan.objects.count() == before

    sent = t.post(CREATE, _body(client, start, meals=[
        _meal(start, "Breakfast", (foods["base"], 100)),
        _meal(start, "Lunch", (foods["base"], 150), (foods["rice"], 200)),
        _meal(start, "Snack", (foods["rice"], 50), time="10:00"),
        _meal(start, "Snack", (foods["rice"], 60), time="16:00"),
        _meal(start + timedelta(days=6), "Dinner", (foods["base"], 120))]), content_type=JSON)
    assert sent.status_code == 201, sent.content
    body = sent.json()
    plan = DietPlan.objects.get(pk=body["diet_plan"]["id"])
    assert body["superseded_plan_ids"] == [automatic.id]
    assert body["diet_plan"]["status"] == "scheduled"
    assert body["diet_plan"]["meals_count"] == 5
    assert plan.is_active and not plan.is_draft
    automatic.refresh_from_db()
    assert not automatic.is_active
    lunch = Meal.objects.get(diet_plan=plan, meal_type="Lunch")
    assert lunch.components.count() == 2 and lunch.name and lunch.name_ar and lunch.reason_ar


def test_b132_a_plan_sent_whole_refuses_a_declared_allergen(make_user, api, foods):
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "whole_allergen")
    _declare(client, "peanut")
    today = timezone.localdate()
    before = DietPlan.objects.count()
    refused = api(trainer).post(CREATE, _body(client, today, meals=[
        _meal(today, "Lunch", (foods["peanut"], 30))]), content_type=JSON)
    assert (refused.status_code, refused.json()["code"], refused.json()["index"]) == \
        (400, "declared_allergen", 0)
    assert DietPlan.objects.count() == before


def test_b132_a_plan_sent_whole_is_bounded(make_user, api, foods):
    trainer, client = _pair(make_user, "whole_cap")
    today = timezone.localdate()
    from diet.trainer_services import MAX_PLAN_MEALS

    too_many = [_meal(today, "Lunch", (foods["base"], 100))] * (MAX_PLAN_MEALS + 1)
    refused = api(trainer).post(CREATE, _body(client, today, meals=too_many), content_type=JSON)
    assert (refused.status_code, refused.json()["code"], refused.json()["limit"]) == \
        (400, "batch_too_large", MAX_PLAN_MEALS)


def test_b132_the_client_never_sees_a_draft(make_user, api, foods):
    trainer, client = _pair(make_user, "hidden")
    today = timezone.localdate()
    c = api(client)
    plan_id = api(trainer).post(CREATE, _body(client, today), content_type=JSON) \
        .json()["diet_plan"]["id"]

    listed = c.get("/api/diet/v1/my/diet-plans/")
    assert listed.status_code == 200, listed.content
    assert plan_id not in [row["id"] for row in listed.json()["results"]]
    for path in (f"/api/diet/v1/nutrition/plan/{plan_id}/",
                 f"/api/diet/v1/plan/{plan_id}/meals-with-ingredients/"):
        assert c.get(path).status_code == 404, path

    # Once sent, it is his.
    api(trainer).post(MEALS, dict(_meal(today, "Lunch", (foods["base"], 100)),
                                  diet_plan_id=plan_id), content_type=JSON)
    listed = c.get("/api/diet/v1/my/diet-plans/")
    assert plan_id in [row["id"] for row in listed.json()["results"]]
    assert c.get(f"/api/diet/v1/nutrition/plan/{plan_id}/").status_code == 200


def test_b132_a_draft_waits_while_the_client_generates_and_then_wins(make_user, api, foods):
    """The client's own generation retires active plans only; a draft is not one. The
    trainer's first meals then send it, and it replaces whatever the client follows."""
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "race")
    today = timezone.localdate()
    t = api(trainer)
    plan_id = t.post(CREATE, _body(client, today), content_type=JSON).json()["diet_plan"]["id"]
    generated = _automatic_plan(client, today, days=3)     # generated after the draft

    sent = t.post(MEALS, dict(_meal(today, "Lunch", (foods["base"], 100)), diet_plan_id=plan_id),
                  content_type=JSON)
    assert sent.status_code == 200, sent.content
    assert sent.json()["published"][0]["superseded_plan_ids"] == [generated.id]
    assert DietPlan.governing(client).first().id == plan_id


# ============================================================ B-133 — days
def test_b133_a_plan_can_be_thirty_days(make_user, api, foods):
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "days")
    today = timezone.localdate()
    t = api(trainer)
    created = t.post(CREATE, _body(client, today, duration_days=30, duration_weeks=None),
                     content_type=JSON)
    assert created.status_code == 200, created.content
    row = created.json()["diet_plan"]
    plan = DietPlan.objects.get(pk=row["id"])
    assert plan.end_date == today + timedelta(days=29)
    assert (row["duration_days"], row["duration_weeks"]) == (30, 5)

    last = t.post(MEALS, dict(_meal(today + timedelta(days=29), "Lunch", (foods["base"], 100)),
                              diet_plan_id=plan.id), content_type=JSON)
    assert last.status_code == 200, last.content
    past = t.post(MEALS, dict(_meal(today + timedelta(days=30), "Dinner", (foods["base"], 100)),
                              diet_plan_id=plan.id), content_type=JSON)
    assert (past.status_code, past.json()["code"]) == (400, "date_outside_plan")

    # Days win over weeks when both are sent; weeks alone keep working.
    both = t.post(CREATE, _body(client, today, duration_days=10, duration_weeks=3),
                  content_type=JSON).json()["diet_plan"]
    assert both["duration_days"] == 10
    weeks = t.post(CREATE, _body(client, today, duration_weeks=2), content_type=JSON)
    assert weeks.json()["diet_plan"]["duration_days"] == 14
    assert DietPlan.objects.get(pk=weeks.json()["diet_plan"]["id"]).end_date == \
        today + timedelta(days=13)

    for bad in (0, 365, -3, "abc", 7.5, True):
        refused = t.post(CREATE, _body(client, today, duration_days=bad), content_type=JSON)
        assert (refused.status_code, refused.json()["code"]) == (400, "bad_duration"), bad

    listed = t.get(f"{CREATE}?client_id={client.id}").json()["results"]
    assert {r["duration_days"] for r in listed} >= {30, 10, 14}


# ============================================================ B-134 — the rows
def test_b134_a_row_says_why_it_is_not_active(make_user, api, foods):
    trainer, client = _pair(make_user, "rows")
    today = timezone.localdate()
    t = api(trainer)

    def send(start, days, meal_day=None):
        return t.post(CREATE, _body(client, start, duration_days=days, meals=[
            _meal(meal_day or start, "Lunch", (foods["base"], 100))]),
            content_type=JSON).json()["diet_plan"]["id"]

    ended = send(today - timedelta(days=20), 7)
    replaced = send(today, 7)
    current = send(today, 3)                           # replaces `replaced`
    upcoming = send(today + timedelta(days=10), 5)
    draft = t.post(CREATE, _body(client, today + timedelta(days=30)),
                   content_type=JSON).json()["diet_plan"]["id"]

    listed = t.get(f"{CREATE}?client_id={client.id}")
    assert listed.status_code == 200, listed.content
    rows = {r["id"]: r for r in listed.json()["results"]}
    assert {pid: rows[pid]["status"] for pid in (ended, replaced, current, upcoming, draft)} == {
        ended: "ended", replaced: "superseded", current: "active", upcoming: "scheduled",
        draft: "draft"}
    # `is_active` keeps its meaning (the flag), beside the reason.
    assert rows[replaced]["is_active"] is False and rows[draft]["is_active"] is False
    assert rows[ended]["is_active"] is True
    # Newest start first, ties broken by id: a total order.
    starts = [(r["start_date"], r["id"]) for r in listed.json()["results"]]
    assert starts == sorted(starts, reverse=True)


def test_b134_adherence_is_the_platforms_figure_from_ticked_meals(make_user, api, foods):
    from ai_assistant.tools.diet_tools import plan_day_adherence
    from diet.models import DietPlan, Meal

    trainer, client = _pair(make_user, "adherence")
    today = timezone.localdate()
    start = today - timedelta(days=3)
    t, c = api(trainer), api(client)
    followed = t.post(CREATE, _body(client, start, meals=[
        _meal(start + timedelta(days=d), "Lunch", (foods["base"], 500)) for d in range(3)]),
        content_type=JSON).json()["diet_plan"]["id"]
    ticked = Meal.objects.get(diet_plan_id=followed, date=start + timedelta(days=1))
    done = c.post(f"/api/diet/v1/client/meals/{ticked.id}/complete/",
                  {"action": "complete_meal"}, content_type=JSON)
    assert done.status_code == 200, done.content

    untouched = t.post(CREATE, _body(client, today + timedelta(days=5), meals=[
        _meal(today + timedelta(days=5), "Lunch", (foods["base"], 100))]),
        content_type=JSON).json()["diet_plan"]["id"]
    draft = t.post(CREATE, _body(client, today + timedelta(days=40)),
                   content_type=JSON).json()["diet_plan"]["id"]

    listed = t.get(f"{CREATE}?client_id={client.id}").json()
    rows = {r["id"]: r for r in listed["results"]}
    plan = DietPlan.objects.get(pk=followed)
    expected = plan_day_adherence(client, plan, plan.start_date, plan.end_date)["average"]
    assert expected is not None
    assert rows[followed]["adherence_percent"] == expected
    assert rows[untouched]["adherence_percent"] is None
    assert rows[draft]["adherence_percent"] is None
    assert listed["adherence_withheld"] is None


# ============================================================ B-131 — the review
def test_b131_the_review_of_a_sent_plan(make_user, api, foods):
    from diet.planner.policy import load_policy

    trainer, client = _pair(make_user, "review")
    today = timezone.localdate()
    t = api(trainer)
    # Target 2000 kcal. Day 1: 1000 g of `base` = 2000 kcal (on target). Day 2: 500 g =
    # 1000 kcal (50 % under). Day 3: no meals.
    plan_id = t.post(CREATE, _body(client, today, daily_calories=2000, duration_days=3, meals=[
        _meal(today, "Lunch", (foods["base"], 500)),
        _meal(today, "Dinner", (foods["base"], 500)),
        _meal(today + timedelta(days=1), "Lunch", (foods["base"], 400), (foods["liver"], 100))]),
        content_type=JSON).json()["diet_plan"]["id"]
    # Declared after the plan was written: the review is where the trainer learns it.
    preference = _declare(client, "peanut")
    preference.disliked_foods.add(foods["liver"])

    review = t.get(_review(plan_id))
    assert review.status_code == 200, review.content
    body = review.json()
    tolerance = load_policy("Maintain").tolerance
    assert body["tolerance_percent"]["calories"] == round(tolerance["calories"] * 100, 1)
    assert body["tolerance_percent"]["carbs"] == round(tolerance["carb"] * 100, 1)
    assert body["target"]["calories"] == 2000
    assert [d["date"] for d in body["days"]] == [str(today + timedelta(days=i)) for i in range(3)]

    first, second, third = body["days"]
    assert (first["day"], first["meals_count"], first["totals"]["calories"]) == (1, 2, 2000.0)
    assert first["deviation"]["calories"] == {"amount": 0.0, "percent": 0.0, "within": True}
    assert second["totals"]["calories"] == 400 * 2 + 135.0
    assert second["deviation"]["calories"]["within"] is False
    assert second["deviation"]["calories"]["amount"] == round(935.0 - 2000, 1)
    assert second["within_tolerance"] is False
    assert (third["meals_count"], third["within_tolerance"]) == (0, False)
    summary = body["summary"]
    assert (summary["days"], summary["days_with_meals"], summary["empty_days"]) == (3, 2, 1)
    # The averages are over the days that hold meals.
    assert body["averages"]["totals"]["calories"] == round((2000 + 935) / 2, 1)

    check = body["check"]
    assert check["preferences_on_file"] is True
    assert check["declared_allergens"] == ["peanut"]
    assert check["allergens"]["verdict"] == "clear"
    assert check["dislikes"]["verdict"] == "present"
    assert check["dislikes"]["foods"] == [{
        "food_id": foods["liver"].id, "name": "TP liver", "name_ar": "طعام liver",
        "dates": [str(today + timedelta(days=1))]}]


def test_b131_the_review_of_a_plan_not_yet_sent(make_user, api, foods):
    """The phone's draft, reviewed before it is sent: nothing is written, and a declared
    allergen is REPORTED here (the send refuses it)."""
    from diet.models import DietPlan, Meal

    trainer, client = _pair(make_user, "draft_review")
    _declare(client, "peanuts, sesame")
    today = timezone.localdate()
    plans, meals = DietPlan.objects.count(), Meal.objects.count()
    body = _body(client, today, duration_days=2, daily_calories=2400, meals=[
        _meal(today, "Lunch", (foods["base"], 600), (foods["peanut"], 30)),
        _meal(today + timedelta(days=1), "Lunch", (foods["base"], 1200))])
    body.pop("template_id")                    # not needed to review

    review = api(trainer).post(DRAFT_REVIEW, body, content_type=JSON)
    assert review.status_code == 200, review.content
    out = review.json()
    assert (DietPlan.objects.count(), Meal.objects.count()) == (plans, meals)
    assert out["plan"]["id"] is None and out["plan"]["duration_days"] == 2
    assert out["days"][1]["deviation"]["calories"]["within"] is True       # 2400 of 2400
    assert out["check"]["allergens"]["verdict"] == "violation"
    assert out["check"]["allergens"]["violations"] == [{
        "food_id": foods["peanut"].id, "name": "TP peanut", "name_ar": "طعام peanut",
        "allergens": ["peanut"], "dates": [str(today)]}]
    assert sorted(out["check"]["declared_allergens"]) == ["peanut", "sesame"]

    # The same item refusals as the send, with the index.
    bad = api(trainer).post(DRAFT_REVIEW, dict(body, meals=[
        _meal(today, "Brunch", (foods["base"], 100))]), content_type=JSON)
    assert (bad.status_code, bad.json()["code"], bad.json()["index"]) == (400, "bad_meal_type", 0)
    missing = api(trainer).post(DRAFT_REVIEW, {"client_id": client.id}, content_type=JSON)
    assert (missing.status_code, missing.json()["code"]) == (400, "missing_fields")


def test_b131_only_the_approved_trainer_reads_it(make_user, api, foods):
    trainer, client = _pair(make_user, "who")
    today = timezone.localdate()
    plan_id = api(trainer).post(CREATE, _body(client, today, meals=[
        _meal(today, "Lunch", (foods["base"], 100))]), content_type=JSON).json()["diet_plan"]["id"]
    draft = _body(client, today, meals=[_meal(today, "Lunch", (foods["base"], 100))])

    stranger = make_user("tp_t_stranger", user_type="trainer", trainer_is_verified=True)
    _grant_diet_access(stranger)
    s = api(stranger)
    assert (s.get(_review(plan_id)).status_code, s.get(_review(plan_id)).json()["code"]) == \
        (403, "not_plan_owner")
    refused = s.post(DRAFT_REVIEW, draft, content_type=JSON)
    assert (refused.status_code, refused.json()["code"]) == (403, "not_assigned_client")

    for who in (client, make_user("tp_admin", user_type="admin")):
        _grant_diet_access(who)
        for resp in (api(who).get(_review(plan_id)),
                     api(who).post(DRAFT_REVIEW, draft, content_type=JSON)):
            assert (resp.status_code, resp.json()["code"]) == (403, "not_a_trainer")

    # The client dismisses the trainer: the health data goes with the relation.
    from users.models import TrainerClientRelation
    TrainerClientRelation.objects.filter(trainer=trainer, client=client).delete()
    t = api(trainer)
    assert (t.get(_review(plan_id)).status_code, t.get(_review(plan_id)).json()["code"]) == \
        (403, "not_assigned_client")
    refused = t.post(DRAFT_REVIEW, draft, content_type=JSON)
    assert (refused.status_code, refused.json()["code"]) == (403, "not_assigned_client")
    assert api(trainer).get(_review(999999999)).status_code == 404


def test_b131_b134_a_frozen_coaching_withholds_the_clients_data(make_user, api, foods, settings):
    """Seeded as the platform writes it: a real paid month, then the renewal that froze
    it (tests/test_coaching_freeze.py)."""
    from tests.test_coaching_freeze import _coached, _freeze

    settings.COACHING_PERIOD_DAYS = 30
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    trainer, client, relation, *_rest = _coached(make_user, api, "tp_frozen")
    for user in (trainer, client):
        _grant_diet_access(user)
    today = timezone.localdate()
    t = api(trainer)
    plan_id = t.post(CREATE, _body(client, today - timedelta(days=2), meals=[
        _meal(today - timedelta(days=2), "Lunch", (foods["base"], 100))]),
        content_type=JSON).json()["diet_plan"]["id"]
    draft = _body(client, today, meals=[_meal(today, "Lunch", (foods["base"], 100))])
    assert t.get(_review(plan_id)).status_code == 200

    _freeze(relation)
    for resp in (t.get(_review(plan_id)), t.post(DRAFT_REVIEW, draft, content_type=JSON)):
        assert (resp.status_code, resp.json()["code"]) == (403, "coaching_frozen"), resp.content
    listed = t.get(f"{CREATE}?client_id={client.id}").json()
    assert listed["adherence_withheld"] == "coaching_frozen"
    assert all(row["adherence_percent"] is None for row in listed["results"])
