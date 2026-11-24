"""The phase 3 worker's second walk states (`users/walk/diet.py`, `coach_desk.py`): each
made by `seed_walk_states --only <state>` on the shared fixtures, and each read through
the API its screen reads.
"""
import pytest

from tests.walk_fixtures import walk_platform, walk_run  # noqa: F401  (the shared fixtures)

pytestmark = pytest.mark.django_db


def _user(entry, part="main"):
    from users.models import CustomUser

    return CustomUser.objects.get(pk=entry["accounts"][part]["id"])


def _rows(body):
    return body["results"] if isinstance(body, dict) and "results" in body else body


# ------------------------------------------------------------------ the diet question
def test_premium_no_diet_plan(walk_run, api):
    entry = walk_run("premium_no_diet_plan")
    me = _user(entry)

    current = api(me).get("/api/subscription/v1/subscriptions/current/")
    assert current.status_code == 200, current.content
    body = current.json()
    sub = body.get("subscription", body)
    assert sub["status"] == "active"
    assert sub.get("plan_id", sub.get("plan", {}).get("id")) == entry["ids"]["plan"]
    assert entry["ids"]["payment"]

    preferences = api(me).get("/api/diet/v1/preferences/")
    assert preferences.status_code == 200, preferences.content
    assert not preferences.json().get("allergies")          # never answered

    plans = api(me).get("/api/diet/v1/my/diet-plans/")
    assert plans.status_code == 200, plans.content
    assert _rows(plans.json()) == []                         # no plan yet


# ------------------------------------------------------------------ a new trainer
def test_trainer_just_signed_up(walk_run, api):
    entry = walk_run("trainer_just_signed_up")
    me = _user(entry)
    assert entry["role"] == "trainer"

    missing = api(me).get("/api/auth/user/update/").json()["onboarding_missing"]
    assert {"first_name", "last_name"} <= set(missing)
    verification = api(me).get("/api/v1/trainer/verification/").json()
    assert verification["status"] == "not_started", verification

    starters = _rows(api(me).get("/api/routine/templates/public_templates/").json())
    assert starters, "the platform's starter templates are offered"
    own = [t for t in _rows(api(me).get("/api/routine/templates/").json())
           if t.get("created_by") == me.pk]
    assert own == []

    listed = {row["id"] for row in _rows(api().get("/api/v1/trainers/").json())}
    assert me.pk not in listed
    queue = api(me).get("/api/v1/trainer/attention/", HTTP_X_APP_CHANNEL="apk").json()
    assert queue["count"] == 0


# ------------------------------------------------------------------ a full Today queue
def test_trainer_full_queue(walk_run, api):
    entry = walk_run("trainer_full_queue")
    me = _user(entry)
    ids = entry["ids"]

    assert api(me).get("/api/v1/trainer/verification/").json()["status"] == "verified"

    items = api(me).get("/api/v1/trainer/attention/", HTTP_X_APP_CHANNEL="apk").json()["items"]
    kinds = {(item["kind"], item["client"]["id"]) for item in items}
    assert ("new_request", ids["asking"]) in kinds
    assert ("awaiting_first_program", ids["held"]) in kinds
    assert ("unanswered_message", ids["active"]) in kinds
    request = next(i for i in items if i["kind"] == "new_request")
    assert request["new_request"]["request_id"] == ids["request"]
    message = next(i for i in items if i["kind"] == "unanswered_message")
    assert message["unanswered_message"]["conversation_id"] == ids["conversation"]
    held = next(i for i in items if i["kind"] == "awaiting_first_program")
    assert held["awaiting_first_program"]["held_amount"] is not None

    roster = api(me).get("/api/routine/trainer/client-progress/recent/").json()
    row = next(r for r in roster["recent_activity"] if r["client_info"]["id"] == ids["active"])
    assert row["last_session"]["id"] == ids["session"]
    assert row["unread_count"] >= 1
    review = api(me).get(f"/api/routine/workout-sessions/{ids['session']}/?expand=sets")
    assert review.status_code == 200 and review.json()["status"] == "completed"
