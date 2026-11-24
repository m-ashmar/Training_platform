"""The walk states of programmes, training, trainers and products (`users/walk/
training.py`, `users/walk/trainer.py`; Road to 10, the app's walk-throughs).

Each state is reset through the command itself (`walk_run`, on a database as a dev server
has it after `bootstrap_platform`: `tests/walk_fixtures.py`) and then read through the API
the way the app's screen reads it: the schedule, Home, the coaching, the trainer's
verification and products, the catalogue. Nothing at request time tells these accounts
apart, so what the API answers here is what any account in the same state gets.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.walk_fixtures import walk_platform, walk_run  # noqa: F401

pytestmark = pytest.mark.django_db

HOME = "/api/v1/home"


def _user(entry, part="main"):
    from users.models import CustomUser

    return CustomUser.objects.get(pk=entry["accounts"][part]["id"])


def _rows(body):
    return body["results"] if isinstance(body, dict) and "results" in body else body


def _home(api, person, channel="apk"):
    from training_platform.cache import private_cache

    private_cache().clear()
    resp = api(person).get(HOME, HTTP_X_APP_CHANNEL=channel)
    assert resp.status_code == 200, resp.content
    return {s["type"]: s["data"] for s in resp.json()["sections"]}


def _schedule(api, person, routine_id):
    resp = api(person).get(f"/api/routine/routines/{routine_id}/schedule/")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _signs_in(api, entry, password):
    resp = api().post("/api/auth/login/", {"email": entry["email"], "password": password},
                      content_type="application/json")
    assert resp.status_code == 200, resp.content


# ------------------------------------------------------------------ 1. programmes
def test_a_trainers_programme_that_finished(walk_run, walk_platform, api):
    entry = walk_run("programme_finished")
    me = _user(entry)
    _signs_in(api, entry, walk_platform)
    body = _schedule(api, me, entry["ids"]["routine"])
    assert (body["finished"], body["week"], body["weeks_total"]) == (True, 9, 9)
    assert body["today"] is None and body["refusal"] is None
    assert "missed" in [slot["state"] for slot in body["slots"]]
    hero = _home(api, me)["workout_hero"]
    assert (hero["finished"], hero["week"], hero["cta"]) == (True, 9, {"state": "finished"})
    # Paid month by month: month 1 held and paid out, two renewals after it.
    coaching = api(me).get("/api/v1/coaching/current/").json()["coaching"]
    assert (coaching["status"], coaching["period"]["number"]) == ("active", 3)


def test_a_trainers_programme_with_today_a_training_day(walk_run, api):
    entry = walk_run("programme_training_today")
    me = _user(entry)
    body = _schedule(api, me, entry["ids"]["routine"])
    assert body["today"] == {"day": entry["ids"]["today_day"], "state": "today"}
    assert (body["finished"], body["week"], body["weeks_total"]) == (False, 3, 9)
    hero = _home(api, me)["workout_hero"]
    assert hero["day"] == 16 and hero["cta"]["state"] == "start"


def test_the_history_is_judged_by_the_days_it_happened(walk_run, api):
    """Nine weeks of workouts written as rows are counted once each, on their days: no
    finished workout dated today, and no badge for the hour the seed ran."""
    from analytics.models import UserActivity
    from routine.models import WorkoutSession

    entry = walk_run("programme_finished")
    me = _user(entry)
    sessions = WorkoutSession.objects.filter(user=me, status="completed")
    activity = UserActivity.objects.filter(user=me, activity_type="routine_completed")
    assert activity.count() == sessions.count() == 32
    assert not activity.filter(timestamp__date=timezone.localdate()).exists()


# ------------------------------------------------------------------ 2. bought programme
def test_a_bought_programme_with_a_missed_day_behind_it(walk_run, api):
    entry = walk_run("bought_programme_missed_day")
    me = _user(entry)
    body = _schedule(api, me, entry["ids"]["routine"])
    assert body["today"] == {"day": 5, "state": "today"}
    assert (body["week"], body["slots_week"], body["weeks_total"]) == (2, 2, 4)
    assert [slot["state"] for slot in body["slots"]] == ["done", "today", "upcoming"]
    states = [day["state"] for day in _home(api, me)["workout_hero"]["week_strip"]]
    assert "planned" not in states[:states.index("today")]       # a missed date is no debt
    bought = api(me).get(f"/api/v1/products/{entry['ids']['product']}/",
                         HTTP_X_APP_CHANNEL="apk").json()
    assert bought["owned"] is True and bought["routine_id"] == entry["ids"]["routine"]


# ------------------------------------------------------------------ 3. verification
def test_a_trainers_verification_returned_with_remarks(walk_run, api):
    from users.walk.trainer import RETURNED_REASON

    entry = walk_run("trainer_verification_returned")
    body = api(_user(entry)).get("/api/v1/trainer/verification/").json()
    assert body["status"] == "rejected" and body["rejection_reason"] == RETURNED_REASON
    assert body["needs_replacing"] and body["can_submit"] is False
    returned = [d for d in body["documents"] if d["state"] == "rejected"]
    assert [d["rejection_reason"] for d in returned] == [RETURNED_REASON]


def test_a_trainers_verification_under_review(walk_run, api):
    entry = walk_run("trainer_verification_in_review")
    body = api(_user(entry)).get("/api/v1/trainer/verification/").json()
    assert body["status"] == "pending" and body["submitted_at"]
    assert body["can_submit"] is False and not body["missing"]


# ------------------------------------------------------------------ 4. products
def test_a_trainers_products_in_review_on_sale_and_returned(walk_run, api):
    from users.walk.trainer import PRODUCT_RETURNED_REASON

    entry = walk_run("trainer_products")
    me, ids = _user(entry), entry["ids"]
    rows = {row["id"]: row for row in _rows(api(me).get(
        "/api/v1/trainer/products/", HTTP_X_APP_CHANNEL="apk").json())}
    assert rows[ids["product_on_sale"]]["status"] == "published"
    assert rows[ids["product_in_review"]]["status"] == "in_review"
    returned = rows[ids["product_returned"]]
    assert returned["status"] == "draft" and PRODUCT_RETURNED_REASON in str(returned)
    catalogue = _rows(api(me).get("/api/v1/products/", HTTP_X_APP_CHANNEL="apk").json())
    assert ids["product_on_sale"] in [row["id"] for row in catalogue]
    assert ids["product_in_review"] not in [row["id"] for row in catalogue]


# ------------------------------------------------------------------ 5. new clients
def test_a_brand_new_client(walk_run, walk_platform, api):
    entry = walk_run("client_brand_new")
    me = _user(entry)
    _signs_in(api, entry, walk_platform)
    profile = api(me).get("/api/auth/user/update/").json()
    assert {"first_name", "date_of_birth", "gender", "training_goal"} <= set(
        profile["onboarding_missing"])
    sections = _home(api, me)
    assert "workout_hero" not in sections
    assert sections["setup_checklist"]["current"] == "profile"
    # The free first workout is offered and not started: nothing done yet.
    assert sections["trial_workout"]["routine_id"] is None
    assert sections["trial_workout"]["times_done"] == 0
    assert api(me).get("/api/v1/coaching/current/").json() == {"coaching": None}


def test_a_client_whose_only_history_is_the_free_first_workout(walk_run, api):
    entry = walk_run("client_trial_only")
    me = _user(entry)
    trial = _home(api, me)["trial_workout"]
    assert (trial["routine_id"], trial["times_done"], trial["place"]) == (
        entry["ids"]["routine"], 1, "home")
    assert trial["last_done_on"] == (timezone.localdate() - timedelta(days=1)).isoformat()
    assert _rows(api(me).get("/api/routine/routines/").json()) == []   # no programme
    sessions = _rows(api(me).get("/api/routine/workout-sessions/").json())
    assert [s["routine"] for s in sessions] == [entry["ids"]["routine"]]


# ------------------------------------------------------------------ the review's B3, B4
def test_no_badge_beyond_what_the_history_earns(walk_run, api):
    """Nine workouts over fifteen days, each at six in the evening: nine counted once,
    one short of `workout_warrior` (10), and no early-bird or night-owl badge for the hour
    the seed happened to run (walk review B3)."""
    from achievements.models import UserAchievement
    from analytics.models import UserActivity

    entry = walk_run("programme_training_today")
    me = _user(entry)
    activity = UserActivity.objects.filter(user=me, activity_type="routine_completed")
    assert activity.count() == 9
    days = [timezone.localtime(row.timestamp).date() for row in activity]
    assert len(set(days)) == 9                              # one a day, each on its own
    assert not activity.filter(timestamp__date=timezone.localdate()).exists()
    earned = set(UserAchievement.objects.filter(user=me)
                 .values_list("achievement__key", flat=True))
    assert not earned & {"workout_warrior", "early_bird", "night_owl"}, earned


def test_a_second_run_leaves_nothing_more_on_the_operators_desk(walk_run):
    """A run retires the last run's trainers; their pending application and their
    product in review are decided as an operator decides them, so the operator's queues
    after run 2 are what they were after run 1 (walk review B4)."""
    from routine.product_models import ProductStatus, RoutineProduct
    from trainer_verification.models import ApplicationState, VerificationApplication

    def desk():
        return (VerificationApplication.objects.filter(state=ApplicationState.PENDING).count(),
                RoutineProduct.objects.filter(status=ProductStatus.IN_REVIEW).count())

    names = ("trainer_verification_in_review", "trainer_products")
    walk_run(*names)
    first = desk()
    assert first == (1, 1)
    walk_run(*names)
    assert desk() == first
