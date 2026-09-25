"""What the review of the trainer nutrition work found (2026-09-24).

| # | Finding | Test |
|---|---|---|
| N1 | a draft sent after its start date retired the plan the client ate from on days they had already lived | `test_n1_*` |
| N2 | a replaced plan's adherence counted, as 0, the days after it stopped being followed | `test_n2_*` |
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_trainer_plans_0924 import (CREATE, JSON, MEALS, _automatic_plan, _body, _meal,
                                           _pair, foods)  # noqa: F401 (fixture)

pytestmark = pytest.mark.django_db


def test_n1_a_draft_whose_start_has_passed_is_not_sent(make_user, api, foods):
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "n1")
    today = timezone.localdate()
    automatic = _automatic_plan(client, today - timedelta(days=10), days=30)
    t = api(trainer)
    created = t.post(CREATE, _body(client, today + timedelta(days=1)), content_type=JSON)
    assert created.status_code == 200, created.content
    draft_id = created.json()["diet_plan"]["id"]
    # Days pass before the trainer sends it: its start is now behind the client.
    DietPlan.objects.filter(pk=draft_id).update(start_date=today - timedelta(days=5),
                                                end_date=today + timedelta(days=1))

    sent = t.post(MEALS, dict(_meal(today, "Lunch", (foods["base"], 150)),
                              diet_plan_id=draft_id), content_type=JSON)
    assert (sent.status_code, sent.json()["code"]) == (409, "draft_start_passed"), sent.content
    automatic.refresh_from_db()
    draft = DietPlan.objects.get(pk=draft_id)
    assert automatic.is_active, "the client's lived days were handed to a plan never shown"
    assert draft.is_draft and not draft.meals.exists()


def test_n1_a_draft_starting_today_is_sent(make_user, api, foods):
    trainer, client = _pair(make_user, "n1b")
    today = timezone.localdate()
    _automatic_plan(client, today)
    t = api(trainer)
    draft_id = t.post(CREATE, _body(client, today), content_type=JSON).json()["diet_plan"]["id"]
    sent = t.post(MEALS, dict(_meal(today, "Lunch", (foods["base"], 150)),
                              diet_plan_id=draft_id), content_type=JSON)
    assert sent.status_code == 200, sent.content


def test_n2_a_replaced_plan_is_scored_on_the_days_it_governed(make_user, api, foods):
    from diet.models import DietPlan, Meal

    trainer, client = _pair(make_user, "n2")
    today = timezone.localdate()
    t, c = api(trainer), api(client)
    # P: sent today for ten days, one lunch a day; its dates are then moved nine days
    # back (setup: a plan cannot be SENT to start in the past).
    p_id = t.post(CREATE, _body(client, today, duration_days=10, daily_calories=1000, meals=[
        _meal(today + timedelta(days=d), "Lunch", (foods["base"], 500)) for d in range(10)]),
        content_type=JSON).json()["diet_plan"]["id"]
    shift = timedelta(days=9)
    DietPlan.objects.filter(pk=p_id).update(start_date=today - shift,
                                            end_date=today - shift + timedelta(days=9))
    for meal in Meal.objects.filter(diet_plan_id=p_id):
        Meal.objects.filter(pk=meal.pk).update(date=meal.date - shift)
    # The client follows P perfectly on its first four days.
    for meal in Meal.objects.filter(diet_plan_id=p_id,
                                    date__lt=today - shift + timedelta(days=4)):
        done = c.post(f"/api/diet/v1/client/meals/{meal.id}/complete/",
                      {"action": "complete_meal"}, content_type=JSON)
        assert done.status_code == 200, done.content
    # Q replaces P from then on; P was retired four days after it began.
    q_id = t.post(CREATE, _body(client, today, meals=[
        _meal(today, "Lunch", (foods["rice"], 200))]), content_type=JSON).json()["diet_plan"]["id"]
    assert q_id
    DietPlan.objects.filter(pk=p_id).update(superseded_on=today - shift + timedelta(days=4))

    rows = {r["id"]: r for r in t.get(f"{CREATE}?client_id={client.id}").json()["results"]}
    assert rows[p_id]["status"] == "superseded"
    assert rows[p_id]["adherence_percent"] == 100.0, rows[p_id]


def test_n2_a_replacement_records_when(make_user, api, foods):
    from diet.models import DietPlan

    trainer, client = _pair(make_user, "n2b")
    today = timezone.localdate()
    automatic = _automatic_plan(client, today)
    t = api(trainer)
    t.post(CREATE, _body(client, today, meals=[_meal(today, "Lunch", (foods["base"], 150))]),
           content_type=JSON)
    automatic.refresh_from_db()
    assert (automatic.is_active, automatic.superseded_on) == (False, today)
    assert DietPlan.objects.filter(pk=automatic.pk, superseded_on=today).exists()
