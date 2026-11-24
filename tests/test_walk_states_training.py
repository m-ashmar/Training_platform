"""The walk states of programmes, training, trainers and products (`users/walk/
training.py`, `users/walk/trainer.py`; Road to 10, the app's walk-throughs).

Each state is built as `manage.py seed_walk_states` builds it (`framework.build`), then
read through the API the way the app's screen reads it: the schedule, Home, the
coaching, the trainer's verification and products. Nothing at request time tells these
accounts apart, so what the API answers here is what any account in the same state
gets.
"""
import pytest

pytestmark = pytest.mark.django_db

PASSWORD = "Walk-Throughs-2026!"
HOME = "/api/v1/home"


@pytest.fixture
def walk_media(settings, tmp_path):
    """Verification documents land in a temporary place, apart from the media root."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private")
    settings.COACHING_COMMISSION_RATE = __import__("decimal").Decimal("0.20")
    return tmp_path


def _build(name):
    from users.models import CustomUser
    from users.walk import load_states
    from users.walk.framework import build

    entry = build(load_states()[name], PASSWORD)
    people = {key: CustomUser.objects.get(pk=acc["id"]) for key, acc in entry["accounts"].items()}
    return entry, people


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


def _signs_in(api, email):
    resp = api().post("/api/auth/login/", {"email": email, "password": PASSWORD},
                      content_type="application/json")
    assert resp.status_code == 200, resp.content


# ------------------------------------------------------------------ 1. programmes
def test_a_trainers_programme_that_finished(api, walk_media):
    entry, people = _build("programme_finished")
    me = people["main"]
    _signs_in(api, entry["email"])
    body = _schedule(api, me, entry["ids"]["routine"])
    assert (body["finished"], body["week"], body["weeks_total"]) == (True, 9, 9)
    assert body["today"] is None and body["refusal"] is None
    assert "missed" in [slot["state"] for slot in body["slots"]]
    hero = _home(api, me)["workout_hero"]
    assert (hero["finished"], hero["week"], hero["cta"]) == (True, 9, {"state": "finished"})
    # The coaching is paid: two months renewed after the first, as the hourly run does.
    coaching = api(me).get("/api/v1/coaching/current/").json()["coaching"]
    assert coaching["status"] == "active", coaching
    assert coaching["period"]["number"] == 3


def test_a_trainers_programme_with_today_a_training_day(api, walk_media):
    entry, people = _build("programme_training_today")
    me = people["main"]
    body = _schedule(api, me, entry["ids"]["routine"])
    assert body["today"] == {"day": entry["ids"]["today_day"], "state": "today"}
    assert (body["finished"], body["week"], body["weeks_total"]) == (False, 3, 9)
    hero = _home(api, me)["workout_hero"]
    assert hero["day"] == 16 and hero["cta"]["state"] == "start"


# ------------------------------------------------------------------ 2. bought programme
def test_a_bought_programme_with_a_missed_day_behind_it(api, walk_media):
    entry, people = _build("bought_programme_missed_day")
    me = people["main"]
    body = _schedule(api, me, entry["ids"]["routine"])
    assert body["today"] == {"day": 5, "state": "today"}
    assert (body["week"], body["slots_week"], body["weeks_total"]) == (2, 2, 4)
    assert [slot["state"] for slot in body["slots"]] == ["done", "today", "upcoming"]
    strip = _home(api, me)["workout_hero"]["week_strip"]
    states = [day["state"] for day in strip]
    assert states.count("planned") <= 2 and "planned" not in states[:states.index("today")]
    # Bought from the wallet: the programme is the client's own, owned and readable.
    bought = api(me).get(f"/api/v1/products/{entry['ids']['product']}/",
                         HTTP_X_APP_CHANNEL="apk").json()
    assert bought["owned"] is True and bought["routine_id"] == entry["ids"]["routine"]


# ------------------------------------------------------------------ 3. verification
def test_a_trainers_verification_returned_with_remarks(api, walk_media):
    from users.walk.trainer import RETURNED_REASON

    entry, people = _build("trainer_verification_returned")
    body = api(people["main"]).get("/api/v1/trainer/verification/").json()
    assert body["status"] == "rejected" and body["rejection_reason"] == RETURNED_REASON
    assert body["needs_replacing"] and body["can_submit"] is False
    returned = [d for d in body["documents"] if d["state"] == "rejected"]
    assert returned and returned[0]["rejection_reason"] == RETURNED_REASON


def test_a_trainers_verification_under_review(api, walk_media):
    entry, people = _build("trainer_verification_in_review")
    body = api(people["main"]).get("/api/v1/trainer/verification/").json()
    assert body["status"] == "pending" and body["submitted_at"]
    assert body["can_submit"] is False and not body["missing"]


# ------------------------------------------------------------------ 4. products
def test_a_trainers_products_in_review_on_sale_and_returned(api, walk_media):
    from users.walk.trainer import PRODUCT_RETURNED_REASON

    entry, people = _build("trainer_products")
    rows = api(people["main"]).get("/api/v1/trainer/products/",
                                   HTTP_X_APP_CHANNEL="apk").json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    by_id = {row["id"]: row for row in rows}
    ids = entry["ids"]
    assert by_id[ids["product_on_sale"]]["status"] == "published"
    assert by_id[ids["product_in_review"]]["status"] == "in_review"
    returned = by_id[ids["product_returned"]]
    assert returned["status"] == "draft"
    assert PRODUCT_RETURNED_REASON in str(returned), returned
    # On sale means a client can find it in the catalogue.
    client = api(people["main"])
    catalogue = client.get("/api/v1/products/", HTTP_X_APP_CHANNEL="apk").json()
    catalogue = catalogue["results"] if isinstance(catalogue, dict) else catalogue
    assert ids["product_on_sale"] in [row["id"] for row in catalogue]
