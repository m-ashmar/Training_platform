"""BACKEND_BUGS B-201 (the manager's ruling, 2026-11-24): one rule for every trainer read
of a client's personal data. While `users.coaching.refusals_for` gives a refusal for the
pair, the client's record is held back — as `trainer/clients/` (G10) and the trainer's
diet reads already did, and as the client's file did not:

  GET /api/auth/trainer/client-profile/{id}/ and the list   the record null, `coaching_refusal`
  GET /api/routine/trainer/client-progress/{id}/            `client.goal` null, `coaching_refusal`
  GET /api/routine/trainer/client-progress/recent/          `coaching_refusal` per row

The three states: `coaching_frozen` (the renewal found the wallet short) and
`coaching_unpaid` (support returned the month) keep the relation approved — the file
answers, with the record null and `coaching.status` `frozen` / `unpaid`;
`coaching_ended` deletes the relation — nothing answers (403 `not_your_client`, absent
from the lists). A running coaching reads in full; another trainer gets nothing.
"""
from datetime import timedelta

import pytest

from tests.test_coaching_freeze import _coached, _freeze
from tests.test_coaching_months import _end_month

pytestmark = pytest.mark.django_db

PROFILE = "/api/auth/trainer/client-profile/"
PROGRESS = "/api/routine/trainer/client-progress/"
ROSTER = "/api/routine/trainer/client-progress/recent/"
RECORD = ("email", "height", "weight", "age", "gender", "specific_injury",
          "activity_level", "client_goals", "client_preferences", "training_goal",
          "training_place", "training_days_per_week", "injury_areas", "bmi", "bmr", "tdee")


@pytest.fixture(autouse=True)
def coaching_rules(settings):
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)


@pytest.fixture(autouse=True)
def a_cold_cache():
    from training_platform.cache import private_cache

    private_cache().clear()
    yield
    private_cache().clear()


def _pair(make_user, api, tag):
    trainer, client, relation, *_rest = _coached(make_user, api, tag)
    type(client).objects.filter(pk=client.pk).update(
        weight=81.5, height=178, gender="Male", activity_level="Moderate",
        client_goals="strength", specific_injury="left knee")
    return trainer, client, relation


def _paused(relation, state):
    if state == "coaching_frozen":
        _freeze(relation)
    else:
        _end_month(relation, ago=timedelta(days=2))
        relation.periods.update(status="refunded")


def _file(api, trainer, client):
    resp = api(trainer).get(f"{PROFILE}{client.pk}/")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _listed(api, trainer, client):
    rows = api(trainer).get(PROFILE).json()["clients"]
    return next((row for row in rows if row["id"] == client.pk), None)


def _roster_row(api, trainer, client):
    body = api(trainer).get(ROSTER).json()
    return next((row for row in body["recent_activity"]
                 if row["client_info"]["id"] == client.pk), None)


def test_a_running_coaching_reads_in_full(make_user, api):
    trainer, client, _relation = _pair(make_user, api, "b201run")
    row = _file(api, trainer, client)
    assert row["coaching_refusal"] is None
    assert (row["weight"], row["client_goals"], row["specific_injury"]) == (
        81.5, "strength", "left knee")
    assert row["email"] == client.email and row["bmi"]
    assert _listed(api, trainer, client)["weight"] == 81.5
    assert _roster_row(api, trainer, client)["coaching_refusal"] is None
    detail = api(trainer).get(f"{PROGRESS}{client.pk}/").json()
    assert detail["coaching_refusal"] is None and detail["client"]["goal"] == "strength"


@pytest.mark.parametrize("state, status", [("coaching_frozen", "frozen"),
                                           ("coaching_unpaid", "unpaid")])
def test_a_paused_coaching_holds_the_record_back_everywhere(make_user, api, state, status):
    trainer, client, relation = _pair(make_user, api, f"b201{status}")
    _paused(relation, state)

    row = _file(api, trainer, client)
    assert row["coaching_refusal"] == state
    for key in RECORD:
        assert key in row and row[key] is None, (key, row[key])
    # What the screen needs to say why stays: the name and the coaching's state.
    assert row["full_name"] and row["coaching"]["status"] == status

    listed = _listed(api, trainer, client)
    assert listed["coaching_refusal"] == state
    assert all(listed[key] is None for key in RECORD)

    assert _roster_row(api, trainer, client)["coaching_refusal"] == state
    detail = api(trainer).get(f"{PROGRESS}{client.pk}/").json()
    assert detail["coaching_refusal"] == state
    assert detail["client"]["goal"] is None and detail["client_info"]["goal"] is None
    assert "weekly_stats" in detail and "recent_activity" in detail     # training stays


def test_an_ended_coaching_reads_nothing(make_user, api):
    from django.db import transaction

    from users.coaching import end_coaching

    trainer, client, relation = _pair(make_user, api, "b201end")
    with transaction.atomic():
        end_coaching(type(relation).objects.select_for_update().get(pk=relation.pk),
                     reason="unpaid")
    refused = api(trainer).get(f"{PROFILE}{client.pk}/")
    assert (refused.status_code, refused.json()["code"]) == (403, "not_your_client")
    listed = _listed(api, trainer, client)
    assert listed is None or (listed["coaching_refusal"] == "coaching_ended"
                              and all(listed[key] is None for key in RECORD))
    assert _roster_row(api, trainer, client) is None
    assert api(trainer).get(f"{PROGRESS}{client.pk}/").status_code == 403


def test_another_trainer_still_gets_nothing(make_user, api):
    _trainer, client, relation = _pair(make_user, api, "b201other")
    _paused(relation, "coaching_frozen")
    stranger = make_user("b201_stranger", user_type="trainer")
    refused = api(stranger).get(f"{PROFILE}{client.pk}/")
    assert (refused.status_code, refused.json()["code"]) == (403, "not_your_client")
    assert _listed(api, stranger, client) is None
    assert _roster_row(api, stranger, client) is None
    assert api(stranger).get(f"{PROGRESS}{client.pk}/").status_code == 403
