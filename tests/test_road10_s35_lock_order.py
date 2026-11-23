"""Road to 10 S3.5, the phase-3 review: every write to a meal and its foods takes the
MEAL's row lock first, so no two of them can deadlock.

The client's confirm and undo lock the meal, then its foods, then the day. The client's
food swap saved the food and then the meal, and the trainer's meal edit deleted the
foods and then saved the meal: the opposite order. A swap or an edit running against a
confirm of the same meal could each hold the row the other wanted, and Postgres aborted
one of them, a 500. Each path now locks the meal before it touches a food; this reads
the SQL each one sends and fails if any food row is written before the meal's
`FOR UPDATE`.
"""
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.test_road10_s35_meal_undo import JSON, _day, _url

pytestmark = pytest.mark.django_db

FOOD_WRITE = ('UPDATE "diet_mealcomponent"', 'DELETE FROM "diet_mealcomponent"',
              'INSERT INTO "diet_mealcomponent"')


def _meal_locked_before_any_food_write(queries, meal_pk):
    sqls = [q["sql"] for q in queries]
    writes = [i for i, sql in enumerate(sqls) if sql.startswith(FOOD_WRITE)]
    assert writes, "no food was written"
    locks = [i for i, sql in enumerate(sqls)
             if sql.startswith('SELECT') and 'FROM "diet_meal"' in sql
             and "FOR UPDATE" in sql and str(meal_pk) in sql]
    assert locks and locks[0] < writes[0], (
        "a food row was written before the meal's lock",
        sqls[writes[0]][:200], [sqls[i][:200] for i in locks])


def _third_food(meal):
    from diet.models import FoodItem

    food = meal.components.select_related("food").first().food
    return FoodItem.objects.create(
        name=f"Bulgur {uuid.uuid4().hex[:4]}", name_en="Bulgur", category=food.category,
        api_id=f"s35l-{uuid.uuid4().hex[:8]}", calories=100, protein=10, carbs=10, fat=1,
        serving_size="100g", allergens=[], allergen_source="verified")


def test_the_confirm_locks_the_meal_first(make_user, api, django_capture_on_commit_callbacks):
    client, _plan, meal = _day(make_user, "lock_confirm")
    with CaptureQueriesContext(connection) as queries:
        resp = api(client).post(_url(meal, "complete"), {"action": "complete_meal"},
                                content_type=JSON)
    assert resp.status_code == 200, resp.content
    _meal_locked_before_any_food_write(queries.captured_queries, meal.pk)


def test_the_undo_locks_the_meal_first(make_user, api, django_capture_on_commit_callbacks):
    client, _plan, meal = _day(make_user, "lock_undo")
    resp = api(client).post(_url(meal, "complete"), {"action": "complete_meal"},
                            content_type=JSON)
    assert resp.status_code == 200, resp.content
    with CaptureQueriesContext(connection) as queries:
        resp = api(client).post(_url(meal, "undo"), {}, content_type=JSON)
    assert resp.status_code == 200 and resp.json()["reopened"], resp.content
    _meal_locked_before_any_food_write(queries.captured_queries, meal.pk)


def test_the_clients_swap_locks_the_meal_first(make_user, api):
    client, _plan, meal = _day(make_user, "lock_swap")
    component = meal.components.order_by("id").first()
    chosen = _third_food(meal)
    with CaptureQueriesContext(connection) as queries:
        resp = api(client).post("/api/diet/v1/client/meals/interact/",
                                {"action": "swap_food", "component_id": component.pk,
                                 "chosen_food_id": chosen.pk}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    component.refresh_from_db()
    assert component.food_id == chosen.pk
    _meal_locked_before_any_food_write(queries.captured_queries, meal.pk)


def test_the_trainers_meal_edit_locks_the_meal_first(make_user, monkeypatch):
    from diet.trainer_services import TrainerDietPlanService

    _client, _plan, meal = _day(make_user, "lock_edit")
    trainer = make_user("s35l_trainer", user_type="trainer")
    service = TrainerDietPlanService(trainer)
    # Who may edit is not what this measures (tests/test_trainer_plans_0924.py is).
    monkeypatch.setattr(TrainerDietPlanService, "_authorize_plan", lambda self, plan: None)
    chosen = _third_food(meal)
    with CaptureQueriesContext(connection) as queries:
        service.update_meal(meal, food_items=[{"food_id": chosen.pk, "quantity": 150}])
    assert list(meal.components.values_list("food_id", flat=True)) == [chosen.pk]
    _meal_locked_before_any_food_write(queries.captured_queries, meal.pk)
