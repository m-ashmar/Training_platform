"""A plan read by id names whose plan it is (BACKEND_BUGS B-140).

The `plan` block of `GET plan/{id}/meals-with-ingredients/` carried the plan's id, goal,
targets and dates but not its client. A trainer who opened a sent plan by id from
«خططه» could not learn whose it was, so the food picker opened from it could not send
`?for_client=` and the per-client marks (B-127) were off for edits of a sent plan.

Fails on 554c604: the key is absent.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_trainer_plans_0924 import (  # noqa: F401  (`foods` is a fixture)
    CREATE, JSON, _body, _meal, _pair, foods)

pytestmark = pytest.mark.django_db


def _read(plan_id):
    return f"/api/diet/v1/plan/{plan_id}/meals-with-ingredients/"


def test_meals_with_ingredients_names_the_plans_client(make_user, api, foods):
    trainer, client = _pair(make_user, "b140")
    start = timezone.localdate() + timedelta(days=1)
    sent = api(trainer).post(
        CREATE, _body(client, start, meals=[_meal(start, "Breakfast", (foods["base"], 150))]),
        content_type=JSON)
    assert sent.status_code == 201, sent.content
    plan_id = sent.json()["diet_plan"]["id"]

    as_trainer = api(trainer).get(_read(plan_id))
    assert as_trainer.status_code == 200, as_trainer.content
    assert as_trainer.json()["plan"]["client_id"] == client.id

    as_client = api(client).get(_read(plan_id))
    assert as_client.status_code == 200, as_client.content
    assert as_client.json()["plan"]["client_id"] == client.id
