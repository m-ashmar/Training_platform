"""The walk state `plan_lapsed_with_diet` (`users/walk/diet.py`; the manager, for the app,
2026-11-24): `plan_lapsed`, with a diet plan the client built BEFORE the cover ended, so
Nutrition has something to show read-only — the owner's freeze rule (2026-09-19): after a
plan lapses the client still sees their last diet plan and cannot use it.

Built by `seed_walk_states --only plan_lapsed_with_diet` on the shared fixtures, a
database shaped like the developers' (plans in both currencies, no exchange rate), and
read through the routes Nutrition reads.
"""
import pytest
from django.utils import timezone

from tests.walk_fixtures import walk_platform, walk_run  # noqa: F401  (the shared fixtures)

pytestmark = pytest.mark.django_db


def _client(entry):
    from users.models import CustomUser

    return CustomUser.objects.get(pk=entry["accounts"]["main"]["id"])


def test_the_plan_is_frozen_and_the_diet_plan_reads(walk_run, api):
    entry = walk_run("plan_lapsed_with_diet")
    me = _client(entry)
    assert me.email == "walk.plan_lapsed_with_diet@yalla.test"
    c = api(me)

    sub = c.get("/api/subscription/v1/subscriptions/current/").json()
    assert sub["access"] == "frozen" and sub["status"] == "expired", sub

    plans = c.get("/api/diet/v1/my/diet-plans/")
    assert plans.status_code == 200, plans.content
    body = plans.json()
    rows = body["results"] if isinstance(body, dict) and "results" in body else body
    assert entry["ids"]["diet_plan"] in [row["id"] for row in rows], rows

    today = c.get("/api/diet/v1/client/progress/",
                  {"date": timezone.localdate().isoformat()})
    assert today.status_code == 200, today.content
    progress = today.json()
    assert progress["has_active_plan"] is True, progress
    assert progress["total_meals"] > 0, progress


def test_using_it_is_refused_with_the_frozen_answer(walk_run, api):
    from diet.models import Meal

    entry = walk_run("plan_lapsed_with_diet")
    me = _client(entry)
    c = api(me)
    meal = (Meal.objects.filter(diet_plan_id=entry["ids"]["diet_plan"],
                                date=timezone.localdate()).order_by("id").first())
    assert meal is not None, "no meal for today"

    ticked = c.post(f"/api/diet/v1/client/meals/{meal.pk}/complete/", {},
                    content_type="application/json")
    assert ticked.status_code == 403, ticked.content
    assert ticked.json()["code"] == "no_entitlement"

    built = c.post("/api/diet/v1/plans/generate-rule/", {"duration_days": 1},
                   content_type="application/json")
    assert built.status_code == 403, built.content
    assert built.json()["code"] == "no_entitlement"


def test_plan_lapsed_itself_is_unchanged(walk_run, api):
    """`plan_lapsed` stays exactly as it was: no diet plan written (the app found a real
    bug with it as it is)."""
    from diet.models import DietPlan

    entry = walk_run("plan_lapsed")
    assert not DietPlan.objects.filter(user_id=entry["accounts"]["main"]["id"]).exists()
